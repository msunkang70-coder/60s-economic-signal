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


def _clip_sent(s: str, n: int = 75) -> str:
    """문장을 n자 이내로 자른다. 줄임표(…) 금지 — 단어 경계에서 자름."""
    if len(s) <= n:
        return s
    # 단어 경계(공백) 기준으로 n자 이내 최대 길이
    cut = s[:n]
    last_space = cut.rfind(" ")
    return cut[:last_space] if last_space > n // 2 else cut


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
    전체 본문 맥락을 분석하는 구조화 3줄 요약 생성기 (v3).

    개선사항:
        1) 지배 주제어 분석 — 문서 전체에서 핵심 명사 빈도 추출
        2) 주제 일관성 필터 — dominant topic 포함 문장만 우선 탐색
        3) 논리 흐름 구조화 — WHAT → HOW → SO WHAT 역할 분리
        4) 위치 + 역할 + 주제 3중 점수로 최적 문장 선택

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
당신은 정책 브리핑 전문 에디터입니다.
아래 기사 본문을 읽고 정확히 3줄로 요약하세요.

[출력 형식 — 반드시 이 형식만 사용]
① [핵심 정책] (해당 정책 또는 이슈의 목적과 핵심 방향 1문장)
② [주요 내용] (정책 실행 방식, 주요 조치, 핵심 수치 등 1문장)
③ [영향·시사점] (글로벌 시장·산업·국가 전략에 미칠 영향 또는 의미 1문장)

[작성 규칙]
- 반드시 본문 전체 맥락을 기반으로 작성
- 서로 다른 주제 혼합 금지 — 정책의 중심 흐름을 기준으로
- 각 줄은 1~2문장 이내, 설명형 문장 (단순 키워드 나열 금지)
- 줄임표(…) 사용 금지
- 정책 분석 보고서 수준의 자연스러운 한국어 문장
- ①②③ 기호와 [레이블]은 반드시 포함
{industry_context}
[기사 제목]
{title}

[기사 본문]
{body}

요약:"""


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


def _summarize_with_llm(text: str, title: str = "", industry_key: str = "일반") -> str | None:
    """
    Groq (Llama 3.3 70B) API 호출로 3줄 요약 생성.

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
    prompt = _LLM_PROMPT.format(title=title or "", body=body_trunc, industry_context=industry_context)

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "llama-3.3-70b-versatile",
                "messages":    [{"role": "user", "content": prompt}],
                "max_tokens":  512,
                "temperature": 0.3,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[summarizer] Groq API 오류 {resp.status_code}: {resp.text[:200]}")
            return None

        raw_out = resp.json()["choices"][0]["message"]["content"].strip()
        return _validate_output(raw_out)

    except Exception as e:
        print(f"[summarizer] Groq 호출 실패: {e}")
        return None


def summarize_3line(
    text: str,
    title: str = "",
    industry_key: str = "일반",
) -> tuple[str, str]:
    """
    정책 브리핑용 표준 3줄 요약 생성 (v3 공개 인터페이스).

    반환: (summary_text, source)
      source = "groq" | "rule"

    우선순위:
      1) GROQ_API_KEY 있으면 Llama 3.3 70B (무료)로 고품질 LLM 요약
      2) 키 없거나 호출 실패 시 규칙 기반 폴백
    """
    llm_result = _summarize_with_llm(text, title, industry_key=industry_key)
    if llm_result:
        print(f"[summarizer] [OK] Groq 요약 성공 ({len(llm_result)}자)")
        return llm_result, "groq"

    print(f"[summarizer] 규칙 기반 폴백 (Groq 키: {'있음' if _get_llm_key() else '없음'})")
    return summarize_rule_based(text, title, max_sentences=3, industry_key=industry_key), "rule"


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
      - 문서 앞 30% 위치: +3.0
      - 숫자·단위 포함:   +2.0
      - 적정 문장 길이(30~100자): +1.0
      - 제목 단어 overlap: +1.5 (단어당)
      - 산업 키워드 매칭: +2.5 (키워드당)

    ★ v3: 주제 일관성 필터 + 개선된 _structured_3line 사용.
    """
    if not text:
        return title or "본문 없음"

    raw_sents = re.split(r"(?<=[.?!다요])\s+|\n", text)
    sentences = [s.strip() for s in raw_sents if 15 <= len(s.strip()) <= 200]

    if not sentences:
        return text[:150]   # ★ MODIFIED v2: "..." 제거

    # 산업 키워드 로드
    _ind_kws: list[str] = []
    if industry_key and industry_key != "일반":
        try:
            from core.industry_config import get_profile
            _ind_kws = get_profile(industry_key).get("keywords", [])
        except ImportError:
            pass

    total = len(sentences)
    title_words = set(re.findall(r"[가-힣]{2,}", title)) if title else set()

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

        scored.append((score, idx, sent))

    # 점수 내림차순 → 원래 순서(idx) 오름차순으로 최종 정렬
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = sorted(scored[:max_sentences], key=lambda x: x[1])
    extracted = [s for _, _, s in top]

    # ── 구조화 3줄 요약: max_sentences >= 3이면 항상 적용 ─
    # 이전: len(extracted) >= 2 조건으로 본문이 짧으면 건너뜀 → 형식 불일치 발생
    # 수정: sentences가 1개 이상이면 무조건 구조화 포맷 적용
    if max_sentences >= 3 and sentences:
        return _structured_3line(sentences, extracted, title)

    # 2줄 이하 요청 시에만 단순 join
    summary = " ".join(extracted)
    return summary if summary else text[:150]


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
        personal_raw = s2_parts[0] if s2_parts else s2[:70]
        corp_raw = s2_parts[0] if s2_parts else s2[:70]

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
    """
    raw = re.split(r"(?<=[.?!다요])\s+|\n", text)
    return [s.strip() for s in raw if 15 <= len(s.strip()) <= 250]


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
            "key_facts": [text[:150]],
            "key_issues": [text[:100]],
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

    우선순위:
      1) key_facts  수치 포함 완전 문장
      2) key_issues 경제 키워드 고득점 완전 문장
      3) "수치 정보는 기사에 제한적입니다" 대체 문장

    규칙: 줄임표("…") 절대 금지. 완전한 문장만 반환.
    """
    marker = ["①", "②", "③"][index]

    # 1순위: 수치 포함 핵심 사실
    if fact_pack.get("key_facts"):
        return f"{marker} {fact_pack['key_facts'][0]}"

    # 2순위: 경제 고득점 이슈
    if fact_pack.get("key_issues"):
        return f"{marker} {fact_pack['key_issues'][0]}"

    # 폴백: 수치 없음 명시 (수치 정보 없다고 명확히 고지)
    title_hint = (fact_pack.get("title") or "해당 기사")[:20]
    return f"{marker} [{title_hint}] 수치 정보는 기사에 제한적입니다."


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

    # 3) 핵심 이슈 ①②③ 모두 포함
    issues_text = sections.get("issues", "")
    for marker in ["①", "②", "③"]:
        if marker not in issues_text:
            problems.append(f"핵심 이슈에 {marker} 없음")

    # 4) 해석 최소 2줄
    interp_lines = [ln for ln in sections.get("interp", "").split("\n") if ln.strip()]
    if len(interp_lines) < 2:
        problems.append(f"해석 문장 수 부족 ({len(interp_lines)}줄, 최소 2줄)")

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

    interp1, interp2 = _build_interpretation_v2(fp0, fp1)
    personal, corp = _build_implications_v2(fp2)

    # ── 강도(intensity) 어구 추가 ───────────────────────────
    intensity = max(1, min(5, int(intensity)))
    interp_note = _INTENSITY_INTERP_NOTE.get(intensity, "")
    impl_note = _INTENSITY_IMPL_NOTE.get(intensity, "")

    # ── 스크립트 조립 (섹션 형식 변경 없음) ─────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    intensity_label = ["", "보수적", "신중", "중립", "적극적", "공격적"][intensity]

    interp_block = f"{interp1}\n{interp2}"
    if interp_note:
        interp_block += f"\n{interp_note}"

    impl_block = f"{personal}\n{corp}"
    if impl_note:
        impl_block += f"\n{impl_note}"

    script = (
        f"60초 경제신호 v2\n"          # ★ MODIFIED v2: 버전 표기
        f"페이지 제목: {page_title}\n"
        f"생성일시: {now}  |  분석강도: {intensity_label}({intensity}/5)\n"
        f"{'-' * 40}\n"
        f"\n"
        f"[0~5초 훅]\n"
        f"{hook}\n"
        f"\n"
        f"[5~25초 핵심 이슈 3개]\n"
        f"{issue1}\n"
        f"{issue2}\n"
        f"{issue3}\n"
        f"\n"
        f"[25~45초 해석]\n"
        f"{interp_block}\n"
        f"\n"
        f"[45~60초 개인/기업 시사점]\n"
        f"{impl_block}"
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
        if "[0~5초 훅]" in line:
            current = "hook"
            continue
        elif "[5~25초 핵심 이슈" in line:
            current = "issues"
            continue
        elif "[25~45초 해석]" in line:
            current = "interp"
            continue
        elif "[45~60초" in line:
            current = "impl"
            continue

        buckets[current].append(line)

    for key, lines in buckets.items():
        sections[key] = "\n".join(ln for ln in lines if ln.strip())

    return sections
