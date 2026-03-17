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

★ v11 — LLM 분석 품질 100% 보장 체계 (Phase 7 #2):
  1) 멀티모델 폴백: llama-3.3-70b → llama-3.1-8b-instant (실패 시 경량 모델 재시도)
  2) 품질 스코어링: 0-100점 기반 다면 평가 (산업키워드, 인과관계, bullet 수, 문장수)
  3) 타겟 재시도: 품질 미달 항목별 구체적 힌트 제공 후 재시도 (최대 2회)
  4) 품질 메트릭: 세션별 groq 성공률, 평균 품질점수, 폴백 비율 추적
  5) 강화된 검증: _validate_summary_quality_v2() — 산업 분화 준수율 점검
"""

import json
import logging
import os
import re
from datetime import datetime

import queue
import requests
import time
import threading

from core.utils import single_line

_log = logging.getLogger(__name__)

# ── V11: 멀티모델 폴백 체인 (Phase 7 #2) ──────────────────
_LLM_MODELS = [
    {
        "name": "llama-3.3-70b-versatile",
        "model_id": "llama-3.3-70b-versatile",
        "temperature": 0.45,
        "max_tokens": 4096,
        "timeout": 20,
        "label": "Llama3.3-70B",
    },
    {
        "name": "llama-3.1-8b-instant",
        "model_id": "llama-3.1-8b-instant",
        "temperature": 0.3,  # 경량 모델은 더 보수적으로
        "max_tokens": 4096,
        "timeout": 15,
        "label": "Llama3.1-8B",
    },
]

# ── V11: 품질 메트릭 추적 (세션 단위) ─────────────────────
_quality_metrics = {
    "total_calls": 0,
    "groq_success": 0,
    "groq_retry_success": 0,
    "fallback_model_success": 0,
    "smart_fallback": 0,
    "quality_scores": [],        # 각 LLM 결과의 품질 점수 목록
    "industry_scores": {},       # 산업별 평균 품질
    "retry_reasons": [],         # 재시도 사유 목록
}
_quality_metrics_lock = threading.Lock()


def _record_quality_metric(source: str, score: int = 0, industry: str = "일반", retry_reason: str = ""):
    """V11: 품질 메트릭 기록."""
    with _quality_metrics_lock:
        _quality_metrics["total_calls"] += 1
        if source == "groq":
            _quality_metrics["groq_success"] += 1
        elif source == "groq_retry":
            _quality_metrics["groq_retry_success"] += 1
        elif source == "fallback_model":
            _quality_metrics["fallback_model_success"] += 1
        elif source in ("smart_fallback", "body_short", "industry_fallback"):
            _quality_metrics["smart_fallback"] += 1
        if score > 0:
            _quality_metrics["quality_scores"].append(score)
            if industry not in _quality_metrics["industry_scores"]:
                _quality_metrics["industry_scores"][industry] = []
            _quality_metrics["industry_scores"][industry].append(score)
        if retry_reason:
            _quality_metrics["retry_reasons"].append(retry_reason)


def get_quality_metrics() -> dict:
    """V11: 현재 세션 품질 메트릭 반환."""
    with _quality_metrics_lock:
        m = _quality_metrics.copy()
        total = m["total_calls"] or 1
        scores = m["quality_scores"]
        m["groq_rate"] = round((m["groq_success"] + m["groq_retry_success"] + m["fallback_model_success"]) / total * 100, 1)
        m["fallback_rate"] = round(m["smart_fallback"] / total * 100, 1)
        m["avg_quality"] = round(sum(scores) / len(scores), 1) if scores else 0
        # 산업별 평균
        m["industry_avg"] = {
            k: round(sum(v) / len(v), 1) for k, v in m["industry_scores"].items() if v
        }
        return m


# V4: Queue 기반 순차 LLM 처리 (동시 요청 방지)
_llm_request_queue = queue.Queue()
_llm_worker_running = False
_llm_worker_lock = threading.Lock()

# Groq API rate limit 관리 (30 RPM → 최소 2초 간격)
_last_groq_call_time = 0.0
_groq_call_lock = threading.Lock()

# V7: 서킷 브레이커 — 연속 실패 시 LLM 호출 일시 중단
_groq_consecutive_fails = 0
_groq_circuit_open_until = 0.0  # epoch time — 이 시점까지 LLM 호출 건너뜀
_GROQ_MAX_CONSECUTIVE_FAILS = 5   # 연속 5회 실패 시 서킷 오픈 (v8.1: 3→5)
_GROQ_COOLDOWN_SECONDS = 60       # 1분간 LLM 호출 건너뜀 (v8.1: 120→60)
_groq_circuit_lock = threading.Lock()

# ── V17: LLM 호출 최적화 상수 ────────────────────────────
_LLM_MAX_ARTICLES = 3        # 세션당 LLM 호출 최대 기사 수 (Top 3만 LLM)
_LLM_MIN_BODY_LENGTH = 400   # LLM 호출 최소 본문 길이 (미달 시 smart_fallback으로 직행)

# ── V17: 세션 단위 LLM 사용량 추적 ────────────────────────
_llm_session_state: dict = {
    "llm_calls": 0,       # 실제 LLM API 호출 수
    "cache_hits": 0,      # 캐시 히트 수
    "fallback_skips": 0,  # body_short/top_limit/circuit_breaker 이유로 LLM 스킵된 수
}
_llm_session_lock = threading.Lock()


def reset_llm_session() -> None:
    """V17: 탭/세션 시작 시 LLM 카운터 초기화."""
    with _llm_session_lock:
        _llm_session_state["llm_calls"] = 0
        _llm_session_state["cache_hits"] = 0
        _llm_session_state["fallback_skips"] = 0


def get_llm_session_stats() -> dict:
    """V17: 현재 세션 LLM 사용량 통계 반환."""
    with _llm_session_lock:
        stats = _llm_session_state.copy()
    total = stats["llm_calls"] + stats["cache_hits"] + stats["fallback_skips"]
    saved = stats["cache_hits"] + stats["fallback_skips"]
    pct = round(saved / total * 100) if total else 0
    return {**stats, "total": total, "llm_saving_pct": pct}


def log_llm_session_summary() -> None:
    """V17: 세션 종료 시 LLM 사용량 요약 출력."""
    s = get_llm_session_stats()
    print(
        f"[summarizer] LLM 호출: {s['llm_calls']}건 / "
        f"캐시 사용: {s['cache_hits']}건 / "
        f"LLM 절감: {s['llm_saving_pct']}%"
    )


def _groq_circuit_is_open() -> bool:
    """서킷 브레이커가 열려 있으면 True (LLM 호출 건너뛰기)."""
    with _groq_circuit_lock:
        if _groq_consecutive_fails >= _GROQ_MAX_CONSECUTIVE_FAILS:
            if time.time() < _groq_circuit_open_until:
                return True
            # 쿨다운 경과 → 서킷 반닫힘 (1회 시도 허용)
            return False
        return False


def _groq_record_success():
    """LLM 호출 성공 → 서킷 브레이커 리셋."""
    global _groq_consecutive_fails, _groq_circuit_open_until
    with _groq_circuit_lock:
        _groq_consecutive_fails = 0
        _groq_circuit_open_until = 0.0


def _groq_record_failure():
    """LLM 호출 실패 → 연속 실패 카운트 증가, 임계치 초과 시 서킷 오픈."""
    global _groq_consecutive_fails, _groq_circuit_open_until
    with _groq_circuit_lock:
        _groq_consecutive_fails += 1
        if _groq_consecutive_fails >= _GROQ_MAX_CONSECUTIVE_FAILS:
            _groq_circuit_open_until = time.time() + _GROQ_COOLDOWN_SECONDS
            print(
                f"[summarizer] ⚡ 서킷 브레이커 OPEN — 연속 {_groq_consecutive_fails}회 실패, "
                f"{_GROQ_COOLDOWN_SECONDS}초간 LLM 건너뜀"
            )


# ──────────────────────────────────────────────────────
# 시스템 프롬프트 (산업 특화 LLM 브리핑용)
# ──────────────────────────────────────────────────────
SYSTEM_PROMPT_TEMPLATE = """
당신은 한국 {industry_label} 수출기업 CEO를 위한 경제 전략 브리핑 전문가입니다.
{industry_context}

아래 경제 기사를 분석하여, {industry_label} 수출기업 CEO가 즉시 의사결정에 활용할 수 있는 전략 브리핑을 작성하세요.

## 출력 형식 (반드시 JSON)

{{"impact": "...", "risk": "...", "opportunity": "...", "action": "...", "questions": "...", "checklist": "..."}}

## 각 필드 작성 규칙

📊 Impact (영향) — 2~3문장, 100~200자
- 이 기사의 핵심 변화가 {industry_label} 수출기업에 미치는 **직접적 영향**을 서술
- 반드시 포함: ①변화의 방향(확대/축소/강화/완화) ②영향 받는 구체적 영역 ③시간적 범위
- 핵심 표현은 **볼드 마크다운**으로 표시
- ⚡ P4 필수 규칙: Impact 첫 문장에 기사에서 추출한 【기업명·브랜드명 / 구체적 수치(억원·%·개사 등) / 시장·국가명 / 핵심 이벤트(ODM·박람회·팝업·MOU 등)】 중 반드시 2개 이상 포함하여 이 기사만의 고유한 내용을 담으세요

📉 Risk (리스크) — 2~3문장, 100~200자
- {industry_label} 산업 특성에 기반한 **구체적 위험 시나리오** 서술
- "A하면 B가 발생하여 C에 영향" 형식의 인과관계 서술
- Impact와 완전히 다른 내용이어야 함

💡 Opportunity (기회) — 2~3문장, 100~200자
- {industry_label} 기업이 이 변화를 통해 얻을 수 있는 **구체적 기회** 서술
- 구체적 행동과 그 결과를 연결
- Risk와 완전히 다른 시각이어야 함

✅ Action (즉시 행동) — bullet point 3개, 각 15~30자
- "• " 로 시작하는 3개의 **구체적 실행 항목** 나열 (\\n으로 구분)
- 각 항목은 "동사 + 대상 + 범위" 형식
- 추상적 표현 금지, 구체적 점검 대상을 명시

❓ Questions (경영진 질문) — 2~3개, 각 20~40자
- "• " 로 시작하는 핵심 질문 2~3개 (\n으로 구분)
- CEO가 이 기사를 읽고 임원회의에서 물어볼 질문
- "우리 {industry_label}에 미치는 영향은?" 수준의 구체성

📋 Checklist (점검 항목) — 3개, 각 15~25자
- "• " 로 시작하는 점검 항목 3개 (\n으로 구분)
- 이번 주 내 확인해야 할 구체적 사항

## ⚡ 짧은 기사 필드 확장 규칙 (V17.4-patch 필수 적용)
- 기사 본문이 짧더라도 Impact / Risk / Opportunity 각 필드는 반드시 80자 이상 작성하세요.
- 기사 내용이 부족하면 해당 산업 CEO 관점에서 논리적 비즈니스 함의를 확장·해석하여 채우세요.
- 수치나 사실이 기사에 없어도 산업 상황에 맞는 경영 해석 문장을 추가하세요.
- 예) "기사에 수치가 없더라도 이 규제가 실행되면 수출원가 구조에 미칠 영향을 추론하여 서술"
- IMPORTANT: If the article body is short, you must still expand each field with business interpretation. Impact / Risk / Opportunity must be at least 80 characters each. You may infer logical business implications even if the article itself is brief.

## 절대 금지 사항
- 원문 문장을 그대로 인용하지 마세요. 모든 문장은 {industry_label} 관점에서 재해석·재구성해야 합니다
- 4개 필드의 내용이 서로 중복되면 안 됩니다
- "영향이 있다", "주의가 필요하다" 같은 의미 없는 문장 금지
- 줄임표(…) 사용 금지
- 기사 본문에 없는 수치를 임의로 만들지 마세요

## ⛔ V15 Hallucination 방지 규칙 (최우선 적용)
1. **기사 원문에 없는 사실을 절대 생성하지 마라.** 모든 서술은 아래 제공된 기사 본문에서 직접 근거를 확인할 수 있어야 한다.
2. **Risk와 Opportunity는 기사 내용에서 직접 근거가 있어야 한다.** "글로벌 신뢰도", "투명 경영 프리미엄", "환율 변동 리스크" 등 기사와 무관한 generic 표현은 절대 금지한다.
3. **기사에 해당 정보가 없다면 해당 항목에 "원문에서 직접 확인 필요" 라고 작성한다.** 추측이나 일반론으로 채우지 않는다.
4. **일반적인 경제 설명이나 템플릿 문장은 금지한다.** "경쟁사 대비", "선제적 대응", "기회 포착" 같은 빈 말 금지.
5. **각 항목은 기사를 읽지 않은 CEO도 이 카드만으로 '무슨 사건이 발생했는지' 알 수 있어야 한다.**

## 🔤 V16 언어 규칙 (필수)
- **모든 출력은 반드시 한국어로 작성한다.** 영어·중국어·일본어 등 비한국어 단어가 혼입되면 안 된다.
- 예외: 고유명사(브랜드명, 기업명), 수치 단위(%, $, bbl, TEU 등), 공식 약어(GDP, WTI, LNG 등)는 원어 표기 허용.
- 한자 및 중국어 간체·번체 표현(예: 实施, 扩大, 加速) 절대 금지.
- 영문 단어가 한글과 동의어가 있으면 반드시 한글을 우선 사용 (예: "시행" ✅ "実施" ❌, "확대" ✅ "expand" ❌).

## ⚠️ 산업 분화 강제 규칙 (최우선)
- Impact 필드 첫 문장에 반드시 "{industry_label}" 산업 고유 키워드를 1개 이상 포함하세요
- "원자재", "비용", "모니터링" 같은 범용 단어만으로 구성된 문장은 절대 금지합니다
- Action 3개 bullet 중 최소 2개는 {industry_label} 산업에서만 사용하는 전문 용어를 포함해야 합니다
- Risk는 반드시 "A하면 → B가 발생하여 → {industry_label} 기업의 C에 영향" 인과관계 형식으로 작성하세요
- Opportunity는 {industry_label} 기업만이 활용할 수 있는 차별화된 기회를 서술하세요
- questions 필드의 모든 질문은 {industry_label} CEO가 임원회의에서 실제로 물어볼 구체적 질문이어야 합니다
- checklist 필드의 모든 항목은 {industry_label} 산업 실무자가 이번 주에 실행 가능한 구체적 점검사항이어야 합니다
- ⚡ P4 기사 고유성 규칙: Impact 생성 시 기사에서 등장하는 【기업명·브랜드명 / 수치(억원·%·개사 등) / 시장·국가명 / 이벤트(ODM·박람회·팝업·MOU·수출상담 등)】 중 최소 2개를 Impact 첫 문장에 명시적으로 포함해 이 기사만의 고유한 분석을 작성하세요. 해당 요소가 기사에 없으면 기사 제목의 핵심 키워드를 2개 이상 포함하세요

## 산업별 분석 관점 가이드
{industry_label} 기업 CEO가 이 기사를 읽고 가장 먼저 궁금해할 것:
- 이것이 우리 {industry_label} 수출에 어떤 의미인가?
- 경쟁사 대비 우리가 취해야 할 차별화된 행동은?
- 단기(1~3개월) vs 중기(6개월~1년) 영향은 어떻게 다른가?
"""

# 하위 호환: 기존 코드에서 SYSTEM_PROMPT를 직접 참조하는 경우
SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE


def _resolve_industry_label(industry_key: str) -> str:
    """industry_key에서 사람이 읽을 수 있는 산업 레이블을 반환."""
    if not industry_key or industry_key == "일반":
        return "일반 수출"
    try:
        from core.industry_config import get_profile
        return get_profile(industry_key).get("label", "일반 수출")
    except ImportError:
        return "일반 수출"


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

_LLM_PROMPT = """
[기사 제목]: {title}

[기사 본문 (요약 대상)]:
{body}

{industry_context}

⚠️ 중요 지시사항 (반드시 모두 준수):
1. 반드시 위 "기사 제목"의 주제에 대해서만 분석하세요
2. 본문 문장을 그대로 옮기지 말고, 산업 관점에서 재해석하여 작성하세요
3. 6개 필드(impact/risk/opportunity/action/questions/checklist)는 모두 서로 다른 관점이어야 합니다
4. Action은 반드시 "• " 로 시작하는 3개 bullet point로 작성하세요
5. 각 필드는 최소 80자 이상, 2~3문장으로 작성하세요
6. 핵심 키워드에 **볼드** 마크다운을 적용하세요
7. questions 필드: CEO가 임원회의에서 물어볼 핵심 질문 2~3개를 "• " bullet로 작성하세요
8. checklist 필드: 이번 주 내 확인해야 할 구체적 점검 항목 3개를 "• " bullet로 작성하세요

🚨 산업 분화 필수 규칙 (위반 시 출력 무효):
9. Impact 첫 문장에 반드시 [산업 맞춤 분석 관점]에 명시된 산업 키워드를 1개 이상 포함하세요
10. Action bullet 3개 중 2개 이상은 해당 산업 전문 용어(위 키워드 참조)를 포함해야 합니다
11. "원자재 확인", "비용 점검", "동향 모니터링" 같은 어떤 산업에도 해당하는 범용 문장은 금지합니다
12. questions는 해당 산업 CEO만 물어볼 수 있는 구체적 질문으로 작성하세요 (예: "HBM 수주 잔고 변동은?")
"""


# ──────────────────────────────────────────────────────
# LLM 제공자 헬퍼 함수
# ──────────────────────────────────────────────────────

def _rate_limit_wait():
    """Groq API rate limit 회피: 최소 2초 간격 유지."""
    global _last_groq_call_time
    with _groq_call_lock:
        now = time.time()
        elapsed = now - _last_groq_call_time
        if elapsed < 2.0:
            wait_time = 2.0 - elapsed
            print(f"[summarizer] 🕐 Rate limit 대기: {wait_time:.1f}초")
            time.sleep(wait_time)
        _last_groq_call_time = time.time()


def _get_llm_key() -> str:
    """
    GROQ_API_KEY 환경변수 반환 (없으면 st.secrets 시도).
    키 발급: https://console.groq.com (무료, 카드 불필요)
    """
    try:
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if key:
            print("[summarizer] 🔑 API 키: 환경변수 (설정 완료)")
            return key
        import streamlit as st
        groq_section = st.secrets.get("groq") or {}
        key = groq_section.get("api_key", "").strip()
        if key:
            print("[summarizer] 🔑 API 키: st.secrets (설정 완료)")
        else:
            print("[summarizer] ⚠️ API 키 없음 — st.secrets['groq']['api_key'] 비어있음")
        return key
    except Exception as e:
        print(f"[summarizer] ❌ API 키 로드 실패: {type(e).__name__}: {e}")
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
        return (
            "[산업 맞춤 분석 관점]\n"
            "- 분석 관점: 반도체·배터리·자동차·조선·화학 등 특정 산업에 치우치지 않은 **범용 수출기업 CEO** 브리핑입니다\n"
            "- 중요 경제 변수: 환율(원달러), 수출증가율, 물가, 금리, 관세율\n"
            "- 해석 관점: 수출 경쟁력, 원가 구조, 시장 접근성, 수출 금융, 글로벌 공급망\n"
            "- Impact: 이 기사의 구체적 사건·수치·정책이 수출기업 매출·원가·시장접근성에 미치는 영향을 서술하세요. 기사 내용을 직접 반영하세요.\n"
            "- Risk: 기사에서 언급된 구체적 위험 요인(규제·관세·공급망 이슈 등)을 중심으로 작성하세요. '환율 변동·무역 장벽' 등 고정 문구 반복 금지.\n"
            "- Opportunity: 기사에서 언급된 구체적 기회 요인(시장·정책·파트너십 등)을 중심으로 작성하세요. '글로벌 신뢰도·투명 경영' 등 고정 문구 반복 금지.\n"
            "- Impact/Risk/Opportunity 모두 수출기업 경영진 관점에서 작성하세요\n"
            "- ⚠️ V16 산업혼재 금지: Action 항목에 반도체·배터리·조선·자동차 등 **특정 산업 고유 전문 용어**를 사용하지 마세요. 일반 수출기업이 바로 실행 가능한 수출 실무 행동을 작성하세요.\n"
            "- ✅ V16 일반수출 Action 예시: L/C(신용장) 조건 재검토, 선물환 계약 비율 점검, 수출보험(KSURE) 가입 한도 확인, 바이어 네트워크 다변화, 관세 환급 절차 확인, HS코드별 관세율 모니터링\n"
        )
    try:
        from core.industry_config import get_profile
        profile = get_profile(industry_key)
    except ImportError:
        return ""
    label = profile.get("label", industry_key)
    crit_vars = ", ".join(profile.get("critical_variables", []))
    keywords = ", ".join(profile.get("keywords", [])[:8])
    templates = profile.get("strategy_templates", [])
    template_text = "\n".join(f"  - {t}" for t in templates[:3]) if templates else ""

    context = (
        f"\n[산업 맞춤 분석 관점]\n"
        f"- 분석 대상: {label} 수출기업 CEO를 위한 브리핑입니다\n"
        f"- 핵심 경제 변수: {crit_vars}\n"
        f"- 산업 키워드: {keywords}\n"
        f"- Impact/Risk/Opportunity 모두 반드시 {label} 관점에서 작성하세요\n"
        f"- 이 산업의 CEO가 가장 궁금해할 포인트:\n"
        f"{template_text}\n"
    )

    # analysis_keywords: 기사 내용 우선, 키워드는 참고 가이드로만 활용 (V14 Fix 4)
    # 키워드를 그대로 나열하면 LLM이 기사 무관하게 키워드만 반복(파롯팅)하므로
    # "기사에서 추출한 구체적 내용 기반 작성" 지시 + 참고 키워드 제시 방식으로 변경
    analysis_kw = profile.get("analysis_keywords", {})
    if analysis_kw:
        impact_kw = ", ".join(analysis_kw.get("impact_focus", []))
        risk_kw = ", ".join(analysis_kw.get("risk_focus", []))
        opp_kw = ", ".join(analysis_kw.get("opportunity_focus", []))
        context += (
            f"- Impact: 기사의 구체적 사건·수치를 기반으로 {label} 관점에서 분석하세요"
            f" (산업 참고: {impact_kw})\n"
            f"- Risk: 기사에서 언급된 구체적 위험 요인을 중심으로 작성하세요"
            f" (산업 참고: {risk_kw})\n"
            f"- Opportunity: 기사에서 언급된 구체적 기회 요인을 중심으로 작성하세요"
            f" (산업 참고: {opp_kw})\n"
        )

    # V5: action_templates 활용 — Action 필드 구체성 강화
    # V17.5-fix: 템플릿 복사 방지 — 형식 참고용임을 명시, 기사 본문 기반 재작성 강제
    action_templates = profile.get("action_templates", [])
    if action_templates:
        action_examples = "\n".join(f"  - {t}" for t in action_templates[:5])
        context += (
            f"- Action bullet 작성 시 아래 산업 전문 템플릿을 '형식 참고용'으로만 활용하세요:\n"
            f"{action_examples}\n"
            f"- ⛔ 핵심 금지: 위 예시를 그대로 복사하지 마세요. 이 기사 본문에 등장하지 않는 내용"
            f"(FOB 가격, 해외 바이어 협의, 해운운임, 물류비 등)은 Action에 포함하지 마세요.\n"
            f"- ✅ Action은 반드시 이 기사에서 언급된 기업명·이슈·수치·지역 등 구체적 정보를 기반으로 재작성하세요.\n"
        )

    # V17.3-demo: 소비재 전용 Action 구체성 강화 (데모 안정화)
    # 짧은 기사(650~800자)에서도 Action이 추상적 원론이 아닌 즉시 실행 가능 항목이 되도록 강제
    # V17.4-patch: "소비재·식품" key도 매칭되도록 확장 (기존 "소비재"만 매칭되던 버그 수정)
    # V17.5-fix: FOB/바이어 예시가 무관 기사에 복사되는 오류 수정 — 예시 조건부 사용 + 금지 규칙 강화
    if industry_key in ("소비재", "소비재·식품"):
        context += (
            "- ⚡ 소비재 Action 필수 규칙: 기사 본문이 짧더라도 Action 3개는 반드시 아래 형식으로 작성\n"
            "  형식: [이 기사와 직접 연관된, 담당자가 오늘 또는 이번 주 내 할 수 있는 구체적 행동]\n"
            "  예시(수출 관련 기사에만 적용): '베트남 현지 유통 파트너에게 이번 규제 변화 영향 파악 요청'\n"
            "  예시(국내·브랜드 관련 기사에만 적용): '이번 콜라보 브랜드 반응 모니터링 후 유사 파트너십 발굴 검토'\n"
            "  ⛔ 절대 금지 1: '모니터링', '검토', '관심 가질 것' 같은 추상 표현만 있는 Action\n"
            "  ⛔ 절대 금지 2: 기사에서 언급되지 않은 FOB 가격·해외 바이어·해운운임·물류비를 Action에 삽입\n"
            "  ⛔ 절대 금지 3: action_templates 예시를 단어만 바꿔 그대로 복사하는 것\n"
        )

    # V5: questions_frame 활용 — 경영진 질문 구체성 강화
    # V17.4-patch: 고정 템플릿 재사용 방지 — 기사 제목·키워드를 질문에 반드시 반영하도록 지시
    questions_frame = profile.get("questions_frame", [])
    if questions_frame:
        q_examples = "\n".join(f"  - {q}" for q in questions_frame[:3])
        context += (
            f"- questions 필드 작성 시 아래 CEO 질문 예시를 참고하되, 반드시 이 기사 고유의 내용(기업명·이슈·수치)을 포함한 질문으로 재작성하세요:\n"
            f"{q_examples}\n"
            f"- ⚠️ 템플릿 복사 금지: 위 예시를 그대로 사용하지 마세요. 기사 제목과 본문에서 추출한 핵심 키워드를 질문 안에 포함하세요.\n"
            f"  예) '이번 [기사 핵심 이슈]가 우리 [구체 영역]에 미치는 단기 영향은?'\n"
        )

    # V5: checklist_frame 활용 — 점검 항목 구체성 강화
    # V17.4-patch: 고정 템플릿 재사용 방지 — 기사 내용 반영 강제
    checklist_frame = profile.get("checklist_frame", [])
    if checklist_frame:
        cl_examples = "\n".join(f"  - {c}" for c in checklist_frame[:3])
        context += (
            f"- checklist 필드 작성 시 아래 주간 점검 예시를 참고하되, 이 기사에서 언급된 구체적 내용을 반영한 점검 항목으로 재작성하세요:\n"
            f"{cl_examples}\n"
            f"- ⚠️ 템플릿 복사 금지: 위 예시를 그대로 사용하지 마세요. 이 기사의 핵심 이슈와 직접 연결된 점검 항목을 작성하세요.\n"
        )

    return context


def _llm_worker():
    """Queue에서 요청을 하나씩 꺼내 순차 처리."""
    global _llm_worker_running
    while True:
        try:
            task = _llm_request_queue.get(timeout=30)
            if task is None:  # poison pill
                break
            fn, args, result_holder = task
            try:
                result_holder["result"] = fn(*args)
            except Exception as e:
                result_holder["error"] = e
            finally:
                _llm_request_queue.task_done()
        except queue.Empty:
            break
    with _llm_worker_lock:
        _llm_worker_running = False


def _ensure_worker():
    """Worker 스레드가 실행 중이 아니면 시작."""
    global _llm_worker_running
    with _llm_worker_lock:
        if not _llm_worker_running:
            _llm_worker_running = True
            t = threading.Thread(target=_llm_worker, daemon=True)
            t.start()


def _enqueue_llm_call(fn, *args, timeout=20):
    """LLM 호출을 큐에 넣고 결과 대기."""
    _ensure_worker()
    result_holder = {}
    _llm_request_queue.put((fn, args, result_holder))
    _llm_request_queue.join()  # 현재 태스크 완료 대기
    if "error" in result_holder:
        raise result_holder["error"]
    return result_holder.get("result")


def _call_groq_model(
    api_key: str,
    system_msg: str,
    prompt: str,
    model_config: dict,
    title: str = "",
    industry_key: str = "일반",
) -> dict | str | None:
    """V11: 단일 모델 Groq API 호출. _summarize_with_llm에서 모델별로 호출."""
    model_id = model_config["model_id"]
    model_label = model_config["label"]
    temperature = model_config["temperature"]
    max_tokens = model_config["max_tokens"]
    timeout = model_config["timeout"]

    try:
        _rate_limit_wait()
        _t_start = time.time()
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        _t_elapsed = time.time() - _t_start

        # V13-429: 429 Rate Limit 즉시 다음 모델로 패스
        # — 같은 모델 backoff 재시도는 quota 낭비. 8B fallback이 더 빠름.
        if resp.status_code == 429:
            print(f"[summarizer] 🚦 Rate Limit (429) [{model_label}] — 즉시 다음 모델로 패스 (backoff 생략)")
            return None  # _summarize_with_llm이 다음 모델(8B)로 이동

        if resp.status_code != 200:
            print(f"[summarizer] ❌ [{model_label}] API 오류 {resp.status_code}: {resp.text[:200]}")
            return None

        raw_out = resp.json()["choices"][0]["message"]["content"].strip()
        print(f"[summarizer] ✅ [{model_label}] 응답 ({_t_elapsed:.1f}s) — '{title[:30]}...' [{industry_key}]")

        parsed = _parse_4frame_json(raw_out)
        if parsed:
            return parsed

        print(f"[summarizer] ❌ [{model_label}] JSON 파싱 실패 — 앞 200자: {raw_out[:200]}")
        validated = _validate_output(raw_out)
        if validated:
            print(f"[summarizer] [{model_label}] 4-frame 파싱 실패 → 3줄 텍스트 폴백")
            return validated
        return None

    except Exception as e:
        print(f"[summarizer] ❌ [{model_label}] 호출 실패 [{type(e).__name__}]: {str(e)[:200]}")
        return None


def _summarize_with_llm(text: str, title: str = "", industry_key: str = "일반",
                        skip_primary: bool = False) -> dict | str | None:
    """
    V11: 멀티모델 폴백 Groq API 호출로 4-frame 요약 생성.

    반환값:
      - 성공 시: dict {"impact", "risk", "opportunity", "action"}
      - JSON 파싱 실패 시: str (기존 3줄 텍스트 폴백)
      - 호출 실패 시: None

    멀티모델 체인: llama-3.3-70b-versatile → llama-3.1-8b-instant
    무료 한도: 30 RPM / 14,400 RPD
    키 발급: https://console.groq.com → API Keys → Create
    환경변수: GROQ_API_KEY=<your_key>
              또는 .streamlit/secrets.toml 의 [groq] api_key = "..."

    V13-429 추가:
      skip_primary=True — 429 발생 후 품질 재시도 시 70B 건너뛰고 8B 직접 사용.
      429는 backoff 없이 즉시 None 반환 → 8B 폴백으로 빠르게 이동.
    """
    api_key = _get_llm_key()
    if not api_key:
        return None

    # V17.4-kotra: KOTRA 구조화 텍스트 감지
    # kotra_parser.py가 생성한 [기사 핵심 요약] / [본문 주요 내용] / [표 요약] / [PDF 핵심 내용] 태그 확인
    _is_kotra_structured = (
        "[기사 핵심 요약]" in text or
        "[본문 주요 내용]" in text or
        "[PDF 핵심 내용]" in text or
        "[표 요약]" in text
    )
    # KOTRA 구조화 텍스트는 섹션이 많으므로 토큰 한도를 확장 (3000 → 4000자)
    _body_limit = 4000 if _is_kotra_structured else 3000

    body_trunc = text[:_body_limit].strip()
    if len(body_trunc) < 80:
        return None

    industry_context = _build_industry_context(industry_key)
    industry_label = _resolve_industry_label(industry_key)

    # V17.4-kotra: KOTRA 구조화 입력용 프롬프트 접두사 주입
    _kotra_prefix = ""
    if _is_kotra_structured:
        _has_pdf = "[PDF 핵심 내용" in text
        _has_table = "[표 요약]" in text
        _kotra_prefix = (
            "\n⚡ KOTRA 구조화 기사 분석 지시사항:\n"
            "- [기사 핵심 요약] 섹션을 Impact 작성의 핵심 근거로 사용하세요\n"
            "- [본문 주요 내용] 섹션에서 구체적 사실·수치를 추출하세요\n"
            + ("- [표 요약] 섹션의 수치·비교 데이터를 Risk/Opportunity 근거로 활용하세요\n" if _has_table else "")
            + ("- [PDF 핵심 내용 — 우선 참고] 또는 [PDF 핵심 내용] 섹션이 HTML 본문보다 상세하므로 분석의 주 근거로 사용하세요\n" if _has_pdf else "")
            + "- 섹션 구분 태그([...]) 자체는 출력에 포함하지 마세요\n"
        )
        print(f"[summarizer] 🔎 KOTRA 구조화 입력 감지 (PDF={_has_pdf}, 표={_has_table}) — 확장 프롬프트 적용")

    prompt = _LLM_PROMPT.format(
        title=title or "",
        body=body_trunc,
        industry_context=industry_context + _kotra_prefix,
    )
    system_msg = SYSTEM_PROMPT_TEMPLATE.format(
        industry_label=industry_label,
        industry_context=industry_context,
    ).strip()

    # V11 + V13-429: 멀티모델 폴백 체인
    for idx, model_config in enumerate(_LLM_MODELS):
        model_label = model_config["label"]
        is_primary = (idx == 0)

        # V13-429: skip_primary=True이면 70B 건너뛰고 8B로 바로 이동
        # (429 발생 후 품질 재시도 시 70B는 여전히 rate-limited일 가능성 높음)
        if is_primary and skip_primary:
            print(f"[summarizer] ⏭️ skip_primary=True — [{model_label}] 건너뛰고 폴백 모델로")
            continue

        result = _call_groq_model(
            api_key, system_msg, prompt, model_config,
            title=title, industry_key=industry_key,
        )

        if result:
            if is_primary:
                _groq_record_success()
            else:
                _groq_record_success()
                print(f"[summarizer] 🔄 폴백 모델 [{model_label}] 성공")
            return result

        # 기본 모델 실패 → 서킷 브레이커 기록 후 경량 모델 시도
        if is_primary:
            _groq_record_failure()
            print(f"[summarizer] 🔄 기본 모델 실패 → 경량 모델 [{_LLM_MODELS[1]['label']}] 시도")

    # 모든 모델 실패
    _groq_record_failure()
    return None


def _parse_4frame_json(raw: str) -> dict | None:
    """LLM 출력에서 4-frame JSON을 파싱 (V4: 중첩 브레이스 + 이스케이프 복구)."""
    if not raw:
        return None

    required_keys = {"impact", "risk", "opportunity", "action"}

    def _add_optional(data, result):
        for opt_key in ("questions", "checklist"):
            if opt_key in data and isinstance(data[opt_key], str) and data[opt_key].strip():
                result[opt_key] = data[opt_key].strip()
        return result

    # Strategy 1: ```json ... ``` 블록
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if required_keys.issubset(data.keys()):
                result = {k: data[k].strip() for k in required_keys if isinstance(data.get(k), str)}
                return _add_optional(data, result)
        except json.JSONDecodeError as e:
            _log.debug("JSON decode error in strategy 1: %s", e)

    # Strategy 2: 중첩 브레이스 허용 매칭
    depth = 0
    start = raw.find("{")
    if start >= 0:
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    json_str = raw[start:i+1]
                    try:
                        data = json.loads(json_str)
                        if required_keys.issubset(data.keys()):
                            result = {k: data[k].strip() for k in required_keys if isinstance(data.get(k), str)}
                            return _add_optional(data, result)
                    except json.JSONDecodeError as e:
                        _log.debug("JSON decode error in strategy 2 with json_str: %s", e)
                    break

    # Strategy 3: 이스케이프 복구 (줄바꿈, 탭 등)
    json_match = re.search(r'\{[^{}]*"impact"[^{}]*\}', raw, re.DOTALL)
    if json_match:
        json_str = json_match.group(0)
        # 제어문자 이스케이프
        json_str = json_str.replace('\n', '\\n').replace('\t', '\\t').replace('\r', '\\r')
        # 이미 이스케이프된 것 이중 이스케이프 방지
        json_str = json_str.replace('\\\\n', '\\n').replace('\\\\t', '\\t')
        try:
            data = json.loads(json_str)
            if required_keys.issubset(data.keys()):
                result = {k: data[k].strip() for k in required_keys if isinstance(data.get(k), str)}
                return _add_optional(data, result)
        except json.JSONDecodeError as e:
            _log.debug("JSON decode error in strategy 3: %s", e)

    # Strategy 4: 필드별 추출 폴백
    result = {}
    for key in required_keys:
        pattern = rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)?"'
        match = re.search(pattern, raw, re.DOTALL)
        if match:
            result[key] = match.group(1).replace('\\n', '\n').replace('\\"', '"').strip()
    if required_keys.issubset(result.keys()) and all(result[k] for k in required_keys):
        # Also try to extract optional keys
        for opt_key in ("questions", "checklist"):
            pattern = rf'"{opt_key}"\s*:\s*"((?:[^"\\]|\\.)*)?"'
            match = re.search(pattern, raw, re.DOTALL)
            if match and match.group(1).strip():
                result[opt_key] = match.group(1).replace('\\n', '\n').replace('\\"', '"').strip()
        return result

    return None


_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "summary_cache.json")
_CACHE_TTL_DAYS = 7   # V16: 1일 → 7일 (LLM 호출 절감, 주 단위 뉴스 사이클 반영)

# TODO-5: 품질 기반 TTL 차등 정책 (source별 유효 기간)
# groq/groq_retry/fallback_model: 7일 — LLM 생성, 신뢰도 높음, 재생성 비용 높음
# smart_fallback/body_short: 3일 — 본문 짧아 제한적, 조만간 full-body로 교체 가능성
# snippet: 2일 — GN 스니펫 기반, 본문 fetch 후 재생성 우선
# minimal_fallback: 캐시 저장 안 함 — 본문 없음, 재생성해도 동일 결과이므로 캐시 낭비
_CACHE_TTL_BY_SOURCE: dict[str, int] = {
    "groq":              7,
    "groq_retry":        7,
    "fallback_model":    7,
    "cache":             7,   # 캐시에서 읽은 경우 (출처 보존)
    "smart_fallback":    3,
    "body_short":        3,
    "industry_fallback": 3,
    "snippet":           2,
    "minimal_fallback":  0,   # 0 = 캐시 저장 안 함
}


def _load_summary_cache() -> dict:
    """요약 캐시 파일 로드."""
    try:
        import pathlib
        p = pathlib.Path(_CACHE_PATH)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        _log.warning("Failed to load groq cache from %s: %s", _CACHE_PATH, e)
    return {}


def _save_summary_cache(cache: dict) -> None:
    """요약 캐시 파일 저장."""
    try:
        import pathlib
        p = pathlib.Path(_CACHE_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _log.warning("Failed to save groq cache to %s: %s", _CACHE_PATH, e)


_PROMPT_VERSION = "v17"  # V17.5-fix: Action 템플릿 복사 방지 + 품질검증 패널티 추가 → v16 캐시 무효화


def _purge_orphaned_cache(cache: dict, max_age_days: int = 30) -> dict:
    """V16 P3: 오래된 orphaned 캐시 엔트리 정리.

    조건:
    - TTL 초과 (max_age_days 이상) 엔트리 제거
    - 현재 _PROMPT_VERSION과 다른 버전 키 제거
    반환: 정리된 캐시 딕트 (원본 미변경, 새 딕트 반환)
    """
    import pathlib as _pl
    _now = datetime.now()
    _cleaned: dict = {}
    _removed = 0
    for _k, _entry in cache.items():
        try:
            # 버전 불일치 엔트리 제거 (orphaned)
            _ver = _entry.get("prompt_version", "")
            if _ver and _ver != _PROMPT_VERSION:
                _removed += 1
                continue
            # TTL 초과 엔트리 제거
            _cached_at = datetime.fromisoformat(_entry.get("cached_at", ""))
            if (_now - _cached_at).days >= max_age_days:
                _removed += 1
                continue
            _cleaned[_k] = _entry
        except Exception:
            _cleaned[_k] = _entry  # 파싱 실패 시 유지
    if _removed:
        _log.info("[cache] orphaned/만료 엔트리 %d건 정리 (max_age=%dd)", _removed, max_age_days)
    return _cleaned


def _cache_key(text: str, industry_key: str) -> str:
    """텍스트 + 산업 키 + 프롬프트 버전 기반 캐시 키 생성."""
    import hashlib
    _ik = industry_key or "일반"
    content = f"{_PROMPT_VERSION}|{_ik}|{text[:500]}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _cache_key_for_url(url: str, industry_key: str) -> str:
    """V17: URL 기반 캐시 키 (URL이 제공될 때 text-hash보다 우선 사용)."""
    import hashlib
    _ik = industry_key or "일반"
    content = f"{_PROMPT_VERSION}|{_ik}|url:{url}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _generate_headline(title: str, text: str = "") -> str:
    """기사 제목에서 핵심 요약 생성 (V9.3: 40자 이내, 오염 패턴 차단)."""
    if not title:
        return ""
    # 날짜/별표 제거
    cleaned = re.sub(r'\[[\d.]+\]\s*', '', title)
    cleaned = re.sub(r'[★⭐]+\s*', '', cleaned).strip()
    if len(cleaned) <= 40:
        return cleaned
    # 40자 넘으면 단어 경계에서 자르기
    cut = cleaned[:40]
    last_space = cut.rfind(' ')
    if last_space > 25:
        return cut[:last_space] + "…"
    return cut + "…"


def _validate_summary_quality(summary_dict: dict) -> bool:
    """요약 품질 최소 기준 검증. False면 재생성 필요. (하위 호환용 래퍼)"""
    score, _issues = _validate_summary_quality_v2(summary_dict)
    return score >= 50  # 50점 이상이면 PASS


def _validate_summary_quality_v2(
    summary_dict: dict,
    industry_key: str = "일반",
) -> tuple[int, list[str]]:
    """V11: 다면 품질 스코어링 (0~100점).

    반환: (score, issues)
      score: 0~100 종합 점수
      issues: 구체적 품질 미달 사유 목록 (재시도 힌트로 활용)

    채점 기준 (100점 만점):
      - 기본 길이 충족 (impact/risk/opp ≥80자, action ≥30자): 25점
      - 필드 간 비중복 (앞 30자 상이): 15점
      - Action bullet 3개 정확: 15점
      - 산업 키워드 포함 (impact 첫 문장): 15점
      - Risk 인과관계 구조 (→ 또는 하면/발생/영향): 10점
      - Questions/Checklist 존재: 10점
      - 볼드 마크다운(**) 사용: 5점
      - 총 문장 수 충분 (2문장 이상): 5점
    """
    import re as _re

    score = 0
    issues: list[str] = []
    fields = ["impact", "risk", "opportunity", "action"]
    texts = []

    # ── 1. 기본 길이 충족 (25점) ──
    length_ok = True
    for key in fields:
        text = summary_dict.get(key, "")
        min_len = 30 if key == "action" else 80
        if len(text) < min_len:
            issues.append(f"{key} 길이 부족 ({len(text)}자 < {min_len}자)")
            length_ok = False
        texts.append(text)
    if length_ok:
        score += 25

    # ── 2. 필드 간 비중복 (15점) ──
    dup_found = False
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            if texts[i] == texts[j]:
                issues.append(f"{fields[i]}와 {fields[j]} 완전 동일")
                dup_found = True
            elif texts[i] and texts[j]:
                overlap = min(30, len(texts[i]), len(texts[j]))
                if overlap > 0 and texts[i][:overlap] == texts[j][:overlap]:
                    issues.append(f"{fields[i]}와 {fields[j]} 앞 30자 동일")
                    dup_found = True
    if not dup_found:
        score += 15

    # ── 3. Action bullet 3개 정확 (15점) ──
    action_text = summary_dict.get("action", "")
    bullet_count = action_text.count("•")
    if bullet_count >= 3:
        score += 15
    elif bullet_count == 2:
        score += 8
        issues.append(f"Action bullet {bullet_count}개 (3개 필요)")
    elif bullet_count == 1:
        score += 3
        issues.append(f"Action bullet {bullet_count}개 (3개 필요)")
    else:
        issues.append("Action에 bullet point(•) 없음")

    # ── 4. 산업 키워드 포함 (15점) ──
    _ind_kws: list[str] = []
    if industry_key and industry_key != "일반":
        try:
            from core.industry_config import get_profile
            _profile = get_profile(industry_key)
            _ind_kws = _profile.get("keywords", [])
        except ImportError:
            pass

    if _ind_kws:
        impact_text = summary_dict.get("impact", "")
        # impact 첫 문장에서 산업 키워드 존재 확인
        first_sentence = _re.split(r'(?<=[.다요음임됨])\s', impact_text)[0] if impact_text else ""
        kw_found = any(kw in first_sentence for kw in _ind_kws)
        if kw_found:
            score += 15
        else:
            # impact 전체에서라도 확인
            kw_in_impact = any(kw in impact_text for kw in _ind_kws)
            if kw_in_impact:
                score += 10
                issues.append("Impact 첫 문장에 산업 키워드 없음 (본문에는 있음)")
            else:
                issues.append(f"Impact에 산업 키워드 없음 (필요: {', '.join(_ind_kws[:5])})")
    else:
        # 일반 산업은 키워드 체크 면제 → 만점
        score += 15

    # ── 5. Risk 인과관계 구조 (10점) ──
    risk_text = summary_dict.get("risk", "")
    causal_patterns = [
        _re.compile(r'[가-힣]+하면.{5,}[가-힣]+(?:발생|영향|우려|위험|악화)'),
        _re.compile(r'→'),  # 화살표 사용
        _re.compile(r'(?:때문에|으로 인해|결과적으로|따라서)'),  # 인과 접속사
        _re.compile(r'(?:경우|상황).{2,20}(?:될 수|할 수|가능성)'),  # 조건-결과
    ]
    has_causal = any(p.search(risk_text) for p in causal_patterns)
    if has_causal:
        score += 10
    else:
        issues.append("Risk에 인과관계 구조 부족 (A→B→영향 형식 권장)")

    # ── 6. Questions/Checklist 존재 (10점) ──
    has_q = bool(summary_dict.get("questions", "").strip())
    has_cl = bool(summary_dict.get("checklist", "").strip())
    if has_q and has_cl:
        score += 10
    elif has_q or has_cl:
        score += 5
        issues.append("questions 또는 checklist 중 하나 누락")
    else:
        issues.append("questions와 checklist 모두 누락")

    # ── 7. 볼드 마크다운 사용 (5점) ──
    bold_count = sum(1 for f in fields if "**" in summary_dict.get(f, ""))
    if bold_count >= 2:
        score += 5
    elif bold_count == 1:
        score += 3
    else:
        issues.append("볼드 마크다운(**) 미사용")

    # ── 8. 문장 수 충분 (5점) ──
    for key in ["impact", "risk", "opportunity"]:
        text = summary_dict.get(key, "")
        sent_count = len([s for s in _re.split(r'(?<=[.다요음임됨])\s', text) if len(s.strip()) > 10])
        if sent_count < 2:
            issues.append(f"{key} 문장 수 부족 ({sent_count}문장, 2문장 이상 권장)")
    # 3개 필드 모두 2문장 이상이면 만점
    all_sufficient = all(
        len([s for s in _re.split(r'(?<=[.다요음임됨])\s', summary_dict.get(k, "")) if len(s.strip()) > 10]) >= 2
        for k in ["impact", "risk", "opportunity"]
    )
    if all_sufficient:
        score += 5

    # ── 9. V17.5: Action 템플릿 복사 감지 (-15점 패널티) ──
    # 두 가지 방식 병행:
    # (A) 소비재 등 산업별 "템플릿 지문 구절" 하드코딩 — 가장 정확, 가장 많이 복사되는 구절
    # (B) industry_config action_templates 중간 부분과 직접 비교
    action_text_check = summary_dict.get("action", "")
    _template_copy_penalty = False

    # (A) 소비재 전용 고빈도 복사 지문 감지
    _TEMPLATE_FINGERPRINTS = {
        "소비재": [
            "주문 동향 확인",              # "바이어에게 이번 이슈 영향 파악 요청 및 주문 동향 확인"
            "수출가(FOB) 조정 가능 여부",   # "수출가(FOB) 조정 가능 여부 및 물류비 변동분 반영"
            "마진율 점검",                 # "K-푸드 수출국별 판매 실적 및 마진율 점검"
            "FOB 가격 재계산",             # "FOB 가격 재계산 및 주요 바이어 통보 준비"
        ],
        "소비재·식품": [
            "주문 동향 확인",
            "수출가(FOB) 조정 가능 여부",
            "마진율 점검",
            "FOB 가격 재계산",
        ],
    }
    _fingerprints = _TEMPLATE_FINGERPRINTS.get(industry_key, [])
    for _fp in _fingerprints:
        if _fp in action_text_check:
            issues.append(f"Action 템플릿 복사 감지: '{_fp}' — 기사 내용 기반으로 재작성 필요")
            _template_copy_penalty = True
            break

    # (B) industry_config action_templates 중간 구절 비교 (앞 5자 제외, 중간 15자 추출)
    if not _template_copy_penalty and action_text_check and industry_key and industry_key != "일반":
        try:
            from core.industry_config import get_profile as _gp
            _prof_check = _gp(industry_key)
            _act_templates = _prof_check.get("action_templates", [])
            for _tmpl in _act_templates:
                # 앞 5자를 건너뛴 중간 구절 15자 — 변형 복사도 감지
                if len(_tmpl) > 20:
                    _mid = _tmpl[5:20].strip()
                    if _mid and len(_mid) >= 8 and _mid in action_text_check:
                        issues.append(f"Action 템플릿 유사 복사 감지: '{_mid}...' — 기사 기반으로 재작성 필요")
                        _template_copy_penalty = True
                        break
        except Exception:
            pass

    if _template_copy_penalty:
        score = max(0, score - 15)

    # V9: 후처리 위생 검사 적용
    _sanitize_summary_output(summary_dict)

    if issues:
        print(f"[summarizer] 📊 품질 점수: {score}/100 — 미달 항목: {len(issues)}개")
        for iss in issues[:3]:
            print(f"[summarizer]   ⚠️ {iss}")

    return score, issues


def _build_retry_hint(issues: list[str], industry_key: str = "일반") -> str:
    """V11: 품질 미달 항목에 기반한 구체적 재시도 힌트 생성."""
    hints = []

    for issue in issues[:4]:  # 최대 4개 힌트
        if "길이 부족" in issue:
            field = issue.split(" ")[0]
            hints.append(f"⚠️ {field} 필드를 100자 이상으로 더 구체적으로 작성하세요.")
        elif "동일" in issue:
            hints.append("⚠️ 각 필드(impact/risk/opportunity/action)는 완전히 다른 관점으로 작성하세요.")
        elif "bullet" in issue:
            hints.append("⚠️ Action 필드에 '• '로 시작하는 bullet point를 정확히 3개 작성하세요.")
        elif "산업 키워드" in issue:
            try:
                from core.industry_config import get_profile
                _profile = get_profile(industry_key)
                kws = _profile.get("keywords", [])[:5]
                hints.append(f"⚠️ Impact 첫 문장에 반드시 다음 키워드 중 하나 포함: {', '.join(kws)}")
            except ImportError:
                hints.append("⚠️ Impact 첫 문장에 산업 전문 키워드를 반드시 포함하세요.")
        elif "인과관계" in issue:
            hints.append("⚠️ Risk는 'A하면 → B가 발생하여 → C에 영향' 형식의 인과관계로 작성하세요.")
        elif "questions" in issue or "checklist" in issue:
            hints.append("⚠️ questions(CEO 질문 2~3개)와 checklist(주간 점검 3개) 필드도 반드시 포함하세요.")
        elif "볼드" in issue:
            hints.append("⚠️ 핵심 키워드에 **볼드 마크다운**을 적용하세요.")
        elif "문장 수" in issue:
            hints.append("⚠️ impact/risk/opportunity 각 필드에 최소 2문장 이상 작성하세요.")
        elif "템플릿 복사" in issue or "유사 복사" in issue:
            hints.append(
                "⚠️ Action 항목이 사전 정의된 템플릿을 그대로 복사한 것으로 감지되었습니다. "
                "이 기사에서 직접 언급된 기업명·이슈·수치·이벤트를 기반으로 Action 3개를 완전히 새로 작성하세요. "
                "기사에 언급되지 않은 FOB 가격, 해외 바이어, 해운운임, 물류비 내용은 포함하지 마세요."
            )

    return "\n".join(hints) if hints else "⚠️ 이전 응답의 품질이 부족합니다. 각 항목을 더 구체적이고 상세하게 작성하세요."


def _sanitize_summary_output(summary_dict: dict) -> dict:
    """V9: AI 분석 결과에서 개인정보, 방송 메타데이터, 무관 텍스트를 탐지하여 제거.

    이 함수는 LLM 결과와 규칙기반 폴백 결과 모두에 적용됩니다.
    """
    import re as _re

    # 제거 대상 패턴들
    _PII_PATTERNS = [
        _re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9.]{2,}'),  # 이메일 (co.kr 등)
        _re.compile(r'<전화연결\s*:.{0,60}>'),                           # 방송 큐시트
        _re.compile(r'\[앵커\]|\[기자\]|\[리포터\]'),                     # 방송 마커
        _re.compile(r'<앵커>|</앵커>|<리포트>|</리포트>'),                 # 방송 태그
        _re.compile(r'[가-힣]{2,4}\s*기자\s*[(（][a-zA-Z0-9_.@]+'),      # 기자 바이라인
        _re.compile(r'영상취재\s*[:=]|촬영\s*[:=]|앵커\s*[:=]'),         # 크레딧
        _re.compile(r'MBC뉴스|YTN\s|SBS뉴스|KBS뉴스'),                   # 방송사명
        _re.compile(r'연합뉴스TV'),                                      # 연합뉴스TV
        # V9.1: 연구원/기자/애널리스트 실명 패턴
        _re.compile(r'[가-힣]{2,4}\s+\S*(?:금융|은행|연구|증권|리서치|자산운용|투자|경제)[가-힣]*\s+(?:연구위원|연구원|수석연구원|선임연구원|애널리스트|수석|팀장|센터장|이사|부장|실장|본부장)'),
        _re.compile(r'[가-힣]{2,4}\s+(?:연구위원|연구원|수석연구원|선임연구원|수석이코노미스트|이코노미스트|애널리스트)(?:\s|$|,|\.)'),  # 이름+직함만
        _re.compile(r'[가-힣]{2,4}\s+(?:기자|특파원|통신원|리포터)(?:\s|$|,|\.)'),  # 기자 이름
        # V9.1: 메타데이터/댓글 태그 누출
        _re.compile(r'\[사람이\s*되고\s*싶어요\d*\]'),                    # 메타 태그 누출
        _re.compile(r'\[[^\]]{0,30}싶어요\d*\]'),                         # 유사 메타 태그
        _re.compile(r'\[편집자\s*주\]|\[광고\]|\[후원\]'),                # 편집 마커
    ]

    # 문맥 부적합 텍스트 패턴 (다른 기사에서 유입된 문장)
    _CONTEXT_LEAK_PATTERNS = [
        _re.compile(r'전세대출.{0,20}서민\s*주거'),        # 부동산 관련
        _re.compile(r'주택시장에\s*영향.{0,20}전세대출'),  # 부동산 관련 변형
        _re.compile(r'정년\s*연장\s*논의'),                # 노동 관련
        _re.compile(r'사익편취'),                           # 기업지배구조
        _re.compile(r'주가\s*상승.{0,20}달갑지\s*않'),     # 주식시장 논평
        _re.compile(r'상속.{0,10}증여세\s*부담'),          # 세금
        # V9.1: 교차 기사 오염 패턴 추가
        _re.compile(r'투자자\s*보호\s*강화.{0,30}거버넌스'),          # 투자자보호 기사 오염
        _re.compile(r'이해충돌\s*상황.{0,40}민사소송'),                # 투자자보호 기사 법적구제 오염
        _re.compile(r'일반주주\s*입장.{0,30}법적\s*구제'),            # 투자자보호 기사 오염
        _re.compile(r'기업\s*거버넌스\s*개선.{0,20}투명\s*경영'),     # 거버넌스 기사 오염
        _re.compile(r'상법\s*개정.{0,30}자사주'),                     # 상법개정 기사 오염
        _re.compile(r'코스피\s*5.?000\s*시대'),                       # 코스피 기사 오염
        _re.compile(r'주가\s*하락\s*등이\s*있다면'),                   # 주가하락 논평 오염
        # V9.3: 기사 제목 전체가 오염원으로 삽입되는 패턴
        _re.compile(r'중국이\s*희토류\s*수출\s*통제하면'),             # 기사 제목 문구 오염
        _re.compile(r'투자자\s*보호\s*강화가\s*곧'),                   # 기사 제목 headline 오염
        _re.compile(r'투자자\s*보호\s*(?:시나리오|활용|관련|기술|모니터링|납품|수주|동향|신규|계약)'),  # "투자자 보호" 무관 삽입 (V9.3 확대)
    ]

    all_fields = ["impact", "risk", "opportunity", "action", "questions", "checklist", "headline"]

    # V9.1: 문장 중간 잘림 보정 (숫자 뒤 잘림 감지)
    _INCOMPLETE_SENTENCE = _re.compile(r'\s\d+$')  # "수출액 24" 같은 숫자 뒤 잘림

    for field in all_fields:
        text = summary_dict.get(field, "")
        if not text:
            continue

        modified = False

        # V16 Fix P2-1: 비한글 문자(중국어 간체·번체, 일본어 가나) 정제
        # 허용: 한글, 영문, 숫자, 공백, 일반 구두점, %, $, ·, ★, ▶ 등
        _CJK_EXTRA = _re.compile(
            r'[\u4E00-\u9FFF\u3400-\u4DBF\uF900-\uFAFF'   # 한자(중국어 간·번체)
            r'\u3040-\u309F\u30A0-\u30FF]'                 # 일본어 히라가나·카타카나
        )
        if _CJK_EXTRA.search(text):
            text = _CJK_EXTRA.sub("", text)
            # 잔여 이중 공백 정리
            text = _re.sub(r'\s{2,}', ' ', text).strip()
            modified = True
            _log.info("[sanitize] %s 필드 비한글 문자(CJK) 제거됨", field)

        # V9.1: 문장 중간 잘림 보정
        if _INCOMPLETE_SENTENCE.search(text.rstrip()):
            # 마지막 완전한 문장까지만 유지
            sentences = _re.split(r'(?<=[.다요음임됨])\s', text)
            if len(sentences) > 1:
                text = ' '.join(sentences[:-1]).strip()
                modified = True

        # PII 제거
        for pat in _PII_PATTERNS:
            if pat.search(text):
                text = pat.sub("", text)
                modified = True

        # 문맥 유출 텍스트 감지 — 해당 문장 제거 (V9.3: headline은 전체 제거)
        for pat in _CONTEXT_LEAK_PATTERNS:
            if pat.search(text):
                if field == "headline":
                    # V9.3: headline은 짧은 텍스트이므로 패턴 직접 제거
                    text = pat.sub("", text).strip()
                else:
                    # 해당 패턴이 포함된 문장 전체 제거 (V9.3: 소수점 보호)
                    sentences = _re.split(r'(?<!\d)\.(?!\d)', text)  # "24.2%" 보호
                    sentences = [s for s in sentences if not pat.search(s)]
                    text = ".".join(sentences)
                modified = True

        if modified:
            # V9.1: PII 제거 후 잔여 조사 처리 ("은 환율이" → "환율이")
            text = _re.sub(r'(?:^|\.\s*)[은는이가의에을를도와과로]\s+', lambda m: m.group(0)[0] + ' ' if m.group(0)[0] == '.' else '', text)
            # 정리: 이중 공백, 빈 bullet 등 제거
            text = _re.sub(r'\s{2,}', ' ', text).strip()
            text = _re.sub(r'•\s*\n|•\s*$', '', text)
            summary_dict[field] = text
            _log.info(f"[sanitize] {field} 필드에서 PII/무관 텍스트 제거됨")

    return summary_dict


def _rule_based_enhanced_summary(text: str, title: str, industry_key: str) -> dict:
    """규칙 기반이지만 품질 보장하는 폴백 요약. 숫자 포함 문장 우선 추출."""
    import re as _re
    try:
        from core.company_profile_v2 import get_profile
    except ImportError:
        get_profile = lambda k: {}

    sentences = [s.strip() for s in _re.split(r'[.。!?\n]+', text) if len(s.strip()) > 10]
    sentences_with_numbers = [s for s in sentences if _re.search(r'\d+[%조억원배p]', s)]
    profile = get_profile(industry_key) if industry_key else {}
    industry_keywords = profile.get("keywords", [])

    neg_words = ["하락", "감소", "위험", "우려", "리스크", "손실", "악화", "둔화"]
    pos_words = ["상승", "증가", "개선", "기회", "확대", "성장", "호전", "강화"]
    action_words = ["점검", "확인", "검토", "조정", "대비", "모니터링", "준비"]

    def _pick(keyword_list, fallback_idx=0):
        for s in sentences_with_numbers:
            if any(kw in s for kw in keyword_list):
                return s[:80]
        for s in sentences:
            if any(kw in s for kw in keyword_list):
                return s[:80]
        if sentences_with_numbers:
            return sentences_with_numbers[min(fallback_idx, len(sentences_with_numbers) - 1)][:80]
        if sentences:
            return sentences[min(fallback_idx, len(sentences) - 1)][:80]
        return title[:80] if title else "분석 정보 부족"

    impact_text = _pick(industry_keywords, 0)
    risk_text = _pick(neg_words, 1)
    opp_text = _pick(pos_words, 2)
    action_text = _pick(action_words, 3)

    return {
        "impact": impact_text,
        "risk": risk_text,
        "opportunity": opp_text,
        "action": action_text,
    }


def _extract_topic_from_title(title: str) -> str:
    """기사 제목에서 핵심 명사구 토픽 추출 (템플릿 {context} 삽입용).

    V8.1: 20자 앞부분 잘림 → 핵심 명사구 추출로 개선.
    - 동사/조사 제거하여 "투자자 보호" 같은 명사구만 추출
    - 템플릿 "유통 파트너 {context} ESG 기준..." 에 자연스럽게 삽입
    """
    cleaned = re.sub(r'\[[\d.]+\]\s*', '', title)  # [2026.03] 제거
    cleaned = re.sub(r'[★⭐]+\s*', '', cleaned)     # 별표 제거
    cleaned = cleaned.strip()

    # 한글 명사구 추출: 2음절 이상 단어 중 동사/조사 종결 제거
    _VERB_ENDINGS = frozenset(["하다", "되다", "이다", "있다", "없다", "한다", "된다"])
    _PARTICLES = frozenset(["위해", "통해", "대해", "따라", "의해", "인해"])
    # V10.1: _TITLE_STOPWORDS 통합 사용 (topic 추출 시에도 동일 기준 적용)
    _STOP = _TITLE_STOPWORDS | frozenset([
        "경제", "동향", "현황", "대책", "한국", "산업", "사회", "문제",
        "보도자료", "조성한다", "연계한",
    ])

    # 숫자+콤마 패턴 먼저 처리 (5,000 → 5000), 그 후 구두점 정리
    cleaned_text = re.sub(r'(\d),(\d)', r'\1\2', cleaned)
    cleaned_text = re.sub(r'[,·""''「」『』]', ' ', cleaned_text)
    words = re.findall(r'[가-힣A-Za-z0-9]+', cleaned_text)
    words = [w for w in words if len(w) >= 2]
    # V12: 날짜/수치 혼합 토큰 제거 ("3월", "12일", "2026년" 등) — T-01 키워드 오염 방지
    # T-09: 더 긴 단위 복합어도 제거 ("7개월분", "10개국", "5분기" 등)
    _LONGER_UNIT_PAT = re.compile(
        r'^\d+(?:개월분?|분기|개국|개사|개기|개항|억불|만불|천불|만개|억개|개소|개년|개월)'
    )
    words = [w for w in words if not re.match(r'^\d+[가-힣]$', w) and
             not re.match(r'^[가-힣]\d+$', w) and
             not re.match(r'^\d+[가-힣]{1,2}$', w) and   # "3월", "12일", "1분기" 등
             not _LONGER_UNIT_PAT.match(w)]               # T-09: "7개월분", "5분기", "10개국" 등
    # 의미 있는 명사/명사구 필터
    nouns = []
    for w in words:
        if w in _STOP or w in _PARTICLES:
            continue
        if any(w.endswith(v[-2:]) for v in _VERB_ENDINGS):
            continue
        # 2자 이상 조사로 끝나는 패턴 정리 (에서/으로/부터 등)
        for suffix in ["에서", "으로", "부터", "까지", "처럼"]:
            if w.endswith(suffix) and len(w) > len(suffix) + 1:
                w = w[:-len(suffix)]
                break
        # 1자 조사로 끝나는 단어 정리 (이/가/를/은/는/의/에/도/로)
        if len(w) > 2 and w[-1] in "이가를은는의에도로":
            w = w[:-1]
        # "~일" 접미 제거 (혁명일 → 혁명)
        if len(w) > 2 and w[-1] == "일" and w[-2] not in "0123456789":
            w = w[:-1]
        if len(w) >= 2:
            nouns.append(w)

    if not nouns:
        # fallback: 앞 12자
        return cleaned[:12].strip()

    # 상위 2개 명사를 합쳐 토픽 구성 (최대 12자)
    topic = nouns[0]
    if len(nouns) > 1 and len(topic) + len(nouns[1]) + 1 <= 12:
        topic = f"{nouns[0]} {nouns[1]}"
    return topic[:12]


def _fill_ctx(template: str, topic: str) -> str:
    """V9: 템플릿 {context} 치환 — 의미 호환성 검증 후 삽입.

    기사 토픽이 템플릿 맥락과 무관할 때 삽입하면 텍스트가 오염되므로,
    토픽이 템플릿의 주변 단어와 의미적으로 호환되는지 검증합니다.
    비호환 시 {context}를 빈 문자열로 대체하여 자연스러운 문장을 유지합니다.
    """
    if not topic or "{context}" not in template:
        return template.replace("{context}", "").replace("  ", " ").strip()

    # 템플릿에서 {context} 주변 단어 추출 (앞뒤 2단어)
    _ctx_idx = template.find("{context}")
    _before = template[:_ctx_idx].strip()
    _after = template[_ctx_idx + len("{context}"):].strip()

    # 토픽 단어 추출
    _topic_words = set(re.findall(r'[가-힣]{2,}', topic))

    # 템플릿 자체 키워드 추출 (산업 전문 용어)
    _tmpl_words = set(re.findall(r'[가-힣]{2,}', _before + " " + _after))

    # 호환성 검증: 토픽 단어 중 최소 1개가 템플릿 맥락과 관련 있어야 함
    # 또는 토픽이 매우 일반적(2자 이하)이면 삽입 허용
    _GENERIC_TOPICS = frozenset([
        "투자자 보호", "중국 희토류", "뉴스특보", "기업 거버넌스",
        "코스피", "주가 상승", "자사주 마법", "하이퍼로컬",
        "동네책방", "최고가격제", "투자자 보호 강화", "거버넌스 개선",
        "중국이 희토류", "희토류 수출", "전세대출", "서민 주거",
    ])

    # V9.3: 토픽이 너무 길면 삽입하지 않음 (12자 초과 = 추출 오류 가능성)
    if len(topic) > 12:
        result = template.replace("{context}", "").replace("  ", " ").strip()
        return result

    # 명확히 무관한 토픽이면 삽입하지 않음
    if topic in _GENERIC_TOPICS:
        result = template.replace("{context}", "").replace("  ", " ").strip()
        return result

    # 토픽 단어가 템플릿 키워드와 1개라도 겹치면 호환으로 판단
    _overlap = _topic_words & _tmpl_words
    if _overlap:
        # V9.5: {context} 바로 앞뒤에 토픽 내 동일 단어가 있으면 중복 방지
        _before_words = _before.split()
        _after_words = _after.split()
        _adjacent = set()
        if _before_words:
            _adjacent.add(_before_words[-1])
        if _after_words:
            _adjacent.add(_after_words[0])
        _dup = _topic_words & _adjacent
        if _dup:
            # 토픽에서 중복 단어 제거 후 삽입
            _clean_topic = topic
            for dw in _dup:
                _clean_topic = _clean_topic.replace(dw, "").strip()
            if len(_clean_topic) >= 2:
                result = template.replace("{context}", _clean_topic)
            else:
                result = template.replace("{context}", "").replace("  ", " ").strip()
                return result
        else:
            result = template.replace("{context}", topic)
        result = result.replace("{" + topic + "}", topic)
        return result

    # 겹침 없어도, 토픽이 짧으면(4자 이하) 삽입 허용 (맥락 보조 역할)
    if len(topic) <= 4:
        result = template.replace("{context}", topic)
        result = result.replace("{" + topic + "}", topic)
        return result

    # 비호환: {context}를 제거하고 자연스러운 문장 유지
    result = template.replace("{context}", "").replace("  ", " ").strip()
    return result


# V10: 중앙화된 제목 키워드 stopwords (5개 함수 공유)
# V12: 날짜·지명·범용 국가명 추가 (QA T-01 수정)
_TITLE_STOPWORDS = frozenset({
    "우리", "이번", "대한", "관련", "통해", "위해", "대비", "이상", "이하", "현재",
    "가능", "경우", "하면", "중국이", "미국이", "일본이", "투자자",
    "보호", "강화", "개선", "기업", "거버넌스", "확대", "변화", "영향",
    "필요", "전망", "지속", "가능성", "대응", "분석", "정책", "글로벌",
    "본격", "시행", "임박", "원대", "일시적", "구조적", "새로운", "사상",
    "최대", "급등", "급락", "돌파", "호조", "가중", "시급", "우려",
    # V10: 기관/행위자 + 부사/관형어 + 일반 동사 추가
    "산업부", "정부", "당국", "각국", "지역별", "특화", "연계한", "조성한다",
    "민관", "합동", "개시", "기반", "올해", "내년", "분야", "방안",
    "전체", "주요", "최근", "향후", "국내외", "국내", "해외",
    "발표", "추진", "검토", "마련", "확인", "예상", "전망한다",
    "조사", "실시", "지원", "활용", "적용", "도입",
    # V12: 날짜·지명·범용 국가명 (QA T-01)
    "베트남", "서울", "중국", "미국", "유럽", "일본", "인도", "독일",
    "베이징", "상하이", "워싱턴", "도쿄", "브뤼셀",
    "국내외", "지방", "지역",
    # 월·일 단독 표현 (수치와 결합한 형태는 regex로 별도 처리)
    "이달", "지난달", "다음달", "올해말", "연말", "연초", "하반기", "상반기",
})


def _extract_title_keywords(title: str, industry_extended_kw: list | None = None) -> list:
    """V10: 제목에서 핵심 키워드 추출 — 산업 관련 키워드 우선.

    반환: [(keyword, priority), ...] priority: 0=산업직접, 1=3자이상구체명사, 2=일반
    """
    import re as _re
    _raw = [w for w in _re.findall(r'[가-힣]{2,}', title) if len(w) >= 2 and w not in _TITLE_STOPWORDS]
    if not _raw:
        return []

    _ext_kw_set = set(industry_extended_kw) if industry_extended_kw else set()
    _prioritized = []
    for w in _raw:
        if w in _ext_kw_set:
            _prioritized.append((w, 0))  # 산업 직접 매칭
        elif len(w) >= 3:
            _prioritized.append((w, 1))  # 3자+ 구체 명사
        else:
            _prioritized.append((w, 2))  # 일반
    _prioritized.sort(key=lambda x: x[1])
    return _prioritized


def _best_keyword(title: str, industry_extended_kw: list | None = None) -> str:
    """V10: 제목에서 가장 적합한 단일 키워드 반환."""
    _kws = _extract_title_keywords(title, industry_extended_kw)
    if _kws:
        return _kws[0][0][:8]
    return ""


def _classify_article_theme(title: str) -> str:
    """V10: 기사 제목에서 정책/시장 테마 분류 — Questions/Checklist 동적 선택용.
    V13: '글로벌' 테마 추가 — K-뷰티·해외진출·박람회 기사 전용.
    V14: 수급/통상 패턴 확장, 금융 오분류(달러 단독) 방지, 검사 순서 최종 최적화.
    - 수급: 지연/차질/납품 추가 → ASML 납품 지연 등 정확 분류
    - 통상: 공급망/MOU/협약 추가 → 한일 공급망 협력 등 정확 분류
    - 금융: 단독 '달러' 제거 → 수출 실적 기사 오분류 방지
    - 글로벌: 수출 활성화/수출지원/수출 간담회 추가 → 지역 수출 행사 기사 분류
    - 검사 순서: 자원 → 규제 → 글로벌 → 통상 → 수급 → 기술 → 금융
      · 자원 최우선: '희토류 수출 규제'가 통상으로 오분류되는 것 방지
      · 규제 > 글로벌: 'K-푸드 간담회서 규제 개선' 등 규제 내용이 핵심인 기사 정확 분류
      · 통상 > 수급: 공급망 MOU 기사가 '공급'으로 오분류되는 것 방지
    """
    _theme_kw = {
        # 자원: 희토류·광물·에너지 — 가장 먼저 체크 (특수 자원 키워드, 통상 '수출 규제'와 충돌 방지)
        "자원": ["원자재", "광물", "희토류", "리튬", "철광석", "유가", "원유", "자원안보", "비축", "핵심광물"],
        # 규제: 환경·인증·ESG 규제 — 글로벌보다 먼저 (K-푸드 기사에서 '규제 개선'이 핵심인 경우)
        "규제": [
            "CBAM", "탄소", "배출", "규제", "인증", "강제노동", "ESG", "환경",
            "포장재", "포장 규제", "라벨링", "원산지",
        ],
        # 글로벌: K-뷰티·해외판로·수출 행사
        "글로벌": [
            "K-뷰티", "K뷰티", "K-푸드", "K푸드", "글로벌 판로", "해외 진출", "박람회",
            "CIBE", "Cosmoprof", "팝업스토어", "팝업 오픈", "글로벌 공략", "수출 상담",
            "판로 확장", "글로벌 안착", "해외 바이어", "ODM 투자", "ODM 생산", "ODM 제조",
            "인프라에 돈", "인프라 투자",
            # V14 추가: 지역 수출 행사 (공백 유무 모두 커버)
            "수출 활성화", "수출 지원", "수출지원", "수출 촉진", "수출 간담회", "수출 상담회",
            "바이어 유치", "판로 개척", "시장 개척",
        ],
        # 통상: 관세·무역·공급망 협력
        "통상": [
            "관세", "무역", "수출통제", "수출 규제", "반덤핑", "FTA", "통상", "301조", "수입 규제",
            # V14 추가: 공급망·협력 체계
            "공급망 협력", "공급망 재편", "공급망 다변화", "공급망 강화",
            "MOU 체결", "협약 체결", "협력 강화", "공급망 구축",
        ],
        # 수급: 수요/공급/지연/차질
        "수급": [
            "수요", "공급", "수주", "재고", "수급", "과잉", "부족",
            # V14 추가: 지연·차질 키워드 (ASML 납품 지연 등)
            "납품 지연", "납품 차질", "공급 차질", "배송 지연", "물량 부족",
            "생산 차질", "수급 차질", "출하 지연",
        ],
        # 기술: AI·반도체·첨단기술
        "기술": ["AI", "양자", "클러스터", "HBM", "파운드리", "디지털", "기술패권",
                 "반도체 기술", "첨단 기술", "기술 전환", "기술 혁신"],
        # 금융: 환율·금리·통화 — '달러' 단독 제거
        "금융": ["환율", "금리", "물가", "인플레이션", "통화", "외환", "채권",
                 "달러 강세", "달러 약세", "달러 환율", "원달러"],
    }
    for theme, kws in _theme_kw.items():
        if any(kw in title for kw in kws):
            return theme
    return "일반"


# =============================================================================
# QUALITY TIER — 본문 품질 계층화 (Item 1)
# FULL: body≥300자, BRIEF: 50~299자, MINIMAL: <50자 or empty
# =============================================================================

def _assess_body_quality(text: str) -> str:
    """본문 길이 기반 품질 계층 반환.

    Returns:
        'full'    — 300자 이상 → 전체 분석
        'brief'   — 50~299자 (RSS summary) → 축약 분석
        'minimal' — 50자 미만 또는 빈 본문 → 최소 분석 (과도한 주장 금지)
    """
    n = len((text or "").strip())
    if n >= 300:
        return "full"
    if n >= 50:
        return "brief"
    return "minimal"


def _build_minimal_fallback(title: str, industry_key: str) -> dict:
    """Item 1: MINIMAL 계층 — 본문 없을 때 과도한 분석 대신 최소 요약.

    '기사 원문을 확보하지 못해 핵심 해석 제한됨'을 명시하고
    제목에서 추출 가능한 사실만 제공합니다.
    """
    _label = _resolve_industry_label(industry_key)
    # 이 함수는 _extract_article_events 이후에 정의되므로 지연 호출
    _ev = _extract_article_events(title, "")
    _ev_topic = _extract_event_topic(title)

    co_str  = "·".join(_ev["companies"][:1]) if _ev["companies"] else ""
    mkt_str = _ev["markets"][0]              if _ev["markets"]   else ""
    evt_str = _ev["events"][0]               if _ev["events"]    else ""

    _fact_parts = []
    if co_str:  _fact_parts.append(f"**{co_str}**")
    if mkt_str: _fact_parts.append(f"**{mkt_str}** 시장")
    if evt_str: _fact_parts.append(f"**{evt_str}**")
    if not _fact_parts and _ev_topic:
        _fact_parts.append(f"**{_ev_topic}**")

    _fact_str = " ".join(_fact_parts) if _fact_parts else f"**{title[:20]}**"
    _notice = "⚠️ 기사 원문 미확보 — 제목 기반 최소 요약입니다. 원문 확인 후 판단하세요."

    return {
        "impact":      f"{_notice}\n\n{_fact_str} 동향이 **{_label}** 기업 환경에 영향을 미칠 수 있습니다. 구체적 영향 분석은 원문 확인 후 가능합니다.",
        "risk":        f"원문 미확보로 리스크 세부 분석이 제한됩니다. {_fact_str} 관련 최신 동향을 원문에서 직접 확인하세요.",
        "opportunity": f"원문 미확보로 기회 세부 분석이 제한됩니다. 원문에서 **{_label}** 관련 시사점을 직접 확인하세요.",
        "action":      f"• 원문 기사 직접 확인 (원문 보기 링크 클릭)\n• {_label} 내부 담당자에게 {_fact_str} 동향 공유\n• 추가 정보 확보 후 전략 검토",
        "headline":    _ev_topic or title[:30],
        "body_tier":   "minimal",
        "analysis_source": "minimal",
        "questions":   f"• {_fact_str} 동향이 우리 {_label} 사업에 미치는 단기 영향은?\n• 원문 확인 후 구체적 대응이 필요한가?",
        "checklist":   f"• 원문 기사 URL 직접 접속 확인\n• {_label} 담당자 공유 여부 점검\n• 1주일 내 추가 정보 수집 일정 확인",
    }


# V15: snippet 카드 생성 (본문 50~119자 — Google News 스니펫 수준)
# 기존 smart_fallback의 generic Risk/Opp 생성을 방지하고
# 제목 기반 Impact 1문장 + "본문 정보 부족" 명시로 대체
def _build_snippet_card(title: str, industry_key: str, body_text: str = "") -> dict:
    """V15: snippet 모드 카드 — 본문이 120자 미만일 때 generic 템플릿 대신 명시적 한계 표시."""
    _label = _resolve_industry_label(industry_key)
    _ev = _extract_article_events(title, body_text or "")
    _ev_topic = _extract_event_topic(title)

    co_str  = "·".join(_ev["companies"][:1]) if _ev["companies"] else ""
    mkt_str = _ev["markets"][0]              if _ev["markets"]   else ""
    num_str = _ev["numbers"][0]              if _ev.get("numbers") else ""

    # Impact: 제목 기반 1문장 (기업명/수치/시장 최대 활용)
    _fact_parts = []
    if co_str:  _fact_parts.append(f"**{co_str}**")
    if mkt_str: _fact_parts.append(f"**{mkt_str}** 시장")
    if num_str: _fact_parts.append(f"({num_str})")
    if not _fact_parts and _ev_topic:
        _fact_parts.append(f"**{_ev_topic}**")
    _fact_str = " ".join(_fact_parts) if _fact_parts else f"**{title[:25]}**"

    _impact = (
        f"{_fact_str} 동향이 **{_label}** 기업 환경에 영향을 미칠 수 있습니다. "
        f"원문 전체 확인 후 구체적 영향을 파악하세요."
    )

    return {
        "impact":          _impact,
        "risk":            "기사 본문 정보 부족 — 원문 확인 후 리스크 판단 필요",
        "opportunity":     "기사 본문 정보 부족 — 원문 확인 후 기회 요소 파악 필요",
        "action":          f"• 관련 산업 영향 추가 확인 권장\n• 원문 기사 직접 확인 (원문 보기 링크)\n• {_label} 담당자에게 동향 공유",
        "headline":        _ev_topic or title[:30],
        "body_tier":       "snippet",
        "analysis_source": "snippet",
        "questions":       f"• {_fact_str} 동향이 우리 {_label} 사업에 미치는 단기 영향은?\n• 원문 확인 후 대응 계획이 필요한가?",
        "checklist":       f"• 원문 기사 URL 직접 접속\n• {_label} 실무 담당자 공유 여부 확인\n• 추가 관련 기사 검색 및 정보 보완",
    }


def _determine_analysis_mode(body_len: int) -> str:
    """V15: 본문 길이 기반 분석 모드 결정."""
    if body_len < 120:
        return "snippet"
    elif body_len < 300:
        return "partial_body"
    else:
        return "full_body"


def _log_card_generation(title: str, analysis_source: str, body_length: int,
                         industry_key: str = "일반") -> None:
    """V15: 카드 생성 로그 — logs/card_generation.log에 기록."""
    import os
    try:
        _log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
        os.makedirs(_log_dir, exist_ok=True)
        _log_path = os.path.join(_log_dir, "card_generation.log")
        from datetime import datetime as _dt
        _ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        _line = (
            f"[{_ts}] title={title[:50]!r} "
            f"industry={industry_key} "
            f"body_length={body_length} "
            f"analysis_source={analysis_source}\n"
        )
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(_line)
    except Exception as _e:
        print(f"[summarizer] ⚠️ 카드 로그 기록 실패: {_e}")


# =============================================================================
# Item 2: 핵심 변화 이벤트 토픽 추출 (_topic 재설계)
# 기존 _extract_topic_from_title은 회사명/표면 키워드 반환 → 이벤트 사건구 추출로 전환
# =============================================================================

def _get_market_prefix(src: str) -> str:
    """제목에서 시장/국가명 앞 접두사 추출 (이벤트 토픽 조합용)."""
    m = re.search(
        r'(브라질|하노이|호치민|베트남|미국|중국|유럽|일본|동남아|중동|인도|싱가포르|'
        r'광저우|뉴욕|런던|방콕|두바이|파리|베이징|상하이|도쿄|말레이시아|필리핀)',
        src
    )
    return f"{m.group(0)} " if m else ""


def _extract_event_topic(title: str) -> str:
    """Item 2: 기사 핵심 변화 이벤트를 명사구 형태로 추출.

    우선순위:
      1) 이벤트 키워드 + 시장 조합
      2) 시장/국가 + 뒤따르는 명사구
      3) 기존 _extract_topic_from_title fallback

    Examples:
      'KGC인삼공사, 브라질 규제 개선으로 수출 간담회' → '브라질 규제 개선'
      'B the B 하노이 상설매장 입점기업 모집'         → '하노이 상설매장 입점'
      'ODM 제조 인프라에 돈 몰린다'                   → 'ODM 제조 인프라 투자'
    """
    _cleaned = re.sub(r'\[[\d.]+\]\s*|[★⭐]+\s*', '', title).strip()

    # 1) 이벤트 패턴 + 시장 prefix
    # V14: 납품 지연/공급망/추가 관세/수출 활성화 등 패턴 추가
    _EVT_PATTERNS = [
        # 규제·관세 이벤트 (V14: 추가 관세, 관세 폭탄 포함)
        (r'(규제\s*개선|규제\s*완화|관세\s*인상|관세\s*부과|관세\s*폭탄|추가\s*관세|통상\s*압박|수출\s*규제)', True),
        # 수급·지연 이벤트 (V14 신규)
        (r'(납품\s*지연|납품\s*차질|공급\s*차질|배송\s*지연|물량\s*부족|생산\s*차질|수급\s*차질)', False),
        # 공급망·협력 이벤트 (V14 신규)
        (r'(공급망\s*협력|공급망\s*재편|공급망\s*다변화|공급망\s*강화)', False),
        # 글로벌 판로·바이어 이벤트
        (r'(상설매장|입점\s*모집|수출\s*간담회|판로\s*개척|시장\s*진출|수출\s*상담)', True),
        # 수출 활성화·지원 행사 (V14 신규)
        (r'(수출\s*활성화|수출\s*지원\s*행사|수출\s*촉진\s*행사|수출\s*상담회)', True),
        # ODM·제조·전시
        (r'(ODM\s*제조|ODM\s*투자|ODM\s*생산|인프라\s*투자|제조\s*인프라)', False),
        (r'(박람회|CIBE|Cosmoprof|전시회|팝업스토어)', True),
        # 계약·협약·MOU
        (r'(MOU\s*체결|공급망\s*MOU|소재\s*MOU|수출\s*계약|협약\s*체결|투자\s*협의)', True),
        # 투자·확대
        (r'(투자\s*확대|바이어\s*유치|판로\s*확장|수출\s*확대)', True),
    ]
    for pat, use_mkt_prefix in _EVT_PATTERNS:
        m = re.search(pat, _cleaned, re.IGNORECASE)
        if m:
            prefix = _get_market_prefix(_cleaned) if use_mkt_prefix else ""
            result = f"{prefix}{m.group(0).strip()}".replace('  ', ' ')
            return result[:20].strip()

    # 2) 시장 + 뒤따르는 명사구
    m2 = re.search(
        r'(브라질|하노이|호치민|베트남|미국|중국|유럽|일본|동남아|중동|인도|싱가포르|광저우|뉴욕|런던|방콕|두바이)',
        _cleaned
    )
    if m2:
        mkt = m2.group(0)
        after_words = re.findall(r'[가-힣A-Za-z]{2,}', _cleaned[m2.end():].strip())[:2]
        if after_words:
            return f"{mkt} {' '.join(after_words)}"[:20]
        return f"{mkt} 시장 동향"[:20]

    # 3) Fallback
    return _extract_topic_from_title(title)


# =============================================================================
# Item 3: 기사 고유 Anchor 키워드 강제 포함
# =============================================================================

def _extract_anchor_keywords(title: str, ev: dict | None = None) -> list:
    """Item 3: 기사 고유 anchor 키워드 추출 (최대 5개).

    우선순위: 기업명 > 시장명 > 이벤트KW > 수치 > 제목 고유어
    """
    anchors: list[str] = []
    if ev:
        anchors += [c for c in ev.get("companies", []) if c][:1]
        anchors += [m for m in ev.get("markets",   []) if m][:2]
        anchors += [e for e in ev.get("events",    []) if e][:1]
        anchors += [f for f in ev.get("figures",   []) if f][:1]

    # 제목에서 추가 고유어 (ev 커버 안 된 것)
    _HIGH_VALUE = re.compile(
        r'[가-힣]{3,}(?:공사|집중|통관|인증|협약|유치|개설|참가|개막)|ODM|K-뷰티|K뷰티|K-푸드|K푸드'
    )
    for m in _HIGH_VALUE.finditer(title):
        kw = m.group(0)
        if kw not in anchors and len(anchors) < 5:
            anchors.append(kw)

    return anchors[:5]


def _anchor_check(text: str, anchor_kws: list) -> bool:
    """anchor 키워드가 text에 1개 이상 포함됐는지 확인."""
    return any(kw in text for kw in anchor_kws)


def _inject_anchor_prefix(text: str, anchor_kws: list, label: str) -> str:
    """text에 anchor가 없으면 primary anchor 기반 도입 문구를 앞에 추가."""
    if not anchor_kws or _anchor_check(text, anchor_kws):
        return text
    primary = anchor_kws[0]
    return f"**{primary}** 관련 동향이 {label} 기업 전략에 영향을 미칩니다. {text}"


# =============================================================================
# Item 4: 세션 내 기사 간 중복 감지
# =============================================================================

_session_summary_hashes: dict = {}
_session_summary_lock = threading.Lock()


def _summary_fingerprint(text: str) -> str:
    """요약 텍스트 핑거프린트 (앞20자+뒤20자)."""
    t = re.sub(r'\s+|\*+', '', text or "")
    return t[:20] + t[-20:]


def _is_duplicate_summary(article_id: str, impact: str) -> bool:
    """Item 4: 다른 기사와 impact 앞부분이 동일하면 True (중복 감지)."""
    fp = _summary_fingerprint(impact)
    with _session_summary_lock:
        for aid, existing_fp in _session_summary_hashes.items():
            if aid == article_id:
                continue
            if existing_fp[:15] == fp[:15] and len(fp) > 10:
                return True
    return False


def _register_summary(article_id: str, impact: str) -> None:
    """세션 내 impact 핑거프린트 등록."""
    with _session_summary_lock:
        _session_summary_hashes[article_id] = _summary_fingerprint(impact)


def clear_session_summary_cache() -> None:
    """대시보드 새로고침 시 중복 감지 캐시 초기화."""
    with _session_summary_lock:
        _session_summary_hashes.clear()


# =============================================================================
# Item 5: 소비재·식품 서브카테고리 분류
# =============================================================================

_SUBCATEGORY_PATTERNS: dict = {
    "화장품·뷰티": [
        "뷰티", "화장품", "K-뷰티", "K뷰티", "ODM", "OEM", "코스메틱", "스킨케어",
        "메이크업", "헤어", "향수", "코스맥스", "한국콜마", "아모레", "LG생활건강",
        "에뛰드", "이니스프리", "마몽드", "더마", "기초화장품", "색조",
    ],
    "식품·음료": [
        "식품", "음료", "K-푸드", "K푸드", "농식품", "제과", "과자", "라면",
        "음식", "식재료", "유제품", "발효", "김치", "삼양", "오리온",
        "CJ제일제당", "농심", "빙그레", "롯데웰푸드", "인삼", "건기식",
    ],
    "생활용품": [
        "생활용품", "세제", "위생용품", "기저귀", "생리대", "치약", "칫솔",
        "샴푸", "바디워시", "생활화학", "청소용품", "주방", "욕실",
    ],
    "유통·브랜드": [
        "유통", "브랜드", "편의점", "마트", "백화점", "면세점", "이커머스",
        "온라인", "팝업", "쇼룸", "상설매장", "입점", "바이어",
        "수출상담", "간담회", "판로",
    ],
}


def _classify_subcategory(title: str, industry_key: str) -> str:
    """Item 5: 소비재·식품 내 서브카테고리 분류.

    Returns: '화장품·뷰티' | '식품·음료' | '생활용품' | '유통·브랜드' | ''
    """
    if industry_key not in ("소비재", "소비재·식품"):
        return ""
    scores = {k: sum(1 for kw in v if kw in title) for k, v in _SUBCATEGORY_PATTERNS.items()}
    best = max(scores, key=lambda c: scores[c])
    return best if scores[best] > 0 else "화장품·뷰티"


# =============================================================================
# V13: 기사별 이벤트 요소 추출 — event-first Impact 생성 기반
# =============================================================================

def _extract_article_events(title: str, text: str) -> dict:
    """기사 제목·본문에서 핵심 이벤트 요소(기업명·수치·시장·이벤트키워드) 추출.

    사용 목적: Impact/Risk/Opp 문장이 산업 템플릿으로만 생성되는 문제를 방지하기 위해
    기사별 고유 정보를 먼저 추출하고, 이를 산업 영향 변환의 재료로 사용한다.

    Returns:
        companies: list[str]  — 기업/브랜드명 (최대 2개)
        figures:   list[str]  — 핵심 수치 (최대 2개)
        markets:   list[str]  — 시장/지역/전시회 (최대 3개)
        events:    list[str]  — 핵심 이벤트 키워드 (최대 2개)
    """
    _src = title + " " + (text[:2000] if text else "")

    # ── 기업·브랜드명 ─────────────────────────────────────────────────────────
    _co_sfx = (
        r"코리아|그룹|전자|화학|자동차|식품|뷰티|바이오|제약|콜마|에너지|"
        r"중공업|건설|물산|테크|홀딩스|인터내셔널|글로벌|코스메틱|팜|앤컴퍼니"
    )
    _co_pat = re.compile(rf'[가-힣A-Za-z]{{2,8}}(?:{_co_sfx})')
    companies = list(dict.fromkeys(c for c in _co_pat.findall(_src) if len(c) >= 3))[:2]

    # V14 Fix 3: _co_sfx 패턴 미적용 주요 기업명 직접 인식
    # 현대차·SK하이닉스·ASML 등 suffix 없이도 인식 가능하도록 _KNOWN_ENTITIES 추가
    _KNOWN_ENTITIES_PAT = re.compile(
        r'(?:^|[\s\(\[,·])'
        r'(ASML|TSMC|NVIDIA|AMD|Intel|Qualcomm|Apple|Google|Microsoft|'
        r'현대차|기아차|현대모비스|현대위아|현대오토에버|포스코|POSCO|'
        r'SK하이닉스|LG디스플레이|LG이노텍|LG화학|현대중공업|대우조선|한화오션|'
        r'KGC인삼공사|KGC|코리아나화장품|신화코리아|아모레퍼시픽|코스맥스|한국콜마|'
        r'지리자동차|BYD|테슬라|도요타|폭스바겐|스텔란티스|'
        r'엑손모빌|쉐브론|토탈에너지|삼성SDI|SK온|에코프로|포스코퓨처엠)'
        r'(?=$|[\s\)\],·:·])'
    )
    for _m in _KNOWN_ENTITIES_PAT.finditer(_src):
        _kh = _m.group(1)
        if _kh not in companies and len(companies) < 2:
            companies.append(_kh)
    # 제목 작은따옴표·꺾쇠 속 브랜드명 추출 ('앰플엔'→앰플엔)
    # 관용구·감탄 표현 등 false positive 제외
    _BRAND_STOP_WORDS = frozenset([
        "미소", "활짝", "웃음", "성공", "탄탄", "우뚝", "날개", "불꽃", "폭발",
        "급등", "주목", "강세", "약세", "호조", "부진", "변화", "이슈", "사태",
    ])
    _brand_hits = re.findall(
        r"['\u2018\u2019\u201c\u201d][가-힣A-Za-z0-9·\s]{2,12}['\u2018\u2019\u201c\u201d]",
        title,
    )
    for b in _brand_hits:
        clean_b = b.strip("'\u2018\u2019\u201c\u201d ")
        # 띄어쓰기 포함이면 첫 단어만 추출 (예: '미소 활짝' → '미소' 제외)
        _first_word = clean_b.split()[0] if ' ' in clean_b else clean_b
        if _first_word in _BRAND_STOP_WORDS:
            continue
        # 순수 형용사/부사성 1-2자 단어는 제외
        if len(clean_b.replace(' ', '')) <= 2:
            continue
        if clean_b not in companies and len(companies) < 2:
            companies.append(clean_b)

    # ── 핵심 수치 ──────────────────────────────────────────────────────────────
    _fig_pat = re.compile(
        r'\d+(?:\.\d+)?(?:억\s*달러|만\s*달러|백만\s*달러|억\s*원|조\s*원|만\s*원|'
        r'%p?|배|개국|개사|만\s*명|천\s*명|억\s*개|만\s*개|만\s*달러)'
    )
    figures = list(dict.fromkeys(_fig_pat.findall(_src)))[:2]

    # ── 시장·지역·전시회 ───────────────────────────────────────────────────────
    # Fix A: 동남아·중동·중남미 주요 도시 추가 (하노이·호치민 누락 문제 해결)
    _mkt_kws = (
        r"미국|중국|일본|유럽|동남아|인도|중동|브라질|호주|캐나다|베트남|태국|인도네시아|말레이시아|필리핀|"
        r"광저우|베이징|상하이|홍콩|싱가포르|뉴욕|런던|두바이|파리|도쿄|성수|"
        r"하노이|호치민|방콕|쿠알라룸푸르|자카르타|마닐라|뭄바이|상파울루|멕시코시티|"
        r"CIBE|CES|SIAL|Cosmoprof|박람회|전시회"
    )
    _mkt_pat = re.compile(rf'(?:{_mkt_kws})')
    markets = list(dict.fromkeys(_mkt_pat.findall(_src)))[:3]

    # ── 핵심 이벤트 키워드 ────────────────────────────────────────────────────
    # Fix A: 상설매장·입점·쇼룸·간담회 추가 (B the B 하노이 상설매장 입점 누락 해결)
    _evt_kws = (
        r"수출 상담|ODM 투자|ODM 생산|ODM 제조|글로벌 판로|수출 판로|해외 진출|"
        r"시장 진출|투자 집중|자금 집중|MOU 체결|수출 계약|납품 계약|글로벌 공략|"
        r"성공적 안착|안착|팝업 오픈|팝업 개설|팝업스토어|팝업 출점|공략 시동|"
        r"인프라 투자|수출 확대|인프라에 돈|"
        r"상설매장|입점|쇼룸|전시관|간담회|수출 간담회|수출 협의|규제 개선|판로 개척"
    )
    _evt_pat = re.compile(rf'(?:{_evt_kws})')
    events = list(dict.fromkeys(_evt_pat.findall(_src)))[:2]

    return {
        "companies": companies,
        "figures":   figures,
        "markets":   markets,
        "events":    events,
    }


def _ko_subj(s: str) -> str:
    """P2: 한국어 주격조사 자동 선택 — 마지막 음절의 받침 유무로 '이'/'가' 반환.

    Korean subject particle:
      - 받침 있음(closed syllable) → '이'
      - 받침 없음(open syllable) 또는 로마자 모음 끝 → '가'
    """
    if not s:
        return "이"
    last = s.rstrip()[-1] if s.rstrip() else s[-1]
    if '가' <= last <= '힣':
        code = ord(last) - 0xAC00
        return "이" if code % 28 != 0 else "가"
    if last.isalpha():
        return "가" if last.lower() in "aeiou" else "이"
    # 숫자·기호로 끝나면 '이' (예: 3이 → 3이)
    return "이"


def _build_event_lead(ev: dict, title: str, label: str) -> str:
    """추출 이벤트 요소로 기사별 고유 도입 문장 생성.

    우선순위: 기업+수치 > 기업+시장 > 기업+이벤트 > 시장+수치 > 기업 단독
    최소 2개 이상의 요소가 있어야 호출됨 (_specifics_count >= 2 보장).
    P2: _ko_subj() 사용으로 이/가 조사 문법 자동 교정.
    """
    co  = ev["companies"]
    fig = ev["figures"]
    mkt = ev["markets"]
    evt = ev["events"]
    co_str  = "·".join(co[:2]) if co else ""
    fig_str = fig[0] if fig else ""
    mkt_str = "·".join(mkt[:2]) if mkt else ""
    evt_str = evt[0] if evt else ""

    if co_str and fig_str and mkt_str:
        return f"{co_str}{_ko_subj(co_str)} {mkt_str}에서 {fig_str} 규모의 성과·투자를 달성했습니다."
    if co_str and fig_str:
        return f"{co_str} 관련 {fig_str} 규모 변화가 {label} 산업에서 확인됩니다."
    if co_str and mkt_str and evt_str:
        return f"{co_str}의 {mkt_str} {evt_str}{_ko_subj(evt_str)} 가시화되고 있습니다."
    if co_str and mkt_str:
        return f"{co_str}의 {mkt_str} 시장 진출·확장이 확인됩니다."
    if co_str and evt_str:
        return f"{co_str}의 {evt_str}{_ko_subj(evt_str)} {label} 시장에서 주목받고 있습니다."
    if mkt_str and fig_str:
        return f"{mkt_str}에서 {fig_str} 규모의 변화가 확인됩니다."
    if mkt_str and evt_str:
        return f"{mkt_str} {evt_str}{_ko_subj(evt_str)} 진행 중입니다."
    if co_str:
        return f"{co_str} 관련 동향이 {label} 기업의 사업 환경 변화에 시사점을 제공합니다."
    return ""


# V13: 이벤트 도입 + 짧은 산업영향 결론문 (event-first Impact 2번째 문장용)
_SMART_IMPACT_CONCLUSIONS: dict[str, str] = {
    "통상":   "**{label}** 수출기업의 관세·통상 리스크와 대체 시장 전략을 즉시 재점검해야 합니다.",
    "자원":   "**{label}** 기업의 원자재 조달 비용과 생산 원가 구조에 직접 영향이 예상됩니다.",
    "금융":   "**{label}** 기업의 수출 결제 마진과 환헤지 포지션을 현 시점에서 재점검해야 합니다.",
    "기술":   "**{label}** 기업의 제품 경쟁력과 기술 포지션에 중기적 구조 변화가 예상됩니다.",
    "수급":   "**{label}** 기업의 수주·가동률·재고 계획에 즉각적인 대응 검토가 필요합니다.",
    "규제":   "**{label}** 기업의 수출 인증 요건과 규제 준수 비용·일정을 확인해야 합니다.",
    "글로벌": (
        "**{label}** 기업의 글로벌 채널 전략·해외 바이어 확보 경쟁이 가속화되고 있으며, "
        "K-브랜드 포지셔닝과 수출 판로 전략의 즉각적인 재검토가 요구됩니다."
    ),
    "일반":   (
        "**{label}** 기업의 수출·사업 전략에 미치는 단중기 영향을 구체적으로 파악하고 "
        "전략적 대응 시점을 결정해야 합니다."
    ),
}

# P3: 글로벌 이벤트 서브타입별 결론 분화
# 이벤트 키워드에 따라 ODM·전시/박람회·팝업·수출판로 4가지 서브타입으로 세분화
_GLOBAL_SUBTYPE_CONCLUSIONS: dict[str, str] = {
    # ODM 관련 (제조·생산·투자 포함)
    "odm": (
        "**{label}** 기업의 ODM 파트너십 경쟁력·제조 단가·납품 조건이 "
        "글로벌 바이어 확보의 핵심 변수로 부상하고 있습니다."
    ),
    # 전시회·박람회·CIBE·Cosmoprof 관련
    "expo": (
        "**{label}** 기업의 전시·박람회 채널 전략이 해외 바이어 첫 접점으로 "
        "직결되며, 전시 퍼포먼스가 중기 수주·수출 계약에 직접 영향을 줍니다."
    ),
    # 팝업스토어·팝업 오픈 관련
    "popup": (
        "**{label}** 기업의 팝업·체험 마케팅이 현지 소비자 인지도 확보의 "
        "전략적 투자로 자리잡으며, 브랜드 현지화 비용과 ROI 검증이 요구됩니다."
    ),
    # 수출 상담·판로 확장·해외 진출 일반
    "export": (
        "**{label}** 기업의 해외 바이어 다변화·수출 채널 확장이 "
        "중기 성장을 결정하는 핵심 과제로 부각되고 있습니다."
    ),
}


def _classify_global_subtype(ev: dict, title: str) -> str:
    """P3: 이벤트 키워드와 제목으로 글로벌 서브타입 분류.

    Returns: 'odm' | 'expo' | 'popup' | 'export'
    """
    _src = title + " " + " ".join(ev.get("events", []))
    if re.search(r'ODM|제조|생산|OEM|위탁|납품', _src):
        return "odm"
    if re.search(r'박람회|CIBE|Cosmoprof|SIAL|전시|CES|전시회|컨퍼런스', _src, re.IGNORECASE):
        return "expo"
    if re.search(r'팝업|팝 업|pop.?up', _src, re.IGNORECASE):
        return "popup"
    return "export"


# V10: 테마별 질문/체크리스트 풀 (questions_frame 고정 사용 대신 동적 선택)
_THEME_QUESTIONS = {
    "통상": [
        "이번 무역 정책 변화가 우리 수출 물량과 마진에 미치는 영향은?",
        "관세/규제 변동 시 대체 시장·바이어 확보 전략은 준비됐는가?",
        "공급망 재편에 따른 거래처 이전 비용과 리드타임 변화는?",
    ],
    "자원": [
        "핵심 원자재 조달 가격이 우리 원가 구조에 미치는 영향은?",
        "특정국 자원 의존도를 줄이기 위한 대체 조달 경로는 확보됐는가?",
        "원자재 가격 변동에 대한 헤징 포지션은 충분한가?",
    ],
    "금융": [
        "환율/금리 변동이 수출 결제 및 자본 조달 비용에 미치는 영향은?",
        "현재 환헤지 비율과 만기 구조는 적정한가?",
        "유동성 리스크 관리 계획은 시나리오별로 준비됐는가?",
    ],
    "기술": [
        "이 기술 변화가 우리 제품 경쟁력과 시장 포지션에 미치는 영향은?",
        "기술 투자·R&D 로드맵을 이번 변화에 맞춰 재점검할 필요는?",
        "기술 표준·특허 경쟁에서 우리의 위치는 적절한가?",
    ],
    "수급": [
        "수요/공급 변동이 우리 생산 가동률과 재고에 미치는 영향은?",
        "가격 변동 시 고객사 전가 가능성과 마진 방어 전략은?",
        "수급 불균형 장기화 시 사업 구조 조정 방안은 준비됐는가?",
    ],
    "규제": [
        "이번 규제 변화의 우리 사업 적용 범위와 시행 시점은?",
        "규제 준수 비용과 타임라인을 이미 예산에 반영했는가?",
        "규제 대응이 경쟁사 대비 빠른가, 느린가?",
    ],
    "일반": [
        "이 변화가 우리 사업의 매출과 이익에 미치는 단기 영향은?",
        "경쟁사 대비 우리의 대응 속도는 적절한가?",
        "리스크와 기회 양면에서 전략적 포지셔닝은 준비됐는가?",
    ],
}

_THEME_CHECKLIST = {
    "통상": [
        "주요 수출 시장 관세·무역 정책 변경 사항 모니터링",
        "수출 물량 및 단가 변동 추이 주간 점검",
        "대체 시장·바이어 파이프라인 현황 업데이트",
    ],
    "자원": [
        "핵심 원자재 국제 가격 및 재고 수준 모니터링",
        "조달 계약 조건 및 대체 공급처 옵션 점검",
        "원자재 비용 변동이 제품 원가에 미치는 영향 시뮬레이션",
    ],
    "금융": [
        "이번 주 환율·금리 변동 추이 및 헤징 포지션 점검",
        "자본 조달 비용과 유동성 버퍼 현황 확인",
        "환율 시나리오별 수출입 손익 시뮬레이션 업데이트",
    ],
    "기술": [
        "기술 투자·R&D 로드맵과 이번 변화의 정합성 점검",
        "경쟁사 기술 대응 동향 모니터링",
        "특허·표준·인증 관련 일정 및 비용 확인",
    ],
    "수급": [
        "주요 제품 수주·재고·가동률 주간 현황 점검",
        "고객사 발주 변동 및 수요 전망 업데이트",
        "가격 변동 시 원가 전가 가능성 시뮬레이션",
    ],
    "규제": [
        "규제 시행 일정과 우리 대응 준비 상태 점검",
        "규제 준수 비용과 인증 취득 일정 확인",
        "경쟁사 규제 대응 현황 비교 분석",
    ],
    "일반": [
        "이번 주 관련 지표 변동 확인",
        "주요 거래처·고객사 동향 점검",
        "내부 리스크 대응 체계 가동 여부 확인",
    ],
}


def _build_differentiated_questions(questions_frame: list, topic: str, label: str, title: str,
                                     industry_extended_kw: list | None = None) -> str:
    """V10: 기사별 차별화된 경영진 질문 생성 — 테마 기반 동적 선택 + topic 삽입으로 기사별 고유화."""
    _theme = _classify_article_theme(title)
    _theme_qs = _THEME_QUESTIONS.get(_theme, _THEME_QUESTIONS["일반"])
    _best_kw = _best_keyword(title, industry_extended_kw)
    # V10.1: Q1에는 topic(2-word phrase) 사용 → 기사별 고유화 극대화
    # Q3에는 _best_kw(산업 키워드) 사용 → 산업 연관성 강화
    _q1_label = topic if topic else (_best_kw or "")

    _q1_base = _theme_qs[0] if _theme_qs else "이 변화가 우리 사업에 미치는 영향은?"
    if _q1_label and _q1_label not in _q1_base:
        _q1 = f"'{_q1_label}' 관련 — {_q1_base}"[:70]
    else:
        _q1 = _q1_base[:70]
    _q_list = [_q1]

    # Q2: 테마 풀에서 선택
    if len(_theme_qs) > 1:
        _q_list.append(_theme_qs[1][:70])

    # Q3: 산업 키워드 기반 고유 질문
    _q3_kw = _best_kw or topic
    if _q3_kw:
        _q_list.append(f"'{_q3_kw}' 관련 우리 {label} 사업 영향도 평가는?"[:70])
    elif len(_theme_qs) > 2:
        _q_list.append(_theme_qs[2][:70])

    return "\n".join(f"• {q}" for q in _q_list)


def _build_differentiated_checklist(checklist_frame: list, topic: str, label: str, title: str,
                                     industry_extended_kw: list | None = None) -> str:
    """V10.1: 기사별 차별화된 점검 항목 생성 — topic 삽입으로 기사별 고유화."""
    _theme = _classify_article_theme(title)
    _theme_cls = _THEME_CHECKLIST.get(_theme, _THEME_CHECKLIST["일반"])
    _best_kw = _best_keyword(title, industry_extended_kw)
    _cl1_label = topic if topic else (_best_kw or "")

    # CL1: topic(2-word) 삽입하여 기사별 차별화
    _cl1_base = _theme_cls[0] if _theme_cls else "관련 지표 변동 확인"
    if _cl1_label and _cl1_label not in _cl1_base:
        _cl1 = f"{_cl1_label} — {_cl1_base}"[:55]
    else:
        _cl1 = _cl1_base[:55]
    _cl_list = [_cl1]

    # CL2: 테마 풀
    if len(_theme_cls) > 1:
        _cl_list.append(_theme_cls[1][:55])

    # CL3: 산업 키워드 기반 고유 항목
    _cl3_kw = _best_kw or topic
    if _cl3_kw:
        _cl_list.append(f"{_cl3_kw} 동향 모니터링 및 영향도 평가"[:55])
    elif len(_theme_cls) > 2:
        _cl_list.append(_theme_cls[2][:55])

    return "\n".join(f"• {c}" for c in _cl_list)


def _build_industry_fallback(title: str, industry_key: str) -> dict:
    """LLM 실패 시 산업 맞춤 고품질 폴백 생성."""
    _label = _resolve_industry_label(industry_key)
    _topic = _extract_event_topic(title)  # Item 2: 회사명 대신 핵심 변화 이벤트 추출

    try:
        from core.industry_config import get_profile
        profile = get_profile(industry_key or "일반")
        analysis_kw = profile.get("analysis_keywords", {})
        impact_focus = analysis_kw.get("impact_focus", ["사업 환경", "수출 경쟁력"])
        risk_focus = analysis_kw.get("risk_focus", ["규제 변동", "시장 불확실성"])
        opp_focus = analysis_kw.get("opportunity_focus", ["시장 기회", "경쟁 우위"])
        strategy_templates = profile.get("strategy_templates", [])
        questions_frame = profile.get("questions_frame", [])
        checklist_frame = profile.get("checklist_frame", [])
    except ImportError:
        impact_focus = ["사업 환경", "수출 경쟁력"]
        risk_focus = ["규제 변동", "시장 불확실성"]
        opp_focus = ["시장 기회", "경쟁 우위"]
        strategy_templates = []
        questions_frame, checklist_frame = [], []

    _impact_kw1 = impact_focus[0] if impact_focus else "사업 환경"
    _impact_kw2 = impact_focus[1] if len(impact_focus) > 1 else "경쟁력"
    _risk_kw1 = risk_focus[0] if risk_focus else "리스크"
    _risk_kw2 = risk_focus[1] if len(risk_focus) > 1 else "불확실성"
    _opp_kw1 = opp_focus[0] if opp_focus else "기회"
    _opp_kw2 = opp_focus[1] if len(opp_focus) > 1 else "전략적 대응"

    # V10: industry_extended_kw 추출 (questions/checklist 차별화용)
    _industry_ext_kw = profile.get("keywords", []) if 'profile' in dir() else []
    try:
        from ui.article_cards import _INDUSTRY_EXTENDED_KW
        _industry_ext_kw = _INDUSTRY_EXTENDED_KW.get(industry_key, _industry_ext_kw)
    except (ImportError, AttributeError):
        pass

    # V10: 테마 분류 — Risk/Opp 차별화에 활용
    _theme = _classify_article_theme(title)
    _best_kw = _best_keyword(title, _industry_ext_kw)

    # V10: 테마별 Risk/Opp 차별화 문구
    # V12: theme + label 기반 Risk 인과관계 문장 (A→B→C 형식) — T-02
    _THEME_RISK_SENTENCES = {
        "통상": (
            f"'{_topic}' 관련 관세·수입 규제가 강화되면 → **{_label}** 수출 단가 경쟁력이 저하되어 → "
            f"기존 거래선 이탈과 매출 감소로 이어질 수 있습니다. "
            f"대체 시장 개척 속도가 느릴 경우 수출 물량 공백이 단기에 확대될 위험이 있습니다."
        ),
        "자원": (
            f"'{_topic}' 원자재 가격이 상승하면 → **{_label}** 기업의 생산 원가가 상승하여 → "
            f"영업이익률 압박과 고객사 납품 단가 인상 협상 부담이 동시에 발생합니다. "
            f"장기 계약 없이 현물 조달 비중이 높은 경우 원가 충격이 더 클 수 있습니다."
        ),
        "금융": (
            f"환율·금리가 불리하게 움직이면 → **{_label}** 기업의 수출 결제 환차손과 자본 조달 비용이 상승하여 → "
            f"영업이익과 순이익에 동시 압박이 가해집니다. "
            f"헤징 비율이 낮거나 만기가 단기에 집중된 경우 즉각적 리스크 노출이 우려됩니다."
        ),
        "기술": (
            f"'{_topic}' 기술 전환이 가속되면 → **{_label}** 기업의 기존 제품 경쟁력이 급격히 저하되어 → "
            f"수주 감소와 수출 단가 하락으로 이어질 수 있습니다. "
            f"기술 투자 지연 시 경쟁사와의 기술 격차가 빠르게 벌어질 위험이 있습니다."
        ),
        "수급": (
            f"'{_topic}' 수급 불균형이 지속되면 → **{_label}** 기업의 가동률이 저하되거나 재고가 급증하여 → "
            f"운전자본 부담과 수익성 악화로 연결될 수 있습니다. "
            f"고객사 발주 패턴 변화가 동반될 경우 영업 전망 불확실성이 더욱 커집니다."
        ),
        "규제": (
            f"'{_topic}' 규제가 강화되면 → **{_label}** 기업의 인증·준수 비용이 증가하여 → "
            f"제품 원가와 납품 일정에 영향을 주고 수출 경쟁력이 약화될 수 있습니다. "
            f"규제 대응 속도가 경쟁사보다 느릴 경우 시장 기회 손실로 이어질 위험이 있습니다."
        ),
        # V13: K-뷰티·글로벌진출 전용 Risk
        "글로벌": (
            f"글로벌 판로 경쟁이 심화되면 → **{_label}** 기업의 해외 바이어 확보 비용과 "
            f"브랜드 마케팅 투자 부담이 증가하여 → 수출 단가 하락과 마진 압박으로 이어질 수 있습니다. "
            f"특히 ODM 생산 단가 상승이나 바이어 이탈이 동반될 경우 단기 수출 실적이 악화될 위험이 있습니다."
        ),
        "일반": (
            f"'{_topic}' 변화가 심화되면 → **{_label}** 기업의 **{_risk_kw1}**이 악화되어 → "
            f"**{_risk_kw2}** 리스크가 현실화될 수 있습니다. "
            f"단기(1~3개월) 내 가시화될 수 있는 영향을 우선 모니터링하고, 시나리오별 대응 방안을 준비하세요."
        ),
    }
    # V12: theme 기반 Opportunity 차별화 문장 — T-02
    _THEME_OPP_SENTENCES = {
        "통상": (
            f"'{_topic}' 통상 변화에 선제 대응하는 **{_label}** 기업은 대체 시장 선점과 **공급망 재편 기회**를 확보할 수 있습니다. "
            f"무역 환경 변화를 바이어 다변화와 신규 시장 진출의 계기로 활용하면 중기 경쟁력이 강화됩니다."
        ),
        "자원": (
            f"'{_topic}' 원자재 가격 변동기에 **장기 공급 계약을 체결**하거나 조달 다변화를 실행한 **{_label}** 기업은 "
            f"원가 안정화 우위를 확보할 수 있습니다. 경쟁사 대비 빠른 대응이 마진 방어로 이어집니다."
        ),
        "금융": (
            f"현재 환율 국면에서 **환헤지 포지션을 최적화**한 **{_label}** 기업은 수익 안정성을 높이고 "
            f"자본 조달 비용을 절감할 수 있습니다. 금융 전략 차별화가 경쟁사 대비 수익성 우위로 연결됩니다."
        ),
        "기술": (
            f"'{_topic}' 기술 전환을 선제적으로 준비한 **{_label}** 기업은 **차세대 제품 수요**를 선점하고 "
            f"수출 단가를 방어할 수 있습니다. 기술 선도권 확보는 중기 수주 경쟁력의 핵심 차별화 요인입니다."
        ),
        "수급": (
            f"'{_topic}' 수급 변화 국면에서 재고·생산 계획을 선제적으로 조정한 **{_label}** 기업은 "
            f"**시장 점유율 확대**와 가격 협상력 강화 기회를 잡을 수 있습니다. "
            f"수급 안정화 이후 우선 납품 포지션을 선점하는 것이 중기 성과에 결정적입니다."
        ),
        "규제": (
            f"'{_topic}' 규제를 경쟁사보다 빠르게 충족한 **{_label}** 기업은 **인증 우위**를 바탕으로 "
            f"시장 진입 가속과 프리미엄 가격 유지가 가능합니다. "
            f"ESG·환경 규제 대응 선도는 글로벌 바이어의 공급망 선호도를 높이는 기회입니다."
        ),
        # V13: K-뷰티·글로벌진출 전용 Opportunity
        "글로벌": (
            f"글로벌 전시·박람회 선제 참가와 **K-브랜드 포지셔닝**을 강화한 **{_label}** 기업은 "
            f"신규 해외 바이어 접점과 **ODM 파트너십** 확대 기회를 선점할 수 있습니다. "
            f"해외 판로 다변화와 프리미엄 라인 확장이 중기 수출 성장의 핵심 레버입니다."
        ),
        "일반": (
            f"'{_topic}' 변화에 선제 대응하는 **{_label}** 기업은 **{_opp_kw1}** 확대와 **{_opp_kw2}** 강화 기회를 잡을 수 있습니다. "
            f"경쟁사 대비 차별화된 전략적 포지셔닝을 검토하고, 변화를 성장 레버리지로 활용하세요."
        ),
    }

    _risk_phrase = _THEME_RISK_SENTENCES.get(_theme, _THEME_RISK_SENTENCES["일반"])
    _opp_phrase = _THEME_OPP_SENTENCES.get(_theme, _THEME_OPP_SENTENCES["일반"])

    if strategy_templates and len(strategy_templates) >= 3:
        _actions = [t.split("?")[0].strip()[:30] if "?" in t else t[:30] for t in strategy_templates[:3]]
        _action_str = "\n".join(f"• {a}" for a in _actions)
    else:
        _action_str = (
            f"• {_label} 산업 영향도 사전 평가 및 시나리오 분석\n"
            f"• {_impact_kw1} 관련 내부 데이터 점검\n"
            f"• 경영진 브리핑 및 대응 방안 수립"
        )

    # V12: theme 기반 Impact 템플릿 — CEO 관점 구체적 비즈니스 영향 서술 (T-01)
    _THEME_IMPACT_TEMPLATES = {
        "통상": (
            f"**{_label}** 수출기업에 **관세·통상 환경 변화**로 인한 수출 물량 및 마진 압박이 예상됩니다. "
            f"'{_topic}' 이슈는 기존 수출 계약 조건과 대체 시장 확보 전략에 즉각적인 재검토를 요구합니다."
        ),
        "자원": (
            f"'{_topic}' 변화는 **{_label}** 기업의 **원자재 조달 비용**과 **생산 원가 구조**에 직접 영향을 미칩니다. "
            f"원가 상승분의 고객사 전가 가능성과 조달 다변화 여부를 즉시 점검해야 합니다."
        ),
        "금융": (
            f"환율·금리 변동이 **{_label}** 기업의 **수출 결제 마진**과 **자본 조달 비용**을 동시에 압박합니다. "
            f"'{_topic}' 관련 외화 노출도와 환헤지 포지션을 현 시점에서 재점검하는 것이 시급합니다."
        ),
        "기술": (
            f"'{_topic}' 기술 변화는 **{_label}** 기업의 **제품 경쟁력**과 **시장 포지션**에 구조적 영향을 미칩니다. "
            f"핵심 기술 확보 여부에 따라 수주 및 수출 단가에 중기적 차별이 발생할 전망입니다."
        ),
        "수급": (
            f"'{_topic}' 관련 수급 변동은 **{_label}** 기업의 **수주·가동률·재고**에 직접 영향을 미칩니다. "
            f"현재 수주잔량과 고객사 발주 동향을 확인하여 생산 계획을 조정해야 합니다."
        ),
        "규제": (
            f"'{_topic}' 규제 변화가 **{_label}** 기업의 **수출 인증 요건**과 **규제 준수 비용**을 변화시킵니다. "
            f"시행 일정과 적용 범위를 확인하고, 경쟁사 대비 준수 속도를 점검해야 합니다."
        ),
        # V13: K-뷰티·해외진출 전용 Impact
        "글로벌": (
            f"**{_label}** 기업의 글로벌 판로·해외 바이어 확보 경쟁이 가속화되고 있습니다. "
            f"K-브랜드 인지도·ODM 생산 역량·전시 채널 전략이 수출 성과의 핵심 차별화 요인으로 부상하고 있습니다."
        ),
        "일반": (
            f"'{_topic}' 변화는 **{_label}** 기업의 **{_impact_kw1}**과 **{_impact_kw2}**에 "
            f"단·중기적 영향을 미칠 수 있습니다. "
            f"영향 범위와 속도를 구체적으로 파악하여 전략적 대응 시점을 결정해야 합니다."
        ),
    }

    # V13: 이벤트 기반 Impact — title에서 기업명/시장/이벤트 추출 후 도입부 개인화
    _ev_fb = _extract_article_events(title, "")  # body_short → title만으로 추출
    _ev_fb_count = sum([
        bool(_ev_fb["companies"]),
        bool(_ev_fb["figures"]),
        bool(_ev_fb["markets"]),
        bool(_ev_fb["events"]),
    ])
    _ev_lead_fb = _build_event_lead(_ev_fb, title, _label) if _ev_fb_count >= 2 else ""
    _theme_conclusion_fb = _SMART_IMPACT_CONCLUSIONS.get(_theme, _SMART_IMPACT_CONCLUSIONS["일반"])
    _theme_conclusion_fb_str = _theme_conclusion_fb.format(label=_label, topic=_topic)

    if _ev_lead_fb and _ev_fb_count >= 2:
        _impact_text_fb = f"{_ev_lead_fb} {_theme_conclusion_fb_str}"
    else:
        _impact_text_fb = _THEME_IMPACT_TEMPLATES.get(_theme, _THEME_IMPACT_TEMPLATES["일반"])

    # V12: theme 기반 완성 문장 직접 사용 (T-02: 인과관계·차별화)
    _result = {
        "impact": _impact_text_fb,
        "risk": _risk_phrase,        # V12: 이미 완성된 인과관계 문장
        "opportunity": _opp_phrase,  # V12: 이미 완성된 차별화 문장
        "action": _action_str,
        "questions": _build_differentiated_questions(questions_frame, _topic, _label, title, _industry_ext_kw),
        "checklist": _build_differentiated_checklist(checklist_frame, _topic, _label, title, _industry_ext_kw),
        "headline": _generate_headline(title),
    }
    # V9: 후처리 위생 검사 적용
    return _sanitize_summary_output(_result)


def _build_smart_fallback(text: str, title: str, industry_key: str) -> dict:
    """V5: 본문 기반 스마트 폴백 — 기사 본문에서 핵심 문장을 추출하여 산업 프레임으로 재구성.

    V5 개선점:
      - _find_relevant()에 산업 키워드 가중치 로직 추가
      - action_templates 활용하여 기사 문맥에 맞는 Action 선별
      - questions/checklist 필드 생성 (questions_frame/checklist_frame 활용)
    """
    _label = _resolve_industry_label(industry_key)
    _topic = _extract_event_topic(title)  # Item 2: 핵심 변화 이벤트 토픽 추출
    _ik = industry_key or "일반"

    try:
        from core.industry_config import get_profile
        profile = get_profile(_ik)
        analysis_kw = profile.get("analysis_keywords", {})
        impact_focus = analysis_kw.get("impact_focus", [])
        risk_focus = analysis_kw.get("risk_focus", [])
        opp_focus = analysis_kw.get("opportunity_focus", [])
        strategy_templates = profile.get("strategy_templates", [])
        interpretation_frames = profile.get("interpretation_frames", {})
        action_templates = profile.get("action_templates", [])
        questions_frame = profile.get("questions_frame", [])
        checklist_frame = profile.get("checklist_frame", [])
        industry_keywords = profile.get("keywords", [])
    except ImportError:
        impact_focus, risk_focus, opp_focus = [], [], []
        strategy_templates, interpretation_frames = [], {}
        action_templates, questions_frame, checklist_frame = [], [], []
        industry_keywords = []

    # V9: 템플릿 삽입 헬퍼 — 의미 호환성 검증 후 {context} 치환
    def _fill_template(template: str, topic: str) -> str:
        """V9: _fill_ctx와 동일한 호환성 검증 로직 적용."""
        return _fill_ctx(template, topic)

    # V9.1: 문장 단위 트리밍 — 글자 단위 잘림 방지
    def _trim_sentence_boundary(text: str, max_len: int) -> str:
        """max_len 이내에서 마지막 완전한 문장까지만 반환. 문장 경계: 다/요/음/임/됨 + 공백 또는 마침표."""
        if len(text) <= max_len:
            return text
        cut = text[:max_len]
        # 한국어 문장 종결 패턴으로 마지막 완전한 문장 찾기
        import re as _re2
        # 마침표, 다/요/음 뒤 공백 등 문장 경계 탐색
        boundaries = list(_re2.finditer(r'(?<=[.다요음임됨])\s', cut))
        if boundaries:
            last_boundary = boundaries[-1].start() + 1
            return text[:last_boundary].strip()
        # 마침표만이라도
        last_dot = cut.rfind('.')
        if last_dot > max_len // 3:
            return text[:last_dot + 1].strip()
        # 경계를 찾지 못하면 공백 기준 잘림
        last_space = cut.rfind(' ')
        if last_space > max_len // 2:
            return text[:last_space].strip()
        return cut.strip()

    # V9.3: 본문에서 PII 사전 제거 후 문장 추출
    _clean_text = text[:3000]
    _clean_text = re.sub(r'[가-힣]{2,4}\s+\S*(?:금융|은행|연구|증권|리서치|자산운용|투자|경제)[가-힣]*\s+(?:연구위원|연구원|수석연구원|선임연구원|애널리스트|수석|팀장|센터장)', '', _clean_text)
    _clean_text = re.sub(r'[가-힣]{2,4}\s+(?:연구위원|연구원|수석연구원|선임연구원|수석이코노미스트|이코노미스트|애널리스트)(?:\s|$|,|\.)', ' ', _clean_text)
    _clean_text = re.sub(r'[가-힣]{2,4}\s+(?:기자|특파원|통신원|리포터)(?:\s|$|,|\.)', ' ', _clean_text)
    _clean_text = re.sub(r'\[사람이\s*되고\s*싶어요\d*\]', '', _clean_text)
    _clean_text = re.sub(r'\[[^\]]{0,30}싶어요\d*\]', '', _clean_text)
    _clean_text = re.sub(r'주택시장에\s*영향.{0,30}전세대출.{0,30}서민\s*주거안정.{0,30}', '', _clean_text)
    sentences = [s.strip() for s in re.split(r'[.。!?\n]+', _clean_text) if len(s.strip()) > 15]
    # V6: "예컨대", "예를 들어" 등 예시 문장 제외 (핵심이 아닌 부수적 예시 방지)
    sentences = [s for s in sentences if not re.match(r'^(예컨대|예를\s*들어|가령|이를테면)', s)]
    num_sents = [s for s in sentences if re.search(r'\d+[%조억원배p]', s)]

    # V11: 산업 키워드 매칭 가중치 기반 문장 선택 (강화)
    _used_sents: set = set()  # 중복 문장 방지
    def _score_sentence(sent, keyword_list):
        """V11: 산업 키워드 + 분석 키워드 + 구체성 점수."""
        score = sum(2 for kw in keyword_list if kw in sent)
        score += sum(1 for kw in industry_keywords if kw in sent)
        if re.search(r'\d+[%조억원배p]', sent):
            score += 2  # 수치 포함 보너스 강화
        # V11: 구체적 주체(기업명, 기관명) 포함 시 가산
        if re.search(r'[A-Z가-힣]{2,}(?:그룹|전자|자동차|화학|중공업|건설|은행|증권|위원회|산업부|통상부)', sent):
            score += 1
        # V11: 인과관계 표현 포함 시 가산
        if re.search(r'(?:때문|으로 인해|영향으로|따라서|결과|이에 따라|전망)', sent):
            score += 1
        return score

    def _find_relevant(keyword_list, fallback_pool, max_len=120):
        """V6: 중복 방지 — 이미 선택된 문장은 재사용하지 않음. V9.1: 문장 단위 트리밍."""
        # 1순위: 수치 포함 + 키워드 매칭 (가중치 순)
        scored = [(s, _score_sentence(s, keyword_list)) for s in num_sents if s not in _used_sents]
        scored.sort(key=lambda x: -x[1])
        if scored and scored[0][1] > 0:
            _used_sents.add(scored[0][0])
            return _trim_sentence_boundary(scored[0][0], max_len)
        # 2순위: 전체 문장 키워드 매칭 (가중치 순)
        scored_all = [(s, _score_sentence(s, keyword_list)) for s in sentences if s not in _used_sents]
        scored_all.sort(key=lambda x: -x[1])
        if scored_all and scored_all[0][1] > 0:
            _used_sents.add(scored_all[0][0])
            return _trim_sentence_boundary(scored_all[0][0], max_len)
        # 3순위: fallback (미사용 문장)
        for s in fallback_pool:
            if s not in _used_sents:
                _used_sents.add(s)
                return _trim_sentence_boundary(s, max_len)
        return ""

    _impact_sent = _find_relevant(impact_focus, num_sents or sentences[:2])
    _risk_sent = _find_relevant(risk_focus, sentences[len(sentences)//2:] if sentences else [])
    _opp_sent = _find_relevant(opp_focus, sentences[-3:] if sentences else [])

    # V13: 기사별 이벤트 요소 추출 → event-first Impact 생성 재료
    _ev = _extract_article_events(title, text)
    _specifics_count = sum([
        bool(_ev["companies"]),
        bool(_ev["figures"]),
        bool(_ev["markets"]),
        bool(_ev["events"]),
    ])
    _event_lead = _build_event_lead(_ev, title, _label) if _specifics_count >= 2 else ""

    # interpretation_frames 활용
    _impact_frame = interpretation_frames.get("impact", "이 변화가 {label} 기업에 미치는 직접적 영향을 파악해야 합니다.")
    _risk_frame = interpretation_frames.get("risk", "단기적으로 {label} 기업이 직면할 수 있는 리스크입니다.")

    # V10: 중앙화된 _TITLE_STOPWORDS 사용 (인라인 dict 제거)
    _title_keywords = [w for w in re.findall(r'[가-힣]{2,}', title) if len(w) >= 2 and w not in _TITLE_STOPWORDS]
    _title_topic_phrase = _topic  # V9.3: 안전한 추출 토픽 사용 (raw title 삽입 방지)

    # T-06: _theme/_topic_clause 사전 정의 (Impact 생성 전 필요)
    _theme = _classify_article_theme(title)
    _topic_clause = f"'{_topic}' 관련 " if _topic else ""

    # T-06: theme 기반 Impact 템플릿 (_build_industry_fallback 동일 로직 적용)
    # P1: 본문이 짧으면(RSS summary < 200자) 임계값 완화 — RSS 문장이 title과 일부 겹쳐도 허용
    _text_is_short = len(text.strip()) < 200

    # 제목 반복 패턴 방지 — body 문장이 제목과 threshold+ 겹치면 무시
    # P1: 단문 본문(RSS fallback)은 0.85로 완화하여 RSS summary 문장 활용 허용
    def _is_title_repetition(sent: str, threshold=None) -> bool:
        if not sent or not title:
            return False
        _s_words = set(re.findall(r'[가-힣A-Z]{2,}', sent))
        _t_words = set(re.findall(r'[가-힣A-Z]{2,}', title))
        if not _t_words:
            return False
        _thr = threshold if threshold is not None else (0.85 if _text_is_short else 0.7)
        return len(_s_words & _t_words) / len(_t_words) >= _thr

    _SMART_IMPACT_THEMES = {
        "통상": (
            f"**{_label}** 수출기업에 **관세·통상 환경 변화**로 인한 수출 물량 및 마진 압박이 예상됩니다. "
            f"'{_topic}' 이슈는 기존 수출 계약 조건과 대체 시장 확보 전략에 즉각적인 재검토를 요구합니다."
        ),
        "자원": (
            f"'{_topic}' 변화는 **{_label}** 기업의 **원자재 조달 비용**과 **생산 원가 구조**에 직접 영향을 미칩니다. "
            f"원가 상승분의 고객사 전가 가능성과 조달 다변화 여부를 즉시 점검해야 합니다."
        ),
        "금융": (
            f"환율·금리 변동이 **{_label}** 기업의 **수출 결제 마진**과 **자본 조달 비용**을 동시에 압박합니다. "
            f"'{_topic}' 관련 외화 노출도와 환헤지 포지션을 현 시점에서 재점검하는 것이 시급합니다."
        ),
        "기술": (
            f"'{_topic}' 기술 변화는 **{_label}** 기업의 **제품 경쟁력**과 **시장 포지션**에 구조적 영향을 미칩니다. "
            f"핵심 기술 확보 여부에 따라 수주 및 수출 단가에 중기적 차별이 발생할 전망입니다."
        ),
        "수급": (
            f"'{_topic}' 관련 수급 변동은 **{_label}** 기업의 **수주·가동률·재고**에 직접 영향을 미칩니다. "
            f"현재 수주잔량과 고객사 발주 동향을 확인하여 생산 계획을 조정해야 합니다."
        ),
        "규제": (
            f"'{_topic}' 규제 변화가 **{_label}** 기업의 **수출 인증 요건**과 **규제 준수 비용**을 변화시킵니다. "
            f"시행 일정과 적용 범위를 확인하고, 경쟁사 대비 준수 속도를 점검해야 합니다."
        ),
        # V13: K-뷰티·해외진출 전용 — 글로벌 채널·바이어 경쟁 관점
        "글로벌": (
            f"**{_label}** 기업의 글로벌 판로·해외 바이어 확보 경쟁이 가속화되고 있습니다. "
            f"K-브랜드 인지도·ODM 생산 역량·전시 채널 전략이 수출 성과의 핵심 차별화 요인으로 부상하고 있습니다."
        ),
        "일반": (
            f"'{_topic}' 변화는 **{_label}** 기업의 사업 환경과 경쟁력에 단·중기적 영향을 미칠 수 있습니다. "
            f"영향 범위와 속도를 구체적으로 파악하여 전략적 대응 시점을 결정해야 합니다."
        ),
    }

    # V13: event-first Impact — [이벤트 도입문] + [산업영향 결론] 조합
    # 이벤트 요소 ≥2개 + event_lead 있으면 이벤트 기반 Impact 우선
    _theme_impact_base = _SMART_IMPACT_THEMES.get(_theme, _SMART_IMPACT_THEMES["일반"])
    # P3: 글로벌 테마는 서브타입별 결론 분화 (ODM/박람회/팝업/수출판로)
    # event_lead 유무와 관계없이 글로벌 테마면 항상 서브타입 분기
    if _theme == "글로벌":
        _global_subtype = _classify_global_subtype(_ev, title)
        _theme_conclusion = _GLOBAL_SUBTYPE_CONCLUSIONS.get(_global_subtype,
                                                            _SMART_IMPACT_CONCLUSIONS["글로벌"])
    else:
        _theme_conclusion = _SMART_IMPACT_CONCLUSIONS.get(_theme, _SMART_IMPACT_CONCLUSIONS["일반"])
    _theme_conclusion_str = _theme_conclusion.format(label=_label, topic=_topic)

    if _event_lead and _specifics_count >= 2:
        # V13: 이벤트 도입 + 산업영향 결론 (기사별 고유 Impact)
        impact_text = f"{_event_lead} {_theme_conclusion_str}"
        # 본문 추출 문장이 있고 제목 반복이 아니면 추가 보강
        if _impact_sent and not _is_title_repetition(_impact_sent) and len(impact_text) < 220:
            impact_text = f"{impact_text} {_impact_sent}"
    elif _impact_sent and not _is_title_repetition(_impact_sent):
        # T-06: theme 도입부 + body 문장 보강 (제목 반복 차단)
        impact_text = f"{_theme_impact_base} {_impact_sent}"
    else:
        # T-06: body 없거나 title 반복 → theme 기반 단독
        impact_text = _theme_impact_base
    if len(impact_text) < 80:
        impact_text += f" {_label} 기업은 이 변화의 방향과 규모를 파악하여 자사 전략에 반영할 필요가 있습니다."

    # Fix C: Risk suffix — 글로벌 테마는 서브타입별 차별화, 그 외는 industry_config 템플릿
    _GLOBAL_RISK_SUFFIX = {
        "odm": (f"ODM 단가 상승·납품 조건 악화 시 **{_label}** 기업의 원가 경쟁력이 직접 훼손되며, "
                f"핵심 바이어 이탈 위험도 동반됩니다."),
        "expo": (f"전시 성과 부진 시 **{_label}** 기업의 해외 바이어 파이프라인이 단절되어 "
                 f"향후 1~2시즌 수출 수주에 공백이 생길 수 있습니다."),
        "popup": (f"팝업 ROI 미달 시 **{_label}** 기업의 현지화 마케팅 예산 효율성이 낮아지고 "
                  f"브랜드 현지 인지도 구축이 지연될 위험이 있습니다."),
        "export": (f"바이어 확보 경쟁 심화 시 **{_label}** 기업의 수출 단가 협상력이 낮아지고 "
                   f"신규 시장 진입 속도가 경쟁사 대비 뒤처질 수 있습니다."),
    }
    if _theme == "글로벌":
        _frame_risk = _GLOBAL_RISK_SUFFIX.get(_global_subtype, _risk_frame.format(label=_label))
    else:
        _frame_risk = _risk_frame.format(label=_label)
    _ev_co_str = "·".join(_ev["companies"][:2]) if _ev.get("companies") else ""
    if _risk_sent:
        # V11: 본문 리스크 문장 우선 + V12: 인과관계 구조 보강
        _causal_check = re.search(r'→|하면|으로 인해|결과적으로|발생|악화|이어질', _risk_sent)
        if _causal_check:
            risk_text = f"**{_label}** 리스크: {_topic_clause}{_risk_sent}"
        else:
            # V12: 인과관계 문구가 없으면 theme 기반 표현 추가
            risk_text = (
                f"**{_label}** 리스크: '{_topic}' 변화가 심화되면 → {_risk_sent}으로 인해 "
                f"{_label} 기업의 수익성이 악화될 수 있습니다."
            )
        if len(risk_text) < 120:
            risk_text += f" {_frame_risk}"
    elif _event_lead and _ev_co_str:
        # V13: 이벤트 기반 Risk — 추출된 기업명 활용
        risk_text = (
            f"{_ev_co_str} 동향이 부정적으로 전개되면 → **{_label}** 기업의 원가·매출·수주에 "
            f"부정적 영향이 발생하여 단기 실적 악화로 이어질 수 있습니다. {_frame_risk}"
        )
    elif _event_lead:
        # Fix B: event_lead 있지만 기업명 없을 때 — market/event 활용, _topic(회사명) 사용 방지
        _risk_mkt  = _ev["markets"][0] if _ev.get("markets") else ""
        _risk_evt  = _ev["events"][0]  if _ev.get("events")  else ""
        _risk_anchor = _risk_mkt or _risk_evt  # 시장·이벤트 우선, 둘 다 없으면 빈 문자열
        if _risk_anchor:
            risk_text = (
                f"**{_label}** 기업은 '{_risk_anchor}' 관련 규제·경쟁 환경이 악화되면 → "
                f"수출 비용 상승과 시장 진입 지연으로 → 단기 실적에 부정적 영향이 예상됩니다. {_frame_risk}"
            )
        else:
            risk_text = (
                f"**{_label}** 기업의 글로벌 판로 확장이 지연되면 → 해외 바이어 이탈과 "
                f"수출 단가 하락으로 → 중기 수익성 악화로 이어질 수 있습니다. {_frame_risk}"
            )
    else:
        # V12: 본문 없을 때 — _topic이 회사명인 경우 대비해 topic_clause만 활용
        _safe_topic = _topic if _topic and _topic not in title[:10] else (_topic_clause.strip("'관련 ") or "이번 변화")
        risk_text = (
            f"{_label} 기업은 '{_safe_topic}' 변화가 심화되면 → {_label} 기업의 원가·매출·수주에 "
            f"부정적 영향이 발생하여 → 단기 실적 악화로 이어질 수 있습니다. {_frame_risk}"
        )
    if len(risk_text) < 80:
        risk_text += f" 특히 1~3개월 내 가시화될 수 있는 영향을 우선 모니터링하세요."

    _opp_frame = interpretation_frames.get("opportunity", "선제 대응 시 경쟁 우위를 확보할 기회입니다.")
    # T-07: theme×산업 특화 Opportunity 템플릿 (_build_industry_fallback 동일 로직 적용)
    _opp_kw1 = opp_focus[0] if opp_focus else "기회"
    _opp_kw2 = opp_focus[1] if len(opp_focus) > 1 else "전략적 대응"
    _SMART_OPP_THEMES = {
        "통상": (
            f"'{_topic}' 통상 변화에 선제 대응하는 **{_label}** 기업은 대체 시장 선점과 "
            f"**공급망 재편 기회**를 확보할 수 있습니다. "
            f"무역 환경 변화를 바이어 다변화와 신규 시장 진출의 계기로 활용하면 중기 경쟁력이 강화됩니다."
        ),
        "자원": (
            f"'{_topic}' 원자재 변동기에 **장기 공급 계약 체결**이나 조달 다변화를 실행한 **{_label}** 기업은 "
            f"원가 안정화 우위를 확보할 수 있습니다. "
            f"경쟁사 대비 빠른 대응이 마진 방어와 수익성 우위로 이어집니다."
        ),
        "금융": (
            f"현재 환율 국면에서 **환헤지 포지션 최적화**를 실행한 **{_label}** 기업은 수익 안정성을 높이고 "
            f"자본 조달 비용을 절감할 수 있습니다. "
            f"금융 전략 차별화가 경쟁사 대비 수익성 우위로 연결됩니다."
        ),
        "기술": (
            f"'{_topic}' 기술 전환을 선제적으로 준비한 **{_label}** 기업은 **차세대 제품 수요**를 선점하고 "
            f"수출 단가를 방어할 수 있습니다. "
            f"기술 선도권 확보는 중기 수주 경쟁력의 핵심 차별화 요인입니다."
        ),
        "수급": (
            f"'{_topic}' 수급 변화에서 재고·생산 계획을 선제 조정한 **{_label}** 기업은 "
            f"**시장 점유율 확대**와 가격 협상력 강화 기회를 잡을 수 있습니다. "
            f"수급 안정화 이후 우선 납품 포지션 선점이 중기 성과에 결정적입니다."
        ),
        "규제": (
            f"'{_topic}' 규제를 경쟁사보다 빠르게 충족한 **{_label}** 기업은 **인증 우위**를 바탕으로 "
            f"시장 진입 가속과 프리미엄 가격 유지가 가능합니다. "
            f"ESG·환경 규제 선도 대응은 글로벌 바이어의 공급망 선호도를 높이는 기회입니다."
        ),
        # V13: K-뷰티·글로벌진출 전용 Opportunity
        "글로벌": (
            f"글로벌 전시·박람회 선제 참가와 **K-브랜드 포지셔닝**을 강화한 **{_label}** 기업은 "
            f"신규 해외 바이어 접점과 **ODM 파트너십** 확대 기회를 선점할 수 있습니다. "
            f"해외 판로 다변화와 프리미엄 라인 확장이 중기 수출 성장의 핵심 레버입니다."
        ),
        "일반": (
            f"이 변화에 선제 대응하는 **{_label}** 기업은 {_topic_clause}**{_opp_kw1}** 확대와 "
            f"**{_opp_kw2}** 강화 기회를 잡을 수 있습니다. "
            f"경쟁사 대비 차별화된 전략적 포지셔닝을 검토하고 변화를 성장 레버리지로 활용하세요."
        ),
    }
    if _opp_sent:
        # V11: 본문 기회 문장 우선
        opp_text = f"**{_label}** 기회: {_topic_clause}{_opp_sent}"
        if len(opp_text) < 120:
            opp_text += f" {_opp_frame}"
    elif _event_lead and _ev.get("markets"):
        # V13: 이벤트 기반 Opportunity — 추출된 시장 정보 활용
        _mkt_s = _ev["markets"][0]
        opp_text = (
            f"{_mkt_s} 등 글로벌 시장에서 선제 대응한 **{_label}** 기업은 "
            f"**{_opp_kw1}** 확대와 해외 채널 선점 기회를 잡을 수 있습니다. "
            f"경쟁사 대비 빠른 글로벌 포지셔닝이 중기 수출 성과의 핵심 차별화 요인입니다."
        )
    elif _theme == "글로벌":
        # Fix D: 글로벌 테마 — Opportunity도 서브타입별 차별화
        _GLOBAL_SUBTYPE_OPP = {
            "odm": (
                f"ODM 파트너십을 선제 강화한 **{_label}** 기업은 "
                f"**바이어별 맞춤 제조** 역량 차별화로 단가 우위와 독점 납품 계약 기회를 확보할 수 있습니다. "
                f"ODM 원가 경쟁력과 품질 인증이 글로벌 주요 바이어 선택의 핵심 기준입니다."
            ),
            "expo": (
                f"전시·박람회를 전략적 바이어 접점으로 활용한 **{_label}** 기업은 "
                f"**현장 수출 상담 → 계약 전환**의 최단 경로를 통해 신규 시장 진입 속도를 높일 수 있습니다. "
                f"전시 전 제품 라인업 정비와 현지 바이어 사전 매칭이 성과 극대화의 핵심입니다."
            ),
            "popup": (
                f"팝업 체험 채널을 브랜드 현지화의 첫 교두보로 활용한 **{_label}** 기업은 "
                f"**현지 소비자 데이터 확보**와 SNS 바이럴 효과로 온·오프라인 동시 성장 기회를 잡을 수 있습니다. "
                f"팝업 참여 소비자를 장기 충성 고객으로 전환하는 CRM 전략이 ROI를 결정합니다."
            ),
            "export": (
                f"수출 상담 채널을 체계적으로 관리한 **{_label}** 기업은 "
                f"**신규 바이어 발굴 → 장기 공급 계약 전환**의 파이프라인을 구축하여 "
                f"수출 의존도와 지역 다변화를 동시에 달성할 수 있습니다."
            ),
        }
        opp_text = _GLOBAL_SUBTYPE_OPP.get(_global_subtype, _SMART_OPP_THEMES["글로벌"])
    else:
        # T-07: theme×산업 특화 Opportunity (모든 산업 동일 "경쟁 우위 확보" 제거)
        opp_text = _SMART_OPP_THEMES.get(_theme, _SMART_OPP_THEMES["일반"])
    if len(opp_text) < 80:
        opp_text += f" 경쟁사 대비 차별화된 전략적 포지셔닝을 검토하세요."

    # V6: action — action_templates 기반 기사 문맥 매칭 (치환 개선)
    _action_str = ""
    if action_templates:
        # 기사 문맥에 매칭되는 템플릿 우선 선택
        _title_body = (title + " " + text[:500]).lower()
        _scored_actions = []
        for t in action_templates:
            # 템플릿에서 주요 단어 추출하여 기사와 매칭
            _t_clean = t.replace("{context}", "").replace("{{context}}", "")
            _t_words = re.findall(r'[가-힣]{2,}', _t_clean)
            _match_score = sum(1 for w in _t_words if w in _title_body)
            _scored_actions.append((t, _match_score))
        _scored_actions.sort(key=lambda x: -x[1])
        # V6: _fill_template 사용 + 잘림 길이 확대 (35→50)
        _top3 = [_fill_template(t, _topic)[:50] for t, _ in _scored_actions[:3]]
        _action_str = "\n".join(f"• {a}" for a in _top3)

    if not _action_str:
        if strategy_templates and len(strategy_templates) >= 3:
            _actions = [t.split("?")[0].strip()[:45] if "?" in t else t[:45] for t in strategy_templates[:3]]
            _action_str = "\n".join(f"• {a}" for a in _actions)
        else:
            _action_str = (
                f"• {_label} 산업 영향도 사전 평가 및 시나리오 분석\n"
                f"• 관련 내부 데이터 점검 및 리스크 요인 파악\n"
                f"• 경영진 브리핑 및 대응 방안 수립"
            )

    # V10: 중앙화된 V10 Q/CL 함수 사용 (테마 기반 동적 선택)
    _questions_str = _build_differentiated_questions(questions_frame, _topic, _label, title, industry_keywords)
    _checklist_str = _build_differentiated_checklist(checklist_frame, _topic, _label, title, industry_keywords)

    # Item 3: Anchor 키워드 강제 포함 — 기사 고유성 보장
    _anchor_kws = _extract_anchor_keywords(title, _ev)
    if _anchor_kws:
        impact_text = _inject_anchor_prefix(impact_text, _anchor_kws, _label)
        risk_text   = _inject_anchor_prefix(risk_text,   _anchor_kws, _label)
        # Opportunity는 공통 전략 성격이므로 anchor 강제 삽입 생략

    # Item 4: 세션 내 중복 감지 — 중복이면 서브타입 분기 접미사 추가
    _art_id = re.sub(r'\s+', '_', title[:30])  # 제목 기반 임시 ID
    if _is_duplicate_summary(_art_id, impact_text):
        # 중복 시 _topic을 앞에 명시하여 차별화
        _dedup_prefix = f"[{_topic}] " if _topic else f"[{title[:10]}] "
        impact_text = _dedup_prefix + impact_text
    _register_summary(_art_id, impact_text)

    # Item 5: 서브카테고리 분류 — 소비재·식품만 적용
    _subcat = _classify_subcategory(title, _ik)

    _result = {
        "impact": impact_text,
        "risk": risk_text,
        "opportunity": opp_text,
        "action": _action_str,
        "questions": _questions_str,
        "checklist": _checklist_str,
        "headline": _generate_headline(title),
        "body_tier": _assess_body_quality(text),
        "subcategory": _subcat,  # Item 5: 서브카테고리 (빈 문자열이면 미해당)
    }
    # V9: 후처리 위생 검사 적용
    return _sanitize_summary_output(_result)


def _verify_body_title_relevance(body_text: str, title: str) -> bool:
    """(디버그 전용) 본문-제목 관련성 로그 출력. 흐름 차단에는 사용하지 않음."""
    if not title or not body_text:
        return False
    title_words = set(re.findall(r'[가-힣]{2,}', title))
    stopwords = {"우리", "이번", "대한", "관련", "통해", "위해", "대비", "이상", "이하", "현재", "가능", "경우", "하면"}
    title_words -= stopwords
    if len(title_words) < 2:
        return True
    body_sample = body_text[:2000]
    match_count = sum(1 for w in title_words if w in body_sample)
    is_relevant = match_count >= min(2, len(title_words))
    if not is_relevant:
        print(f"[summarizer] ℹ️ 본문-제목 관련도 낮음: 제목키워드={title_words}, 매칭={match_count}")
    return is_relevant


def summarize_3line(
    text: str,
    title: str = "",
    industry_key: str = "일반",
    url: str = "",            # V17: URL 기반 캐시 키 지원
    article_rank: int = 0,   # V17: Top N 제한 (0=무제한, 1~=순위)
) -> tuple[dict, str]:
    """
    정책 브리핑용 요약 생성 (v6 — V3 흐름 재구성).

    반환: (summary, source)
      summary = dict {"impact","risk","opportunity","action","headline"} (4-frame)
      source  = "groq" | "cache" | "body_short" | "industry_fallback"

    V3 핵심 변경:
      - title_guard 제거 → 본문 100자 미만일 때만 차단
      - 저품질 캐시(rule_enhanced/title_guard) 자동 무시 → 재생성
      - rate limit 대기 (2초 간격)
      - 상세 에러 로깅

    V17 추가:
      - url 파라미터: URL 기반 캐시 키 우선 사용 (동일 URL 재생성 금지)
      - article_rank: Top 3 초과 시 LLM 금지 → smart_fallback 직행
      - body_length < 400: LLM 금지 → smart_fallback 직행
      - 세션 LLM 카운터 추적
    """
    # title이 dict로 전달된 경우 방어
    _title_str = title if isinstance(title, str) else str(title.get("title", "")) if isinstance(title, dict) else ""
    _ik = industry_key or "일반"

    # ── Phase 0: URL 기반 캐시 확인 (V17: text hash보다 우선) ──
    _url = url or ""
    _cache = _load_summary_cache()
    if _url:
        _url_ck = _cache_key_for_url(_url, _ik)
        if _url_ck in _cache:
            _entry = _cache[_url_ck]
            try:
                _cached_at = datetime.fromisoformat(_entry.get("cached_at", ""))
                # TODO-5: 엔트리별 ttl_days 우선, 없으면 소스별 TTL, 없으면 전역 TTL
                _entry_src = _entry.get("source", "cache")
                _eff_ttl = _entry.get("ttl_days",
                           _CACHE_TTL_BY_SOURCE.get(_entry_src, _CACHE_TTL_DAYS))
                if _eff_ttl > 0 and (datetime.now() - _cached_at).days < _eff_ttl:
                    _cached_source = _entry_src
                    if _cached_source in ("groq",):
                        _cached_summary = _entry["summary"]
                        if isinstance(_cached_summary, dict):
                            _cached_summary = _sanitize_summary_output(_cached_summary)
                        with _llm_session_lock:
                            _llm_session_state["cache_hits"] += 1
                        print(f"[summarizer] cache_hit=True llm_call=False (URL) — '{_title_str[:30]}' [{_ik}]")
                        return _cached_summary, "cache"
            except Exception:
                pass

    # ── Phase 1: 텍스트 해시 캐시 확인 (groq만 히트, 나머지 재생성) ──
    _ck = _cache_key(text, _ik)
    if _ck in _cache:
        _entry = _cache[_ck]
        try:
            _cached_at = datetime.fromisoformat(_entry.get("cached_at", ""))
            # TODO-5: 소스별 TTL 적용
            _entry_src = _entry.get("source", "cache")
            _eff_ttl = _entry.get("ttl_days",
                       _CACHE_TTL_BY_SOURCE.get(_entry_src, _CACHE_TTL_DAYS))
            if _eff_ttl > 0 and (datetime.now() - _cached_at).days < _eff_ttl:
                _cached_source = _entry_src
                if _cached_source in ("groq",):
                    # V9.3: 캐시 읽기 시에도 위생 검사 적용 (오염 캐시 방어)
                    _cached_summary = _entry["summary"]
                    if isinstance(_cached_summary, dict):
                        _cached_summary = _sanitize_summary_output(_cached_summary)
                    with _llm_session_lock:
                        _llm_session_state["cache_hits"] += 1
                    print(f"[summarizer] 📦 캐시 히트 (groq) — '{_title_str[:30]}...' [{_ik}]")
                    return _cached_summary, "cache"
                else:
                    print(f"[summarizer] 🔄 저품질 캐시 무시 ({_cached_source}) — 재생성 시도")
        except Exception:
            pass

    # ── Phase 2: 본문 품질 계층화 (Item 1) ──
    _body_len = len(text.strip()) if text else 0
    _body_tier = _assess_body_quality(text or "")

    # MINIMAL 계층 (<50자) — 본문 없음: 과도한 주장 금지, 최소 요약만
    if _body_tier == "minimal":
        print(f"[summarizer] ⛔ 본문 없음 ({_body_len}자) — 최소 폴백: '{_title_str[:40]}'")
        _fallback = _build_minimal_fallback(_title_str, _ik)
        # TODO-5: minimal_fallback은 캐시 저장 안 함 (TTL=0) — 항상 재생성해도 동일 결과
        # _cache[_ck] = {...}  # 저장 생략
        return _fallback, "minimal_fallback"

    # SNIPPET 계층 (50~119자) — GN 스니펫 수준: generic 방지, snippet 전용 카드 생성 (V15)
    if _body_len < 120:
        print(f"[summarizer] ⚠️ snippet ({_body_len}자) — snippet 카드: '{_title_str[:40]}...'")
        _fallback = _build_snippet_card(text or "", _title_str, _ik)
        _log_card_generation(_title_str, "snippet", _body_len, _ik)
        # TODO-5: snippet TTL=2일 (groq 7일보다 짧게, 본문 fetch 후 재생성 유도)
        _cache[_ck] = {"summary": _fallback, "source": "snippet",
                       "cached_at": datetime.now().isoformat(), "ttl_days": 2}
        _save_summary_cache(_cache)
        return _fallback, "snippet"

    # ── Phase 2.5: V17 LLM 호출 조건 강화 ──────────────────────
    _llm_blocked = False
    _llm_block_reason = ""

    # (a) body_length < 400 → LLM 금지 (snippet_only 기사)
    if _body_len < _LLM_MIN_BODY_LENGTH:
        _llm_blocked = True
        _llm_block_reason = f"body_short ({_body_len}자 < {_LLM_MIN_BODY_LENGTH}자)"

    # (b) article_rank > 0: Top N 초과 시 LLM 금지
    if not _llm_blocked and article_rank > 0:
        with _llm_session_lock:
            _current_llm_calls = _llm_session_state["llm_calls"]
        if _current_llm_calls >= _LLM_MAX_ARTICLES:
            _llm_blocked = True
            _llm_block_reason = f"top_limit ({_current_llm_calls}/{_LLM_MAX_ARTICLES}건 이미 호출)"

    if _llm_blocked:
        print(f"[summarizer] ⛔ LLM 금지: {_llm_block_reason} — '{_title_str[:30]}'")
        with _llm_session_lock:
            _llm_session_state["fallback_skips"] += 1
        # Phase 4 (smart_fallback) 직행
        _groq_key_v17 = ""
    else:
        _groq_key_v17 = _get_llm_key()

    # ── Phase 3: LLM 호출 (본문 400자 이상 + Top N 이내 + 서킷 브레이커 통과 시) ──
    _groq_key = _groq_key_v17
    if _groq_key and _groq_circuit_is_open():
        print(
            f"[summarizer] circuit_breaker=True fallback_summary_used=True — "
            f"'{_title_str[:30]}...'"
        )
        with _llm_session_lock:
            _llm_session_state["fallback_skips"] += 1
        _groq_key = ""  # Phase 3 건너뛰기
    if _groq_key:
        # 디버그: 본문-제목 관련도 로그 (흐름 차단 안 함)
        _verify_body_title_relevance(text, _title_str)

        # V17: 세션 LLM 카운터 증가
        with _llm_session_lock:
            _llm_session_state["llm_calls"] += 1
        print(f"[summarizer] 🚀 LLM 호출 시도 — '{_title_str[:30]}...' [{_ik}] (본문 {_body_len}자)")
        llm_result = _summarize_with_llm(text, _title_str, industry_key=_ik)

        if llm_result:
            if isinstance(llm_result, dict):
                # V11: 다면 품질 스코어링
                _q_score, _q_issues = _validate_summary_quality_v2(llm_result, industry_key=_ik)

                if _q_score < 50:
                    # 심각한 품질 미달 → 타겟 힌트와 함께 재시도
                    # V13-429: skip_primary=True — 70B가 방금 rate-limited일 가능성 높으므로 8B로 직접 재시도
                    _retry_hint = _build_retry_hint(_q_issues, industry_key=_ik)
                    print(f"[summarizer] ⚠️ 품질 {_q_score}점 — 타겟 재시도 (힌트 {len(_q_issues)}개, 8B 직접)")
                    _record_quality_metric("groq_retry_needed", score=_q_score, industry=_ik,
                                           retry_reason="; ".join(_q_issues[:3]))
                    retry_result = _summarize_with_llm(
                        text + f"\n\n{_retry_hint}",
                        _title_str,
                        industry_key=_ik,
                        skip_primary=True,   # V13-429: 70B 건너뛰고 8B 직접 사용
                    )
                    if isinstance(retry_result, dict):
                        _retry_score, _retry_issues = _validate_summary_quality_v2(retry_result, industry_key=_ik)
                        if _retry_score > _q_score:
                            llm_result = retry_result
                            _q_score = _retry_score
                            print(f"[summarizer] ✅ 재시도 품질 향상: {_q_score}점")
                            _record_quality_metric("groq_retry", score=_retry_score, industry=_ik)
                        else:
                            # V17.4-patch: 재시도 품질 미향상 시 smart_fallback과 비교 후 더 나은 결과 선택
                            _sf_candidate = _build_smart_fallback(text or "", _title_str, _ik)
                            _sf_score, _ = _validate_summary_quality_v2(_sf_candidate, industry_key=_ik)
                            if _sf_score > _q_score:
                                llm_result = _sf_candidate
                                _q_score = _sf_score
                                print(f"[summarizer] ⚠️ 재시도 품질 미향상 ({_retry_score}점) → smart_fallback 채택 ({_sf_score}점)")
                            else:
                                print(f"[summarizer] ⚠️ 재시도 품질 미향상 ({_retry_score}점) — 기존 LLM 결과 유지")
                    else:
                        # V17.4-patch: 재시도 자체 실패(429 등) → smart_fallback으로 저품질 고착 방지
                        print("[summarizer] ⚠️ 재시도 실패(429/타임아웃) → smart_fallback 실행")
                        _sf_candidate = _build_smart_fallback(text or "", _title_str, _ik)
                        _sf_score, _ = _validate_summary_quality_v2(_sf_candidate, industry_key=_ik)
                        if _sf_score > _q_score:
                            llm_result = _sf_candidate
                            _q_score = _sf_score
                            print(f"[summarizer] ✅ smart_fallback 채택 (LLM {_q_score}점 < fallback {_sf_score}점 불가, fallback 사용)")
                        else:
                            print(f"[summarizer] ⚠️ smart_fallback({_sf_score}점)도 LLM({_q_score}점)보다 낮음 — 기존 LLM 유지")

                elif _q_score < 70:
                    # 중간 품질 → 1회 경량 재시도 (힌트 축소, 8B 직접)
                    _retry_hint = _build_retry_hint(_q_issues[:2], industry_key=_ik)
                    print(f"[summarizer] ⚠️ 품질 {_q_score}점 — 경량 재시도 (8B 직접)")
                    retry_result = _summarize_with_llm(
                        text + f"\n\n{_retry_hint}",
                        _title_str,
                        industry_key=_ik,
                        skip_primary=True,   # V13-429: 70B 건너뛰고 8B 직접 사용
                    )
                    if isinstance(retry_result, dict):
                        _retry_score, _ = _validate_summary_quality_v2(retry_result, industry_key=_ik)
                        if _retry_score > _q_score:
                            llm_result = retry_result
                            _q_score = _retry_score
                            print(f"[summarizer] ✅ 경량 재시도 품질 향상: {_q_score}점")
                            _record_quality_metric("groq_retry", score=_retry_score, industry=_ik)
                else:
                    # 고품질 → 즉시 사용
                    print(f"[summarizer] ✅ 품질 {_q_score}점 — 즉시 사용")

                _record_quality_metric("groq", score=_q_score, industry=_ik)
                llm_result["headline"] = _generate_headline(_title_str)
                # V9.1: LLM 결과에도 위생 검사 적용 (교차 기사 오염 방지)
                llm_result = _sanitize_summary_output(llm_result)
                print(f"[summarizer] ✅ Groq 4-frame 요약 성공 — [{_ik}] (품질: {_q_score}/100)")
            else:
                print(f"[summarizer] ✅ Groq 요약 성공 (text, {len(llm_result)}자)")
                _record_quality_metric("groq", score=60, industry=_ik)

            _analysis_src = _determine_analysis_mode(_body_len)
            if isinstance(llm_result, dict):
                llm_result["analysis_source"] = _analysis_src
            _log_card_generation(_title_str, _analysis_src, _body_len, _ik)
            _cache_entry = {
                "summary": llm_result,
                "source": "groq",
                "cached_at": datetime.now().isoformat(),
                "prompt_version": _PROMPT_VERSION,   # V16: 버전 태그 → orphaned 정리용
            }
            _cache[_ck] = _cache_entry
            # V17: URL 기반 캐시도 함께 저장 (동일 URL 재생성 금지)
            if _url:
                _url_ck_save = _cache_key_for_url(_url, _ik)
                _cache[_url_ck_save] = _cache_entry
            # V16 P3: 캐시 저장 시 orphaned 엔트리 정리 (30일 이상 또는 버전 불일치)
            _cache = _purge_orphaned_cache(_cache, max_age_days=30)
            _save_summary_cache(_cache)
            return llm_result, "groq"
        else:
            print(f"[summarizer] ❌ LLM 결과 없음 — 산업별 폴백으로 이동")
    else:
        # V17: top_limit/body_short/circuit_breaker 차단 vs 실제 API 키 없음 분리
        if _llm_blocked:
            print(f"[summarizer] fallback_reason={_llm_block_reason.split('(')[0].strip()} — smart_fallback 직행")
        elif not _get_llm_key():
            print(f"[summarizer] ⚠️ Groq API 키 없음 — 폴백 사용")

    # ── Phase 4: 고품질 스마트 폴백 ──
    print(f"[summarizer] 📋 스마트 폴백 생성 — '{_title_str[:30]}...' [{_ik}]")
    _fallback = _build_smart_fallback(text or "", _title_str, _ik)
    if isinstance(_fallback, dict):
        _fallback["analysis_source"] = "smart_fallback"
    _log_card_generation(_title_str, "smart_fallback", _body_len, _ik)
    _record_quality_metric("smart_fallback", score=35, industry=_ik)
    # TODO-5: smart_fallback TTL=3일 (groq 7일보다 짧게 — LLM 회복 시 재생성 유도)
    _cache[_ck] = {"summary": _fallback, "source": "smart_fallback",
                   "cached_at": datetime.now().isoformat(),
                   "ttl_days": _CACHE_TTL_BY_SOURCE.get("smart_fallback", 3)}
    _save_summary_cache(_cache)
    return _fallback, "smart_fallback"


# ──────────────────────────────────────────────────────
# 1. 규칙 기반 추출 요약 (훅 생성·호환용)
# ──────────────────────────────────────────────────────
def summarize_rule_based(
    text: str,
    title: str = "",
    max_sentences: int = 3,
    industry_key: str = "일반",
) -> dict:
    """
    텍스트에서 중요 문장을 점수화하여 추출 요약을 반환한다.
    외부 LLM 사용 없이 순수 규칙 기반으로 동작한다.
    4-frame dict 반환으로 통일 (v5).

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
        _t = title if isinstance(title, str) else "본문 없음"
        _label = _resolve_industry_label(industry_key)
        return {
            "impact": f"{_t} 관련 영향 분석 필요",
            "risk": "상세 본문 확인 후 리스크 평가 필요",
            "opportunity": f"{_label} 관점 기회 요인 검토 필요",
            "action": "원문 기사 확인 후 대응 방안 수립",
            "headline": _generate_headline(_t),
        }

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
        except ImportError as e:
            _log.debug("Could not import industry_config: %s", e)

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

    # 점수 내림차순 → 상위 4문장 추출
    scored.sort(key=lambda x: (-x[0], x[1]))
    top_sentences = [s for _, _, s in scored[:4]]

    industry_label = _resolve_industry_label(industry_key)
    return {
        "impact": top_sentences[0] if len(top_sentences) > 0 else f"{_title_str} 관련 영향 분석 필요",
        "risk": top_sentences[1] if len(top_sentences) > 1 else "상세 본문 확인 후 리스크 평가 필요",
        "opportunity": top_sentences[2] if len(top_sentences) > 2 else f"{industry_label} 관점 기회 요인 검토 필요",
        "action": top_sentences[3] if len(top_sentences) > 3 else "원문 기사 확인 후 대응 방안 수립",
        "headline": _generate_headline(_title_str),
    }


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
