"""
core/summarizer.py
3줄 요약(LLM 우선 / 규칙 기반 폴백) + 60초 유튜브 쇼츠 스크립트 생성 담당.

★ v3 — LLM-Hybrid Summarizer:
  - ANTHROPIC_API_KEY 환경변수가 있으면 Claude Haiku로 고품질 3줄 요약 생성
  - API 키 없거나 호출 실패 시 개선된 규칙 기반(v2) 폴백
  - 출력 형식 항상 동일: ① 핵심 정책 / ② 주요 내용 / ③ 영향·시사점

외부 LLM 없이 순수 규칙 기반으로 동작.
훅 유형 A(대비형) / B(질문형) / C(경고형) 자동 선택.

★ MODIFIED v2 — Summarizer Quality Upgrade:
  1) 2단계 생성 (FACT PACK → 스크립트)
       Step 1: 원문에서 구조화 FACT PACK 추출
               (수치 사실 5개, 핵심 이슈 3개, 리스크/기회, 확정/불확실 분리)
       Step 2: FACT PACK만을 재료로 스크립트 조립
  2) 줄임표(… / ...) 완전 금지 — 모든 함수에서 제거
  3) 각 섹션 문장 수 엄격 준수
       [0~5초 훅]       정확히 1문장
       [5~25초 이슈]    ①②③ 각 1문장, 수치 우선
       [25~45초 해석]   정확히 2문장 (원인 → 결과)
       [45~60초 시사점] 정확히 2문장 (개인 1, 기업 1)
  4) 자기검증(self-check) 패스 — 줄임표·섹션 누락 탐지, 1회 자동 수정
"""

import json
import os
import re
from datetime import datetime

import requests

from core.utils import single_line


# ──────────────────────────────────────────────────────
# 시스템 프롬프트 (산업 특화 LLM 브리핑용)
# ──────────────────────────────────────────────────────
SYSTEM_PROMPT = """
당신은 한국 {industry_label} 수출기업 전략 브리핑 전문가입니다.
{industry_variables}
아래 경제 기사를 읽고 다음 4가지 관점에서 각 1문장(30자 이내)으로 분석하세요.

📊 Impact(영향): 이 변화가 {industry_label} 수출기업에 미치는 직접적 영향
📉 Risk(리스크): 주의해야 할 위험 요소
💡 Opportunity(기회): 활용 가능한 기회
✅ Action(즉시 행동): 지금 당장 확인해야 할 것 1가지

[작성 규칙]
- 각 항목에 구체적 수치, 기간, 또는 비율을 반드시 1개 이상 포함하세요
- 추상적 표현("영향이 크다", "주의 필요") 대신 구체적 맥락을 기술하세요
- 줄임표(…) 사용 금지

[예시 — 좋은 분석]
{{"impact": "환율 1,480원 돌파로 반도체 수출 마진 약 3%p 개선 전망",
  "risk": "원자재 수입 원가 동반 상승 시 마진 개선분 상쇄 가능",
  "opportunity": "달러 매출 비중 높은 기업은 2분기 내 환헷지 비율 조정 적기",
  "action": "주요 원자재 공급사 결제 통화별 원가 변동률 즉시 점검"}}

[예시 — 나쁜 분석 (이렇게 쓰지 마세요)]
{{"impact": "영향이 있을 것으로 보입니다",
  "risk": "리스크가 존재합니다",
  "opportunity": "기회가 될 수 있습니다",
  "action": "확인이 필요합니다"}}

출력 형식 (반드시 아래 JSON으로):
{{"impact": "...", "risk": "...", "opportunity": "...", "action": "..."}}
"""


def _resolve_industry_label(industry_key: str) -> str:
    """industry_key에서 사람이 읽을 수 있는 산업 레이블을 반환."""
    if not industry_key or industry_key == "일반":
        return "일반 수출기업"
    try:
        from core.industry_config import get_profile
        return get_profile(industry_key).get("label", "일반 수출기업")
    except ImportError:
        return "일반 수출기업"


def _resolve_industry_variables(industry_key: str) -> str:
    """산업별 핵심 경제 변수를 프롬프트용 텍스트로 반환."""
    if not industry_key or industry_key == "일반":
        return "이 산업의 핵심 변수: 환율, 수출증가율, 물가, 금리"
    try:
        from core.industry_config import get_profile
        profile = get_profile(industry_key)
        crit = profile.get("critical_variables", [])
        weights = profile.get("macro_weights", {})
        parts = []
        if crit:
            parts.append(f"핵심 경제 변수: {', '.join(crit)}")
        # 가중치 높은 지표 강조
        high_weight = [k for k, v in weights.items() if v >= 1.5]
        if high_weight:
            parts.append(f"특히 민감한 지표: {', '.join(high_weight)}")
        return "\n".join(parts) if parts else ""
    except ImportError:
        return ""


# ──────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────
ECONOMIC_KEYWORDS = [
    # 거시경제 지표
    "성장", "GDP", "물가", "인플레이션", "디플레이션", "경기",
    # 통화·금융
    "금리", "기준금리", "환율", "통화", "채권", "주가", "부채",
    # 무역·생산
    "수출", "수입", "무역", "생산", "제조업", "산업",
    # 고용·소득
    "고용", "실업", "소비", "가계", "소득",
    # 재정·정책
    "재정", "정책", "투자", "서비스",
    # 시장
    "기업", "시장", "불황", "회복", "위기", "전망",
    "개선", "악화", "상승", "하락", "증가", "감소",
    # 수치 단위
    "억원", "조원", "달러", "유로", "%", "퍼센트", "만명", "천명",
]

# 훅 유형 판별용 긍정/부정 신호 키워드 (기존 유지)
_POSITIVE_KW = frozenset({
    "성장", "회복", "개선", "상승", "증가", "호조", "확대", "흑자", "안정", "활성"
})
_NEGATIVE_KW = frozenset({
    "위기", "악화", "하락", "감소", "불황", "위험", "둔화", "적자", "침체", "위축", "경고"
})

# ★ MODIFIED v2: FACT PACK 전용 리스크/기회/헤징 신호 키워드
_RISK_SIGNALS = frozenset({
    "위기", "악화", "하락", "감소", "불황", "위험", "둔화",
    "적자", "침체", "위축", "경고", "우려", "부진", "취약",
})
_OPP_SIGNALS = frozenset({
    "성장", "회복", "개선", "상승", "증가", "호조", "확대",
    "흑자", "안정", "활성", "기회", "반등", "가속",
})
# 불확실성 헤징 표현 — 확정 사실 vs 전망 문장 분리에 사용
_HEDGE_SIGNALS = [
    "예상", "전망", "가능성", "것으로 보인다", "할 수 있다",
    "우려", "불확실", "것으로 예상", "추정", "관측",
]

# 훅 3종 템플릿 (18~26자 내외)
_HOOKS = {
    "A": "지표와 체감이 엇갈리는 경제 신호, 60초로 분석합니다.",
    "B": "경기 회복, 정말일까요? 이번 달 핵심 신호 확인합니다.",
    "C": "경제 경고등이 켜졌습니다. 놓치면 안 될 신호 3가지입니다.",
}

# 강도별 해석·시사점 추가 어구 (1=보수적, 5=공격적)
_INTENSITY_INTERP_NOTE = {
    1: "※ 불확실성이 높아 신중한 판단이 필요합니다.",
    2: "※ 단기 변동성에 유의하시기 바랍니다.",
    3: "",
    4: "※ 적극적인 모니터링이 권장됩니다.",
    5: "※ 즉각적인 대응이 필요한 상황입니다.",
}
_INTENSITY_IMPL_NOTE = {
    1: "※ 충분한 검토 후 신중히 결정하시기 바랍니다.",
    2: "※ 보수적 접근을 유지하세요.",
    3: "",
    4: "※ 선제적 대비가 필요합니다.",
    5: "※ 지금 즉시 행동하세요.",
}


# ── 3줄 요약 구조 키워드 사전 ─────────────────────────────
# ① 핵심 정책 (WHAT): 어떤 정책인가 — 주어·대상·정의 포함 문장
_POLICY_KW  = [
    "위해", "목적", "추진", "도입", "시행", "마련", "검토", "계획",
    "방침", "의결", "발표", "정책", "전략", "법안", "제도", "개정",
    "결정", "확정", "채택", "선정",
]
# ② 주요 내용 (HOW): 구체적 시행 방법·수단 포함 문장
_METHOD_KW  = [
    "통해", "적용", "확대", "강화", "지원", "개선", "조정", "변경",
    "운영", "실시", "투입", "배정", "편성", "인하", "인상", "완화",
    "규제", "허용", "제한", "의무", "기준", "대상", "규모",
]
# ③ 영향·시사점 (SO WHAT): 결과·전망·기업 영향 포함 문장
_IMPACT_KW  = [
    "기대", "전망", "예상", "효과", "영향", "결과", "증가", "감소",
    "완화", "개선", "우려", "위험", "기회", "부담", "수혜", "혜택",
    "전환", "변화", "파급", "촉진", "억제",
]

# 구조 레이블 (화면·리포트에 표시)
_LABEL = {
    "policy": "핵심 정책",
    "method": "주요 내용",
    "impact": "영향·시사점",
}


def _score_sent_for(sent: str, keywords: list) -> float:
    """문장이 해당 역할 키워드를 얼마나 포함하는지 0‥1 점수 반환."""
    hits = sum(1 for kw in keywords if kw in sent)
    return hits / max(len(keywords), 1)


def _pick_best(
    candidates: list,
    keywords: list,
    used: set,
    fallback_pool: list,
) -> str:
    """
    키워드 점수 기반 최적 문장 선택.

    1순위: candidates 중 키워드 점수 최고 문장
    2순위: fallback_pool 중 미사용 첫 문장
    3순위: candidates 중 미사용 첫 문장 (키워드 무관)
    """
    # 1순위: 키워드 매칭 최고 점수 문장
    scored = [
        (s, _score_sent_for(s, keywords))
        for s in candidates
        if s not in used
    ]
    scored.sort(key=lambda x: -x[1])
    for s, score in scored:
        if score > 0:
            used.add(s)
            return s

    # 2순위: fallback_pool 미사용 첫 문장
    for s in fallback_pool:
        if s not in used:
            used.add(s)
            return s

    # 3순위: candidates 미사용 아무 문장
    for s in candidates:
        if s not in used:
            used.add(s)
            return s

    return ""


def _clip_sent(s: str, n: int = 9999) -> str:
    """문장을 그대로 반환한다. 잘림(truncation) 금지."""
    return s.strip()


def _dominant_topic_words(pool: list, title_words: set) -> set:
    """
    전체 문장 풀에서 주제 핵심어(명사 2글자+)의 빈도를 분석하여
    상위 8개 단어를 '지배 주제 집합'으로 반환.

    이 집합을 기준으로 문장을 필터링하면 단일 주제 일관성을 높인다.
    """
    freq: dict = {}
    for s in pool:
        for w in re.findall(r"[가-힣]{2,}", s):
            if w not in {"있다", "없다", "이다", "된다", "한다", "하는", "위해",
                         "대해", "통해", "따라", "관련", "등의", "또한", "하지만",
                         "이를", "이에", "이번", "지난", "올해", "현재"}:
                freq[w] = freq.get(w, 0) + 1
    # 제목 단어는 가중치 2배
    for w in title_words:
        if w in freq:
            freq[w] += freq[w]
    top = sorted(freq, key=lambda x: -freq[x])[:8]
    return set(top)


def _topic_score(sent: str, dominant: set) -> float:
    """문장이 지배 주제 단어를 얼마나 포함하는지 0‥1 점수."""
    words = set(re.findall(r"[가-힣]{2,}", sent))
    hits  = len(words & dominant)
    return hits / max(len(dominant), 1)


def _structured_3line(
    all_sents: list,
    top_sents: list,
    title: str = "",
) -> str:
    """
    전체 본문 맥락을 분석하는 구조화 3줄 요약 생성기 (v4).

    개선사항:
        1) 지배 주제어 분석 — 문서 전체에서 핵심 명사 빈도 추출
        2) 주제 일관성 필터 — dominant topic 포함 문장만 우선 탐색
        3) 논리 흐름 구조화 — WHAT → HOW → SO WHAT 역할 분리
        4) 위치 + 역할 + 주제 3중 점수로 최적 문장 선택
        5) ★ v4: 제목 관련성 필터 — 제목 키워드와 무관한 사이드바/탐색 콘텐츠 제거

    출력 형식 (항상 정확히 3줄):
        ① [핵심 정책] 해당 정책의 목적·핵심 방향 (1문장)
        ② [주요 내용] 실행 방식·주요 조치·핵심 사례 (1문장)
        ③ [영향·시사점] 산업·시장·국가 전략 영향 (1문장)

    보장:
        - 항상 정확히 3줄 (레이블 포함)
        - 줄임표(… / ...) 사용 금지
        - 서로 다른 주제 혼합 방지
    """
    used: set = set()

    # ── 전체 문장 풀 (중복 제거, 원래 순서 유지) ─────────────
    pool = list(dict.fromkeys(all_sents))

    # ── ★ v4: 제목 관련성 필터 (사이드바/네비 문장 제거) ──────
    # KDI 등 페이지에서 본문 외 사이드바·관련기사 목록이 포함되는 경우,
    # 제목 키워드와 전혀 겹치지 않는 문장을 필터링한다.
    if title:
        _title_words = set(re.findall(r"[가-힣]{2,}", title))
        _title_words -= {"있다", "없다", "이다", "된다", "한다", "하는", "위해",
                         "관련", "등의", "또한", "이번", "지난", "올해", "현재",
                         "경우", "때문", "이후", "사이", "이상", "이하"}
        # 핵심 키워드만 추출 (조사 제거: 2~4자 한글 단어)
        _core_words = {w for w in _title_words if 2 <= len(w) <= 4}
        if _core_words and len(pool) > 5:
            # 제목 핵심어가 문장 내에 부분 문자열로 포함되는지 체크
            def _has_title_word(sent: str) -> bool:
                return any(tw in sent for tw in _core_words)

            _relevant = [s for s in pool if _has_title_word(s)]
            # 관련 문장이 최소 5개 이상일 때만 필터 적용 (너무 적으면 원본 유지)
            if len(_relevant) >= 5:
                pool = _relevant
                # top_sents도 동일 필터 적용 (fallback pool로 사이드바 문장 유입 방지)
                _relevant_set = set(_relevant)
                top_sents = [s for s in top_sents if s in _relevant_set]

    if not pool:
        return (
            f"① [{_LABEL['policy']}] (내용 없음)\n"
            f"② [{_LABEL['method']}] (내용 없음)\n"
            f"③ [{_LABEL['impact']}] (내용 없음)"
        )

    # ── 지배 주제어 집합 추출 ─────────────────────────────────
    title_words = set(re.findall(r"[가-힣]{2,}", title)) if title else set()
    dominant    = _dominant_topic_words(pool, title_words)

    # ── 주제 일관성 기준 상위 문장 풀 ────────────────────────
    # topic_score 0.1 이상 문장 → 주제 집중 풀
    topic_pool = [s for s in pool if _topic_score(s, dominant) >= 0.1] or pool

    # ── ① 핵심 정책 (WHAT) ──────────────────────────────────
    # 범위: 앞 50% 문장 + 제목 연관 문장
    # 목적: "무엇을 왜 하는지" — 정책 주어·목적·배경 포함 문장
    n = len(pool)
    front       = pool[:max(1, n * 5 // 10)]
    title_sents = [s for s in topic_pool if title_words & set(re.findall(r"[가-힣]{2,}", s))]
    line1_raw   = _pick_best(
        candidates    = title_sents + front,
        keywords      = _POLICY_KW,
        used          = used,
        fallback_pool = top_sents,
    )

    # ── ② 주요 내용 (HOW) ───────────────────────────────────
    # 범위: 전체에서 ①과 주제 일관성이 높은 문장 우선
    # 목적: "어떻게 시행하는지" — 구체 수단·조치·수치 포함 문장
    # 수치 포함 문장 우선 (규모·비율이 주요 내용의 핵심)
    numeric_sents = [
        s for s in topic_pool
        if s not in used and re.search(r"\d+[\.,]?\d*\s*(%|원|달러|위|배|건|억|조|조원|억원|만명)", s)
    ]
    mid           = pool[n // 5 : n * 4 // 5]
    line2_raw     = _pick_best(
        candidates    = numeric_sents + mid + topic_pool,
        keywords      = _METHOD_KW,
        used          = used,
        fallback_pool = top_sents,
    )

    # ── ③ 영향·시사점 (SO WHAT) ─────────────────────────────
    # 범위: 문서 뒤 45% 우선 (결론·전망은 후반 집중)
    # 목적: "어떤 영향을 미치는지" — 전망·기대·우려·파급 포함 문장
    rear          = pool[max(0, n * 55 // 100):]
    hedge_sents   = [
        s for s in topic_pool
        if s not in used and any(h in s for h in _HEDGE_SIGNALS)
    ]
    line3_raw     = _pick_best(
        candidates    = rear + hedge_sents + topic_pool,
        keywords      = _IMPACT_KW,
        used          = used,
        fallback_pool = top_sents,
    )

    # ── 최종 조립 ────────────────────────────────────────────
    def _fmt(label: str, raw: str) -> str:
        body = _clip_sent(raw.strip(), n=90) if raw.strip() else f"({label} 정보 없음)"
        return f"[{label}] {body}"

    return "\n".join([
        f"① {_fmt(_LABEL['policy'], line1_raw)}",
        f"② {_fmt(_LABEL['method'], line2_raw)}",
        f"③ {_fmt(_LABEL['impact'], line3_raw)}",
    ])


# ──────────────────────────────────────────────────────
# 1. 규칙 기반 추출 요약 (훅 생성·호환용)
# ──────────────────────────────────────────────────────
# ★ v3: LLM 기반 3줄 요약 (ANTHROPIC_API_KEY 환경변수 필요)
# ──────────────────────────────────────────────────────

_LLM_PROMPT = """\
아래 기사를 읽고, 시스템 프롬프트의 4가지 관점(Impact, Risk, Opportunity, Action)에서 분석하세요.

[작성 규칙]
- 반드시 본문 전체 맥락을 기반으로 작성
- 각 항목에 구체적 수치, 기간, 또는 비율을 1개 이상 포함
- 추상적 표현 금지 ("영향이 크다" 대신 구체적 맥락 기술)
- 줄임표(…) 사용 금지
- 각 항목 30자 이내, 한국어 자연스러운 문장
{industry_context}
[기사 제목]
{title}

[기사 본문]
{body}

반드시 아래 JSON 형식으로만 출력하세요:
{{"impact": "...", "risk": "...", "opportunity": "...", "action": "..."}}"""


# ──────────────────────────────────────────────────────
# LLM 제공자 헬퍼 함수
# ──────────────────────────────────────────────────────

def _get_llm_key() -> str:
    """
    GROQ_API_KEY 환경변수 반환 (없으면 st.secrets 시도).
    키 발급: https://console.groq.com (무료, 카드 불필요)
    """
    try:
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if key:
            return key
        import streamlit as st
        return (st.secrets.get("groq") or {}).get("api_key", "").strip()
    except Exception:
        return ""


def _validate_output(raw: str) -> str | None:
    """LLM 출력에서 ①②③ 세 줄 추출·검증. 성공 시 문자열, 실패 시 None."""
    if not raw or "①" not in raw:
        return None
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    valid = [l for l in lines if l.startswith(("①", "②", "③"))]
    if len(valid) == 3:
        return "\n".join(valid)
    return None


def _build_industry_context(industry_key: str) -> str:
    """산업 키에 따른 LLM 프롬프트 컨텍스트 블록 생성."""
    if not industry_key or industry_key == "일반":
        return ""
    try:
        from core.industry_config import get_profile
        profile = get_profile(industry_key)
    except ImportError:
        return ""
    label = profile.get("label", industry_key)
    crit_vars = ", ".join(profile.get("critical_variables", []))
    return (
        f"\n[산업 맞춤 분석 관점]\n"
        f"- 분석 관점: {label} 수출기업 CEO를 위한 브리핑입니다\n"
        f"- 중요 경제 변수: {crit_vars}\n"
        f"- ③ 영향·시사점은 반드시 {label} 관점에서 작성하세요\n"
    )


def _summarize_with_llm(text: str, title: str = "", industry_key: str = "일반") -> dict | str | None:
    """
    Groq (Llama 3.3 70B) API 호출로 4-frame 요약 생성.

    반환값:
      - 성공 시: dict {"impact", "risk", "opportunity", "action"}
      - JSON 파싱 실패 시: str (기존 3줄 텍스트 폴백)
      - 호출 실패 시: None

    무료 한도: 30 RPM / 14,400 RPD
    키 발급: https://console.groq.com → API Keys → Create
    환경변수: GROQ_API_KEY=<your_key>
              또는 .streamlit/secrets.toml 의 [groq] api_key = "..."
    """
    api_key = _get_llm_key()
    if not api_key:
        return None

    body_trunc = text[:3000].strip()
    if len(body_trunc) < 80:
        return None

    industry_context = _build_industry_context(industry_key)
    industry_label = _resolve_industry_label(industry_key)
    industry_variables = _resolve_industry_variables(industry_key)
    prompt = _LLM_PROMPT.format(title=title or "", body=body_trunc, industry_context=industry_context)
    system_msg = SYSTEM_PROMPT.format(
        industry_label=industry_label,
        industry_variables=industry_variables,
    ).strip()

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "llama-3.3-70b-versatile",
                "messages":    [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens":  4096,
                "temperature": 0.3,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[summarizer] Groq API 오류 {resp.status_code}: {resp.text[:200]}")
            return None

        raw_out = resp.json()["choices"][0]["message"]["content"].strip()

        # 4-frame JSON 파싱 시도
        parsed = _parse_4frame_json(raw_out)
        if parsed:
            return parsed

        # JSON 파싱 실패 시 기존 3줄 텍스트 폴백
        validated = _validate_output(raw_out)
        if validated:
            print("[summarizer] 4-frame JSON 파싱 실패 → 3줄 텍스트 폴백")
            return validated
        return None

    except Exception as e:
        print(f"[summarizer] Groq 호출 실패: {e}")
        return None


def _parse_4frame_json(raw: str) -> dict | None:
    """LLM 출력에서 4-frame JSON을 파싱. 성공 시 dict, 실패 시 None."""
    if not raw:
        return None

    # JSON 블록 추출 (```json ... ``` 또는 { ... })
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # 멀티라인 JSON 객체 매칭 (중첩 없는 단일 객체)
        json_match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
        else:
            return None

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    required_keys = {"impact", "risk", "opportunity", "action"}
    if not required_keys.issubset(data.keys()):
        return None

    # 모든 값이 비어있지 않은지 확인
    if not all(isinstance(data[k], str) and data[k].strip() for k in required_keys):
        return None

    return {k: data[k].strip() for k in required_keys}


_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "summary_cache.json")
_CACHE_TTL_DAYS = 7


def _load_summary_cache() -> dict:
    """요약 캐시 파일 로드."""
    try:
        import pathlib
        p = pathlib.Path(_CACHE_PATH)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_summary_cache(cache: dict) -> None:
    """요약 캐시 파일 저장."""
    try:
        import pathlib
        p = pathlib.Path(_CACHE_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _cache_key(text: str, industry_key: str) -> str:
    """텍스트 + 산업 키 기반 캐시 키 생성."""
    import hashlib
    content = f"{industry_key}|{text[:500]}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def summarize_3line(
    text: str,
    title: str = "",
    industry_key: str = "일반",
) -> tuple[dict | str, str]:
    """
    정책 브리핑용 요약 생성 (v5 공개 인터페이스).

    반환: (summary, source)
      summary = dict {"impact","risk","opportunity","action"} (4-frame)
              | str (기존 3줄 텍스트 폴백)
      source  = "groq" | "rule" | "cache"

    우선순위:
      0) 캐시 히트 시 캐시 반환 (TTL 7일)
      1) GROQ_API_KEY 있으면 Llama 3.3 70B → 4-frame JSON 시도
      2) JSON 파싱 실패 시 기존 3줄 텍스트 (LLM)
      3) 키 없거나 호출 실패 시 규칙 기반 폴백
    """
    # title이 dict로 전달된 경우 방어
    _title_str = title if isinstance(title, str) else str(title.get("title", "")) if isinstance(title, dict) else ""

    # ── 캐시 확인 ──
    _ck = _cache_key(text, industry_key)
    _cache = _load_summary_cache()
    if _ck in _cache:
        _entry = _cache[_ck]
        # TTL 확인
        try:
            _cached_at = datetime.fromisoformat(_entry.get("cached_at", ""))
            if (datetime.now() - _cached_at).days < _CACHE_TTL_DAYS:
                return _entry["summary"], "cache"
        except Exception:
            pass

    # ── LLM 시도 ──
    llm_result = _summarize_with_llm(text, _title_str, industry_key=industry_key)
    if llm_result:
        if isinstance(llm_result, dict):
            print(f"[summarizer] [OK] Groq 4-frame 요약 성공")
        else:
            print(f"[summarizer] [OK] Groq 요약 성공 ({len(llm_result)}자)")
        # 캐시 저장
        _cache[_ck] = {"summary": llm_result, "source": "groq", "cached_at": datetime.now().isoformat()}
        _save_summary_cache(_cache)
        return llm_result, "groq"

    # ── 규칙 기반 폴백 ──
    print(f"[summarizer] 규칙 기반 폴백 (Groq 키: {'있음' if _get_llm_key() else '없음'})")
    result = summarize_rule_based(text, _title_str, max_sentences=3, industry_key=industry_key)
    return result, "rule"


# ──────────────────────────────────────────────────────
# 1. 규칙 기반 추출 요약 (훅 생성·호환용)
# ──────────────────────────────────────────────────────
def summarize_rule_based(
    text: str,
    title: str = "",
    max_sentences: int = 3,
    industry_key: str = "일반",
) -> str:
    """
    텍스트에서 중요 문장을 점수화하여 추출 요약을 반환한다.
    외부 LLM 사용 없이 순수 규칙 기반으로 동작한다.

    점수 기준:
      - 경제 키워드 포함: +2.0 (키워드당)
      - 리스크/기회 키워드: +3.0 (키워드당)
      - 문서 앞 30% 위치: +3.0
      - 숫자·단위 포함:   +2.0
      - 적정 문장 길이(30~100자): +1.0
      - 제목 단어 overlap: +1.5 (단어당)
      - 산업 키워드 매칭: +2.5 (키워드당)

    ★ v4: 리스크/기회 키워드 가중치 + 산업별 핵심변수 부스트 추가.
    """
    if not text:
        return title if isinstance(title, str) else "본문 없음"

    # title이 dict로 전달된 경우 방어
    _title_str = title if isinstance(title, str) else str(title.get("title", "")) if isinstance(title, dict) else ""

    raw_sents = re.split(r"(?<=[.?!다요])\s+|\n", text)
    sentences = [s.strip() for s in raw_sents if 15 <= len(s.strip()) <= 200]

    if not sentences:
        return text.strip()

    # 산업 키워드 + 핵심 변수 로드
    _ind_kws: list[str] = []
    _crit_vars: list[str] = []
    if industry_key and industry_key != "일반":
        try:
            from core.industry_config import get_profile
            _profile = get_profile(industry_key)
            _ind_kws = _profile.get("keywords", [])
            _crit_vars = _profile.get("critical_variables", [])
        except ImportError:
            pass

    # 리스크/기회 키워드 (macro_utils에서 import)
    try:
        from core.macro_utils import _ECON_KW, _RISK_KW, _OPP_KW
    except ImportError:
        _ECON_KW = []
        _RISK_KW = []
        _OPP_KW = []

    total = len(sentences)
    title_words = set(re.findall(r"[가-힣]{2,}", _title_str)) if _title_str else set()

    scored = []
    for idx, sent in enumerate(sentences):
        score = 0.0

        # 1) 경제 키워드 점수
        score += sum(2.0 for kw in ECONOMIC_KEYWORDS if kw in sent)

        # 2) 앞부분 위치 가중치 (상위 30%)
        if idx < total * 0.3:
            score += 3.0

        # 3) 숫자·단위 포함 여부
        if re.search(r"\d+[\.,]?\d*\s*(%|원|달러|배|명|개월|분기|년|위)", sent):
            score += 2.0

        # 4) 적정 길이 (30~100자 선호)
        if 30 <= len(sent) <= 100:
            score += 1.0

        # 5) 제목과 한국어 2글자 이상 단어 겹침
        if title_words:
            sent_words = set(re.findall(r"[가-힣]{2,}", sent))
            score += len(title_words & sent_words) * 1.5

        # 6) 산업 키워드 가중치
        score += sum(2.5 for kw in _ind_kws if kw in sent)

        # 7) 리스크/기회 키워드 가중치 (관련도 높은 문장 부스트)
        score += sum(3.0 for kw in _RISK_KW if kw in sent)
        score += sum(3.0 for kw in _OPP_KW if kw in sent)

        # 8) 산업별 핵심 변수 매칭 (환율, CPI 등)
        score += sum(3.0 for cv in _crit_vars if cv.replace("(", "").replace(")", "") in sent)

        scored.append((score, idx, sent))

    # 점수 내림차순 → 원래 순서(idx) 오름차순으로 최종 정렬
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = sorted(scored[:max_sentences], key=lambda x: x[1])
    extracted = [s for _, _, s in top]

    # ── 구조화 3줄 요약: max_sentences >= 3이면 항상 적용 ─
    if max_sentences >= 3 and sentences:
        return _structured_3line(sentences, extracted, _title_str)

    # 2줄 이하 요청 시에만 단순 join
    summary = " ".join(extracted)
    return summary if summary else text.strip()


# ──────────────────────────────────────────────────────
# 2. 훅 유형 판별
# ──────────────────────────────────────────────────────
def _pick_hook_type(summaries: list) -> str:
    """
    요약 내용의 긍정/부정 신호 비율로 훅 유형을 결정한다.

    Returns:
        "A" 대비형  : 긍정·부정 신호 혼재 (엇갈리는 경제 신호)
        "B" 질문형  : 긍정 우세, 불확실 (회복 맞나요?)
        "C" 경고형  : 부정 우세 (위험 신호)
    """
    pos = sum(1 for s in summaries for kw in _POSITIVE_KW if kw in s)
    neg = sum(1 for s in summaries for kw in _NEGATIVE_KW if kw in s)

    if pos > 0 and neg > 0:
        return "A"
    elif neg > pos:
        return "C"
    else:
        return "B"


# ──────────────────────────────────────────────────────
# 3. 이슈 문장 압축 (★ MODIFIED v2: 줄임표 금지, 완전 문장 반환)
# ──────────────────────────────────────────────────────
def _compress_issue(summary: str, max_len: int = 32) -> str:
    """
    요약에서 방향성(상승/하락/증가/감소 등)이 담긴 핵심 문장을 반환한다.

    ★ MODIFIED v2:
      - "…" 잘림 완전 제거 — 완전한 문장을 반환한다.
      - max_len 파라미터는 하위 호환성을 위해 유지하지만 더 이상 강제 적용하지 않는다.
      - 수치+방향 패턴이 있으면 그 문장 전체를, 없으면 첫 문장 전체를 반환한다.
    """
    t = single_line(summary)

    # 수치 + 방향 패턴 우선 탐색
    m = re.search(
        r"[^.!?]*\d+[\.,]?\d*\s*(%|원|달러|배)[^.!?]*"
        r"(상승|하락|증가|감소|개선|악화|회복|위축|확대|축소|흑자|적자).{0,20}",
        t,
    )
    if m:
        return m.group().strip()   # ★ MODIFIED v2: 줄임표 없이 완전 반환

    # 첫 번째 의미 있는 문장 완전 반환 (자르지 않음)
    parts = re.split(r"(?<=[.?!다요])\s+", t)
    return parts[0].strip() if parts else t


# ──────────────────────────────────────────────────────
# 4. 해석 2문장 생성 (★ MODIFIED v2: 줄임표 금지)
# ──────────────────────────────────────────────────────
def _make_interpretation(s0: str, s1: str) -> tuple:
    """
    두 요약 텍스트에서 선행-후행 관점의 해석 2문장을 생성한다.

    ★ MODIFIED v2: _nth_sent 내부의 "…" 잘림 완전 제거.
    """

    def _nth_sent(t: str, n: int, maxlen: int = 70) -> str:
        t = single_line(t)
        parts = [p.strip() for p in re.split(r"(?<=[.?!다요])\s+", t) if p.strip()]
        s = parts[n - 1] if len(parts) >= n else (parts[-1] if parts else t)
        return s   # ★ MODIFIED v2: "…" 잘림 제거 (maxlen 파라미터 무시)

    s0_parts = re.split(r"(?<=[.?!])\s", s0)
    s1_parts = re.split(r"(?<=[.?!])\s", s1)

    raw1 = _nth_sent(s0, 2) if len(s0_parts) > 1 else _nth_sent(s1, 1)
    raw2 = _nth_sent(s1, 2) if len(s1_parts) > 1 else _nth_sent(s0, 1)

    _CONJ_START = (
        "이", "그", "이는", "또한", "하지만", "따라서",
        "그러나", "특히", "우리", "현재", "이에", "이와",
    )

    interp1 = (
        raw1 if any(raw1.startswith(c) for c in _CONJ_START)
        else f"이는 선행적으로 {raw1}"
    )
    interp2 = (
        raw2 if any(raw2.startswith(c) for c in _CONJ_START)
        else f"후행 지표로 보면 {raw2}"
    )

    return interp1, interp2


# ──────────────────────────────────────────────────────
# 5. 개인/기업 시사점 생성 (★ MODIFIED v2: 줄임표 금지)
# ──────────────────────────────────────────────────────
def _make_implications(s2: str) -> tuple:
    """
    세 번째 기사 요약에서 개인·기업 시사점을 각각 추출한다.
    구독/좋아요 등 CTA 없이 실질적 시사점만 반환한다.

    ★ MODIFIED v2: 80자 "…" 잘림 완전 제거.
    """
    s2_parts = [
        p.strip()
        for p in re.split(r"(?<=[.?!다요])\s+", single_line(s2))
        if p.strip()
    ]

    if len(s2_parts) >= 2:
        personal_raw = s2_parts[0]
        corp_raw = s2_parts[1]
    else:
        personal_raw = s2_parts[0] if s2_parts else s2.strip()
        corp_raw = s2_parts[0] if s2_parts else s2.strip()

    _PERSONAL_START = ("개인", "가계", "소비자", "근로자", "직장인", "나는", "우리는")
    _CORP_START = ("기업", "회사", "사업자", "경영자", "제조업")

    personal = (
        personal_raw if any(personal_raw.startswith(k) for k in _PERSONAL_START)
        else f"개인은 {personal_raw}"
    )
    corp = (
        corp_raw if any(corp_raw.startswith(k) for k in _CORP_START)
        else f"기업은 {corp_raw}"
    )

    # ★ MODIFIED v2: "…" 80자 잘림 제거 (아래 두 줄 삭제)
    # if len(personal) > 80: personal = personal[:80] + "…"
    # if len(corp) > 80:     corp = corp[:80] + "…"

    return personal, corp


# ──────────────────────────────────────────────────────
# 6. 훅 3종 전체 생성 (UI 선택용, 변경 없음)
# ──────────────────────────────────────────────────────
def generate_all_hooks(s0: str, s1: str, s2: str) -> dict:
    """
    A/B/C 3가지 훅 텍스트를 모두 생성하여 dict로 반환한다.
    앱에서 라디오 버튼 선택지로 표시할 때 사용한다.

    Args:
        s0, s1, s2: 기사 요약 텍스트 (훅 템플릿 맞춤화에 사용)

    Returns:
        {"A": str, "B": str, "C": str}
    """
    result = {}
    num_m = re.search(r"\d+[\.,]?\d*\s*(%|원|달러|배)", s0)
    for key in ("A", "B", "C"):
        h = _HOOKS[key]
        # C형: 수치가 있으면 더 구체적인 훅으로 교체
        if key == "C" and num_m:
            h = (
                f"{num_m.group()} 변화! "
                "지금 경제에 무슨 일이 있는지 60초로 알려드립니다."
            )
        result[key] = h
    return result


# ══════════════════════════════════════════════════════
# ★ MODIFIED v2: FACT PACK 빌더 — 2단계 생성의 Step 1
# ══════════════════════════════════════════════════════

def _split_sentences_full(text: str) -> list:
    """
    ★ MODIFIED v2: 텍스트를 문장 단위로 분리한다.
    15~250자 범위의 문장만 반환. FACT PACK 구성에 사용.
    ★ TASK-01: 줄임표 제거 — 소스 텍스트에 포함된 말줄임 처리
    """
    raw = re.split(r"(?<=[.?!다요])\s+|\n", text)
    result = []
    for s in raw:
        s = s.strip()
        if not s:
            continue
        # 문장 끝 줄임표 제거 (소스 텍스트의 말줄임 처리)
        s = re.sub(r"\s*[…]+\s*$", "", s).strip()
        s = re.sub(r"\.{2,}\s*$", ".", s).strip()
        if 15 <= len(s) <= 250:
            result.append(s)
    return result


def _score_sentence_standalone(sent: str, title_words: set) -> float:
    """
    ★ MODIFIED v2: 단일 문장의 경제 관련 점수를 계산한다.
    FACT PACK 구성 시 문장 선별에 사용.
    """
    score = 0.0
    score += sum(2.0 for kw in ECONOMIC_KEYWORDS if kw in sent)
    if re.search(
        r"\d+[\.,]?\d*\s*(%|원|달러|배|명|개월|분기|년|위|포인트|bp)", sent
    ):
        score += 3.0
    if 30 <= len(sent) <= 120:
        score += 1.0
    if title_words:
        sent_words = set(re.findall(r"[가-힣]{2,}", sent))
        score += len(title_words & sent_words) * 1.5
    return score


def build_fact_pack(text: str, title: str = "") -> dict:
    """
    ★ MODIFIED v2: Step 1 — 기사 원문에서 구조화된 FACT PACK을 추출한다.

    스크립트 생성 전에 반드시 이 단계를 거쳐 사실 기반 카탈로그를 만든다.
    완전한 원문을 스캔하므로, 3-문장 요약 단계에서 손실된 수치·맥락이 보존된다.

    FACT PACK 구조:
      title        str          기사 제목
      key_facts    list[str]    수치 포함 핵심 문장 (최대 5개) — 이슈 섹션의 주재료
      key_issues   list[str]    경제 키워드 고득점 문장 (최대 3개) — key_facts 보충
      risk         str          위험 신호 문장 (없으면 빈 문자열)
      opportunity  str          기회 신호 문장 (없으면 빈 문자열)
      confirmed    list[str]    헤징 없는 확정 사실 (최대 4개) — 해석·시사점 재료
      uncertain    list[str]    헤징 있는 불확실 전망 (최대 2개)
      has_numbers  bool         수치 정보 포함 여부

    Returns:
        위 항목으로 구성된 dict
    """
    if not text:
        return {
            "title": title, "key_facts": [], "key_issues": [],
            "risk": "", "opportunity": "", "confirmed": [], "uncertain": [],
            "has_numbers": False,
        }

    title_words = set(re.findall(r"[가-힣]{2,}", title)) if title else set()
    sentences = _split_sentences_full(text)

    if not sentences:
        return {
            "title": title,
            "key_facts": [text.strip()],
            "key_issues": [text.strip()],
            "risk": "", "opportunity": "", "confirmed": [], "uncertain": [],
            "has_numbers": bool(re.search(r"\d", text)),
        }

    # 전체 문장에 점수 부여 → 내림차순 정렬
    scored = [
        (s, _score_sentence_standalone(s, title_words))
        for s in sentences
    ]
    scored_sorted = sorted(scored, key=lambda x: -x[1])

    # 수치 포함 핵심 사실 (최대 5개) — 이슈 섹션 주재료
    _num_pat = re.compile(
        r"\d+[\.,]?\d*\s*(%|원|달러|배|명|개월|분기|년|위|포인트|bp)"
    )
    key_facts = [s for s, _ in scored_sorted if _num_pat.search(s)][:5]

    # 경제 고득점 핵심 이슈 (최대 3개) — key_facts 보충용
    key_issues = [s for s, sc in scored_sorted if sc >= 2.0][:3]
    if not key_issues:
        key_issues = [s for s, _ in scored_sorted[:3]]

    # 위험 신호 — 리스크 키워드 포함 상위 문장
    risk = next(
        (s for s, _ in scored_sorted if any(k in s for k in _RISK_SIGNALS)),
        "",
    )

    # 기회 신호 — 긍정 키워드 포함 상위 문장
    opportunity = next(
        (s for s, _ in scored_sorted if any(k in s for k in _OPP_SIGNALS)),
        "",
    )

    # 확정 사실: 헤징 표현 없는 문장
    confirmed = [
        s for s in sentences
        if not any(h in s for h in _HEDGE_SIGNALS) and 20 <= len(s) <= 200
    ][:4]

    # 불확실 전망: 헤징 표현 있는 문장
    uncertain = [
        s for s in sentences
        if any(h in s for h in _HEDGE_SIGNALS) and 20 <= len(s) <= 200
    ][:2]

    return {
        "title": title,
        "key_facts": key_facts,
        "key_issues": key_issues,
        "risk": risk,
        "opportunity": opportunity,
        "confirmed": confirmed,
        "uncertain": uncertain,
        "has_numbers": len(key_facts) > 0,
    }


# ──────────────────────────────────────────────────────
# ★ MODIFIED v2: FACT PACK 기반 섹션 빌더 (Step 2 보조 함수)
# ──────────────────────────────────────────────────────

def _build_issue_sentence(fact_pack: dict, index: int) -> str:
    """
    ★ MODIFIED v2: FACT PACK에서 핵심 이슈 문장 1개를 구성한다.
    ★ TASK-01: 줄임표 안전 제거 추가
    ★ TASK-02: 마커 형식을 "- 이슈 N:" 으로 변경 (이슈별 명시적 구분)

    우선순위:
      1) key_facts  수치 포함 완전 문장
      2) key_issues 경제 키워드 고득점 완전 문장
      3) "수치 정보는 기사에 제한적입니다" 대체 문장

    규칙: 줄임표("…") 절대 금지. 완전한 문장만 반환.
    """
    marker = f"이슈{index + 1}"

    def _clean(s: str) -> str:
        """줄임표 안전 제거."""
        s = re.sub(r"\s*…+", "", s)
        s = re.sub(r"\.{3,}", ".", s)
        return s.strip()

    # 1순위: 수치 포함 핵심 사실
    if fact_pack.get("key_facts"):
        return f"{marker}: {_clean(fact_pack['key_facts'][0])}"

    # 2순위: 경제 고득점 이슈
    if fact_pack.get("key_issues"):
        return f"{marker}: {_clean(fact_pack['key_issues'][0])}"

    # 폴백: 수치 없음 명시
    return f"{marker}: 수치 정보는 기사에 제한적입니다."


def _build_interpretation_v2(fp0: dict, fp1: dict) -> tuple:
    """
    ★ MODIFIED v2: FACT PACK 기반 해석 2문장 구성.

    문장 1 (원인/선행 프레임): fp0의 리스크 또는 확정 사실
    문장 2 (결과/방향 프레임): fp1의 기회 또는 핵심 이슈

    규칙: 줄임표("…") 절대 금지. 냉정한 분석 어조 유지.
    """
    _CONJ_START = (
        "이", "그", "이는", "또한", "하지만", "따라서",
        "그러나", "특히", "우리", "현재", "이에", "이와",
    )

    # 문장 1: 원인·선행 요소 (리스크 우선 → 확정 사실 → 핵심 이슈)
    raw1 = (
        fp0.get("risk")
        or (fp0["confirmed"][0] if fp0.get("confirmed") else "")
        or (fp0["key_issues"][0] if fp0.get("key_issues") else "")
    )
    if not raw1:
        interp1 = "현재 경제 지표들은 복합적인 방향성을 보이고 있습니다."
    elif any(raw1.startswith(c) for c in _CONJ_START):
        interp1 = raw1
    else:
        interp1 = f"이는 {raw1}"

    # 문장 2: 결과·후행 요소 (기회 우선 → 핵심 이슈 → 확정 사실)
    raw2 = (
        fp1.get("opportunity")
        or (fp1["key_issues"][0] if fp1.get("key_issues") else "")
        or (fp1["confirmed"][0] if fp1.get("confirmed") else "")
    )
    if not raw2:
        interp2 = "후행 지표를 통한 추가 확인이 필요한 상황입니다."
    elif any(raw2.startswith(c) for c in _CONJ_START):
        interp2 = raw2
    else:
        interp2 = f"반면 {raw2}"

    return interp1, interp2


def _build_issue_interpretation(fp: dict, issue_num: int) -> str:
    """
    ★ TASK-02: FACT PACK 기반 이슈별 해석 블록 1개를 구성한다.

    Returns:
        "▶ 이슈 N 해석: {문장1} {문장2}"
    """
    _CONJ_START = (
        "이", "그", "이는", "또한", "하지만", "따라서",
        "그러나", "특히", "우리", "현재", "이에", "이와",
    )

    # 문장 1: 리스크 또는 확정 사실 (원인 프레임)
    raw1 = (
        fp.get("risk")
        or (fp["confirmed"][0] if fp.get("confirmed") else "")
        or (fp["key_issues"][0] if fp.get("key_issues") else "")
    )
    if not raw1:
        s1 = "현재 경제 지표들은 복합적인 방향성을 보이고 있습니다."
    elif any(raw1.startswith(c) for c in _CONJ_START):
        s1 = raw1
    else:
        s1 = f"이는 {raw1}"

    # 문장 2: 기회 또는 추가 이슈 (결과 프레임)
    raw2 = (
        fp.get("opportunity")
        or (fp["confirmed"][1] if len(fp.get("confirmed", [])) > 1 else "")
        or (fp["key_issues"][1] if len(fp.get("key_issues", [])) > 1 else "")
        or (fp["key_issues"][0] if fp.get("key_issues") else "")
    )
    if not raw2 or raw2 == raw1:
        s2 = ""
    elif any(raw2.startswith(c) for c in _CONJ_START):
        s2 = raw2
    else:
        s2 = f"또한 {raw2}"

    body = f"{s1} {s2}".strip() if s2 else s1
    return f"▶이슈{issue_num} 해석: {body}"


def _build_implications_v2(fp2: dict) -> tuple:
    """
    ★ MODIFIED v2: FACT PACK 기반 개인·기업 시사점 2문장 구성.

    각 문장은 실행 가능한 내용이어야 하며, 줄임표("…") 절대 금지.
    확정 사실(confirmed) 우선, 부족하면 핵심 이슈(key_issues) 사용.
    """
    confirmed = fp2.get("confirmed", [])
    key_issues = fp2.get("key_issues", [])

    src1 = (confirmed[0] if confirmed else "") or (key_issues[0] if key_issues else "")
    src2 = (
        confirmed[1] if len(confirmed) > 1
        else (key_issues[1] if len(key_issues) > 1 else src1)
    )

    _PERSONAL_START = ("개인", "가계", "소비자", "근로자", "직장인", "나는", "우리는")
    _CORP_START = ("기업", "회사", "사업자", "경영자", "제조업")

    # 개인 시사점
    if not src1:
        personal = "개인은 지출 계획과 자산 배분을 재점검하고 경제 변화를 모니터링하기 바랍니다."
    elif any(src1.startswith(k) for k in _PERSONAL_START):
        personal = src1
    else:
        personal = f"개인은 {src1}"

    # 기업 시사점
    if not src2:
        corp = "기업은 시장 변화에 대비한 리스크 관리 체계와 비용 구조를 점검하기 바랍니다."
    elif any(src2.startswith(k) for k in _CORP_START):
        corp = src2
    else:
        corp = f"기업은 {src2}"

    return personal, corp


# ──────────────────────────────────────────────────────
# ★ MODIFIED v2: 자기검증(self-check) 함수
# ──────────────────────────────────────────────────────

def _validate_script(script: str) -> tuple:
    """
    ★ MODIFIED v2: 생성된 스크립트의 품질을 검증한다.

    검증 항목:
      1) 줄임표(… / ...) 없음
      2) 훅 섹션 존재 여부
      3) 핵심 이슈 ①②③ 모두 포함
      4) 해석 2줄 이상
      5) 시사점 2줄 이상

    Returns:
        (is_valid: bool, problems: list[str])
    """
    problems = []

    # 1) 줄임표 검사
    if "…" in script:
        problems.append("줄임표(…) 발견")
    if re.search(r"\.{3,}", script):
        problems.append("연속 마침표(...) 발견")

    # 섹션 파싱 (render_script_markdown은 모듈 하단에 정의되나,
    # Python은 호출 시점에 이름을 해석하므로 정상 동작함)
    sections = render_script_markdown(script)

    # 2) 훅 존재
    if not sections.get("hook", "").strip():
        problems.append("훅 섹션 비어 있음")

    # 3) 핵심 이슈 이슈1/2/3 모두 포함
    issues_text = sections.get("issues", "")
    for i in [1, 2, 3]:
        if f"이슈{i}:" not in issues_text:
            problems.append(f"핵심 이슈에 이슈{i} 없음")

    # 4) 해석 이슈별 블록 3개 포함
    interp_text = sections.get("interp", "")
    for i in [1, 2, 3]:
        if f"이슈{i} 해석" not in interp_text:
            problems.append(f"해석에 이슈{i} 블록 없음")
    interp_lines = [ln for ln in interp_text.split("\n") if ln.strip()]
    if len(interp_lines) < 3:
        problems.append(f"해석 블록 수 부족 ({len(interp_lines)}줄, 최소 3줄)")

    # 5) 시사점 최소 2줄
    impl_lines = [ln for ln in sections.get("impl", "").split("\n") if ln.strip()]
    if len(impl_lines) < 2:
        problems.append(f"시사점 문장 수 부족 ({len(impl_lines)}줄, 최소 2줄)")

    return (len(problems) == 0, problems)


def _strip_ellipses(script: str) -> str:
    """
    ★ MODIFIED v2: 스크립트에서 모든 줄임표를 제거하는 1회 수정 패스.
    자기검증 실패 시 호출된다.
    """
    result = script
    result = re.sub(r"\s*…+", "", result)      # "…" → 제거
    result = re.sub(r"\.{3,}", ".", result)     # "..." → "."
    result = re.sub(r"\.{2,}", ".", result)     # ".." → "."
    return result.strip()


# ──────────────────────────────────────────────────────
# 7. 60초 쇼츠 스크립트 생성 (★ MODIFIED v2: 2단계 생성)
# ──────────────────────────────────────────────────────
def generate_shorts_script(
    page_title: str,
    articles: list,
    summaries: list,
    hook_type: str = "auto",
    selected_hook: str = None,   # UI에서 직접 선택한 훅 텍스트 (None이면 자동)
    intensity: int = 3,           # 분석 강도 1(보수적)~5(공격적)
    texts: list = None,           # ★ MODIFIED v2: 원문 텍스트 리스트 (FACT PACK 생성용)
) -> str:
    """
    수집된 기사 요약(+ 선택적 원문)을 바탕으로 60초 유튜브 쇼츠 스크립트를 생성한다.

    ★ MODIFIED v2 — 2단계 생성 프로세스:

      Step 1  FACT PACK 구성
              texts(원문)가 제공되면 원문에서 직접 추출.
              미제공 시 summaries(요약)에서 FACT PACK 구성 (폴백).
              각 기사별 수치 사실·리스크·기회·확정/불확실 문장을 분리 저장.

      Step 2  FACT PACK 기반 스크립트 조립
              [0~5초 훅]       selected_hook 또는 자동 선택 (기존 로직 유지)
              [5~25초 이슈]    _build_issue_sentence: 수치 우선, 줄임표 금지
              [25~45초 해석]   _build_interpretation_v2: 원인→결과 2문장
              [45~60초 시사점] _build_implications_v2: 개인 1문장 + 기업 1문장

      Step 3  자기검증 패스
              줄임표·섹션 누락을 검사하고, 실패 시 _strip_ellipses로 1회 수정.

    구조 (섹션 형식 변경 없음 — render_script_markdown 호환):
      [0~5초 훅]           1문장
      [5~25초 핵심 이슈]    ①②③ 각 1문장
      [25~45초 해석]        2문장
      [45~60초 시사점]      2문장

    Args:
        page_title:    목록 페이지 제목
        articles:      기사 정보 리스트 [{"title": str, "url": str}, ...]
        summaries:     각 기사 요약 텍스트 리스트
        hook_type:     "auto" / "A" / "B" / "C"
        selected_hook: UI 선택 훅 텍스트 (None이면 자동)
        intensity:     분석 강도 1~5
        texts:         ★ v2 추가 — 기사 원문 텍스트 리스트 (FACT PACK 생성용)

    Returns:
        완성된 스크립트 문자열
    """

    def _c(t: str) -> str:
        return re.sub(r"\s+", " ", t).strip()

    _summaries = list(summaries)
    _articles = list(articles)
    _texts = list(texts) if texts else []

    # 3개 미만이면 패딩
    while len(_summaries) < 3:
        _summaries.append("관련 경제 동향을 주목해야 합니다.")
    while len(_articles) < 3:
        _articles.append({"title": "추가 경제 이슈", "url": ""})
    # texts가 summaries보다 짧으면 요약으로 보충
    while _texts and len(_texts) < 3:
        _texts.append(_summaries[len(_texts)])

    s0, s1, s2 = [_c(s) for s in _summaries[:3]]

    # ── 훅 결정 (selected_hook 우선, 없으면 자동 선택) ──────
    if selected_hook:
        hook = selected_hook
    else:
        if hook_type == "auto":
            hook_type = _pick_hook_type([s0, s1, s2])
        hook = _HOOKS.get(hook_type, _HOOKS["B"])
        num_m = re.search(r"\d+[\.,]?\d*\s*(%|원|달러|배)", s0)
        if num_m and hook_type == "C":
            hook = (
                f"{num_m.group()} 변화! "
                "지금 경제에 무슨 일이 있는지 60초로 알려드립니다."
            )

    # ── ★ Step 1: FACT PACK 구성 ───────────────────────────
    # 원문(texts)이 제공되면 원문에서, 없으면 요약에서 FACT PACK 생성
    if _texts:
        raw_sources = [
            (_texts[i], _articles[i]["title"]) for i in range(3)
        ]
        print("[summarizer] FACT PACK: 원문(texts) 기반 구성")
    else:
        raw_sources = [
            (s, _articles[i]["title"]) for i, s in enumerate([s0, s1, s2])
        ]
        print("[summarizer] FACT PACK: 요약(summaries) 기반 구성 (폴백)")

    fact_packs = [build_fact_pack(src, ttl) for src, ttl in raw_sources]
    fp0, fp1, fp2 = fact_packs

    # 수치 정보 보유 현황 로깅
    for i, fp in enumerate(fact_packs, 1):
        num_status = f"{len(fp['key_facts'])}개 수치 문장" if fp["has_numbers"] else "수치 없음"
        print(f"[summarizer] FACT PACK [{i}] {num_status} | 이슈:{len(fp['key_issues'])}개")

    # ── ★ Step 2: FACT PACK 기반 섹션 생성 ───────────────────
    issue1 = _build_issue_sentence(fp0, 0)
    issue2 = _build_issue_sentence(fp1, 1)
    issue3 = _build_issue_sentence(fp2, 2)

    # ★ TASK-02: 이슈별 해석 블록 3개 생성 (기존 2문장 혼합 → 이슈별 분리)
    interp_block1 = _build_issue_interpretation(fp0, 1)
    interp_block2 = _build_issue_interpretation(fp1, 2)
    interp_block3 = _build_issue_interpretation(fp2, 3)
    personal, corp = _build_implications_v2(fp2)

    # ── 강도(intensity) 어구 추가 ───────────────────────────
    intensity = max(1, min(5, int(intensity)))
    interp_note = _INTENSITY_INTERP_NOTE.get(intensity, "")
    impl_note = _INTENSITY_IMPL_NOTE.get(intensity, "")

    # ── 스크립트 조립 (섹션 형식 변경 없음) ─────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    intensity_label = ["", "보수적", "신중", "중립", "적극적", "공격적"][intensity]

    interp_block = f"{interp_block1}\n{interp_block2}\n{interp_block3}"
    if interp_note:
        interp_block += f"\n{interp_note}"

    impl_block = f"{personal}\n{corp}"
    if impl_note:
        impl_block += f"\n{impl_note}"

    # ── 참고 기사 목록 블록 생성 ──────────────────────────
    ref_lines = [f"\n{'-' * 40}", "※ 참고 기사 목록"]
    for i, art in enumerate(_articles[:3], 1):
        ref_lines.append(f"  [{i}] {art.get('title', '제목 없음')}")
        url = art.get("url", "")
        if url:
            ref_lines.append(f"      {url}")
    ref_block = "\n".join(ref_lines)

    script = (
        f"60초 경제신호 v2\n"
        f"페이지 제목: {page_title}\n"
        f"생성일시: {now}  |  분석강도: {intensity_label}({intensity}/5)\n"
        f"{'-' * 40}\n"
        f"\n"
        f"[0~5초 훅]\n"
        f"{hook}\n"
        f"\n"
        f"[5~25초]\n"
        f"{issue1}\n"
        f"{issue2}\n"
        f"{issue3}\n"
        f"\n"
        f"[25~45초]\n"
        f"{interp_block}\n"
        f"\n"
        f"[45~60초 개인/기업 시사점]\n"
        f"{impl_block}\n"
        f"{ref_block}"
    )

    # ── ★ Step 3: 자기검증 패스 ─────────────────────────────
    is_valid, problems = _validate_script(script)
    if not is_valid:
        print(f"[summarizer] WARN 검증 문제 {len(problems)}건: {problems}")
        script = _strip_ellipses(script)
        is_valid2, remaining = _validate_script(script)
        if is_valid2:
            print("[summarizer] OK  줄임표 수정 후 검증 통과")
        else:
            # 줄임표 외 구조 문제는 로깅만 (섹션 형식은 정상이므로 드문 케이스)
            print(f"[summarizer] WARN 수정 후 잔여 문제: {remaining}")
    else:
        print("[summarizer] OK  스크립트 검증 통과")

    return script


# ──────────────────────────────────────────────────────
# 8. 스크립트 섹션 파싱 (Streamlit 카드 렌더링용, 변경 없음)
# ──────────────────────────────────────────────────────
def render_script_markdown(script: str) -> dict:
    """
    스크립트 텍스트를 섹션 레이블 기준으로 파싱하여 dict로 반환한다.
    Streamlit app.py에서 st.container(border=True) 카드 렌더링에 사용.

    Returns:
        {
            "header": str,  # 제목·생성일시 메타 정보
            "hook":   str,  # [0~5초 훅] 내용
            "issues": str,  # [5~25초 핵심 이슈] 내용
            "interp": str,  # [25~45초 해석] 내용
            "impl":   str,  # [45~60초 시사점] 내용
        }
    """
    sections = {
        "header": "",
        "hook": "",
        "issues": "",
        "interp": "",
        "impl": "",
    }

    current = "header"
    buckets = {k: [] for k in sections}

    for line in script.split("\n"):
        # 구분선은 건너뜀
        if line.startswith("---") or set(line.strip()) <= {"-", "─"}:
            continue
        if "[0~5초" in line:
            current = "hook"
            continue
        elif "[5~25초" in line:
            current = "issues"
            continue
        elif "[25~45초" in line:
            current = "interp"
            continue
        elif "[45~60초" in line:
            current = "impl"
            continue

        buckets[current].append(line)

    for key, lines in buckets.items():
        sections[key] = "\n".join(ln for ln in lines if ln.strip())

    return sections


# ──────────────────────────────────────────────────────
# Enhanced Summarize — 심층 4-Frame 분석 래퍼 (Phase 13)
# ──────────────────────────────────────────────────────

def enhanced_summarize(
    text: str,
    title: str = "",
    doc_id: str = "",
    industry_key: str = "일반",
) -> dict:
    """기존 3줄 요약 + 심층 4-Frame 분석을 통합 반환.

    Returns
    -------
    dict
        {
            "summary": dict | str,   # summarize_3line 결과
            "summary_source": str,   # "groq" | "rule" | "cache"
            "deep": dict,            # DeepAnalysis.to_dict()
        }
    """
    # 1) 기존 3줄 요약
    summary, source = summarize_3line(text, title, industry_key)

    # 2) 심층 분석 (import 지연으로 순환 참조 방지)
    try:
        from core.llm_analyzer import analyze_article_deep
        deep = analyze_article_deep(
            text=text,
            title=title,
            doc_id=doc_id,
            industry_key=industry_key,
        )
        deep_dict = deep.to_dict()
    except Exception:
        deep_dict = {
            "impact": "", "risk": "", "opportunity": "", "action": "",
            "confidence": 0.0, "source": "error",
        }

    return {
        "summary": summary,
        "summary_source": source,
        "deep": deep_dict,
    }


# ════════════════════════════════════════════════════════════════════════════
# Article Intelligence — summarize_executive / generate_comparison_summary
# ════════════════════════════════════════════════════════════════════════════

def summarize_executive(
    text: str,
    title: str,
    macro_data: dict | None = None,
    industry_key: str = "일반",
) -> dict:
    """
    경영진용 요약.
    Returns: {
        "headline": str (title 기반 20자 이내),
        "body": str (summarize_rule_based 호출),
        "recommendation": str (리스크/기회 키워드 기반),
        "urgency": "high"|"medium"|"low" (긴급 키워드 수 기반),
        "relevance_score": 0.0-1.0 (경제 키워드 매칭 비율)
    }
    """
    from core.macro_utils import _RISK_KW, _OPP_KW, _ECON_KW

    # headline: title 20자 이내로 잘라냄
    headline = (title[:20] if len(title) > 20 else title) if title else "제목 없음"

    # body
    body = summarize_rule_based(text, title, industry_key=industry_key)

    # recommendation
    combined = (title or "") + " " + (text or "")
    risk_count = sum(1 for kw in _RISK_KW if kw in combined)
    opp_count = sum(1 for kw in _OPP_KW if kw in combined)

    if risk_count > opp_count:
        recommendation = "리스크 대응 필요"
    elif opp_count > risk_count:
        recommendation = "기회 활용 검토"
    else:
        recommendation = "모니터링 지속"

    # urgency
    _URGENT_KW = ["즉시", "긴급", "시행", "발효", "폐지", "당장", "비상", "위기"]
    urgent_count = sum(1 for kw in _URGENT_KW if kw in combined)
    if urgent_count >= 3:
        urgency = "high"
    elif urgent_count >= 1:
        urgency = "medium"
    else:
        urgency = "low"

    # relevance_score
    if _ECON_KW:
        matched = sum(1 for kw in _ECON_KW if kw in combined)
        relevance_score = round(min(1.0, matched / len(_ECON_KW)), 2)
    else:
        relevance_score = 0.0

    return {
        "headline": headline,
        "body": body,
        "recommendation": recommendation,
        "urgency": urgency,
        "relevance_score": relevance_score,
    }


def generate_comparison_summary(
    articles: list[dict], industry_key: str = "일반"
) -> str:
    """2-5개 기사 비교 요약. 공통 주제 + 차이점. 빈 리스트면 빈 문자열."""
    if not articles:
        return ""

    from core.macro_utils import _ECON_KW

    # 각 기사에서 경제 키워드 추출
    article_keywords: list[set] = []
    titles: list[str] = []
    for art in articles:
        title = art.get("title", "")
        body = art.get("body", "") or art.get("body_text", "")
        combined = title + " " + body
        titles.append(title)
        kws = {kw for kw in _ECON_KW if kw in combined}
        article_keywords.append(kws)

    # 공통 키워드
    if article_keywords:
        common = article_keywords[0]
        for kws in article_keywords[1:]:
            common = common & kws
    else:
        common = set()

    # 각 기사의 고유 키워드
    unique_per_article: list[set] = []
    for kws in article_keywords:
        unique_per_article.append(kws - common)

    # 비교 요약 생성
    parts: list[str] = []
    if common:
        parts.append(f"공통 주제: {', '.join(sorted(common))}")
    else:
        parts.append("공통 주제: 명확한 공통 주제 없음")

    for i, (title, unique) in enumerate(zip(titles, unique_per_article)):
        short_title = title[:30] if len(title) > 30 else title
        if unique:
            parts.append(f"기사{i+1}({short_title}): {', '.join(sorted(unique))}")
        else:
            parts.append(f"기사{i+1}({short_title}): 고유 키워드 없음")

    return " | ".join(parts)
