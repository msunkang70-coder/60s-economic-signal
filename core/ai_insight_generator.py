"""
core/ai_insight_generator.py
LLM 기반(Groq) 또는 규칙 기반 fallback — 산업별 거시경제 인사이트 생성.

Function:
  generate_ai_insight(label, trend, industry_key, direction, ...) → str (1 sentence)

Item 10-11 of the dashboard intelligence spec.
"""

from __future__ import annotations

from core.utils import safe_execute

# ── 규칙 기반 인사이트 템플릿 ─────────────────────────────────────
# Key: (지표명_키워드, direction)  → 1문장 Korean insight
# {industry} placeholder는 산업 한국어명으로 대체됨
_TEMPLATES: dict[tuple[str, str], str] = {
    # FX / 환율
    ("환율",    "Positive"): "{industry} 수출 마진 개선 가능 — 원화 약세 수혜 구간.",
    ("환율",    "Neutral"):  "{industry} 환율 영향 제한적 — 현 수준 모니터링 유지.",
    ("환율",    "Negative"): "{industry} 원자재 수입 원가 상승 — 비용 구조 재검토 필요.",
    # CPI / 소비자물가
    ("소비자물가", "Positive"): "{industry} 원가 부담 완화 — 마진 개선 기회.",
    ("소비자물가", "Neutral"):  "{industry} 물가 수준 관리 가능 범위 — 추세 주시 필요.",
    ("소비자물가", "Negative"): "{industry} 원자재·에너지 비용 상승 압박 심화.",
    ("CPI",    "Positive"):   "{industry} 물가 안정으로 원가 부담 완화.",
    ("CPI",    "Neutral"):    "{industry} 물가 추세 점검 필요.",
    ("CPI",    "Negative"):   "{industry} 고물가로 원가 상승 압박 증가.",
    # 수출증가율
    ("수출증가율", "Positive"): "{industry} 글로벌 수요 회복 신호 — 생산·재고 확대 검토.",
    ("수출증가율", "Neutral"):  "{industry} 수출 성장 둔화 — 주력 시장 수요 점검 필요.",
    ("수출증가율", "Negative"): "{industry} 수출 감소 추세 — 긴급 시장 전략 재검토.",
    # 기준금리
    ("기준금리", "Positive"): "{industry} 금리 인하 기조 — 자금 조달 비용 완화 기대.",
    ("기준금리", "Neutral"):  "{industry} 금리 동결 기조 — 금융 비용 현행 수준 유지.",
    ("기준금리", "Negative"): "{industry} 금리 인상 압박 — 투자·조달 비용 증가 우려.",
    # 수입물가
    ("수입물가", "Positive"): "{industry} 수입 원가 안정 — 원자재 조달 비용 완화.",
    ("수입물가", "Neutral"):  "{industry} 수입 물가 추세 점검 필요.",
    ("수입물가", "Negative"): "{industry} 수입 원가 상승 — 원자재 조달 비용 압박.",
    # 수출물가
    ("수출물가", "Positive"): "{industry} 수출 단가 상승 — 수출 채산성 개선 기회.",
    ("수출물가", "Neutral"):  "{industry} 수출 단가 추세 점검 필요.",
    ("수출물가", "Negative"): "{industry} 수출 단가 하락 — 수익성 압박 점검 필요.",
    # 엔화
    ("엔",      "Positive"): "{industry} 엔화 동향 — 일본 경쟁사 대비 가격 모니터링.",
    ("엔",      "Neutral"):  "{industry} 엔화 영향 제한적 — 현 수준 관찰 유지.",
    ("엔",      "Negative"): "{industry} 엔화 약세 — 일본산 경쟁 제품 가격 경쟁력 상승.",
}

# Groq prompt (English for better LLM performance, result must be Korean)
_GROQ_PROMPT = """\
You are a Korean export business macro analyst.
Given a macro signal, industry, and impact direction, write ONE concise Korean sentence (under 35 characters) describing the business impact.

Macro Signal: {macro_signal} {trend}
Industry: {industry}
Impact Direction: {direction}

Rules:
- Output ONLY the Korean sentence
- Maximum 35 Korean characters
- Start with the industry name
- End with a period (.)
- No explanation, no English
"""


def _rule_based_insight(label: str, industry_label: str, direction: str) -> str:
    """규칙 기반 fallback 인사이트."""
    for (keyword, dir_key), tmpl in _TEMPLATES.items():
        if keyword in label and dir_key == direction:
            return tmpl.format(industry=industry_label)
    # Generic fallback
    dir_ko = {"Positive": "긍정적", "Neutral": "중립적", "Negative": "부정적"}.get(direction, "중립적")
    return f"{industry_label} — {label} 변화의 {dir_ko} 영향 점검 필요."


def _groq_insight(
    macro_signal: str,
    trend: str,
    industry_label: str,
    direction: str,
) -> str | None:
    """Groq API로 인사이트 생성. 실패 시 None 반환."""
    try:
        from core.summarizer import _get_llm_key, _build_groq_client  # type: ignore[import]
        key = _get_llm_key()
        if not key:
            return None
        client = _build_groq_client(key)
        prompt = _GROQ_PROMPT.format(
            macro_signal=macro_signal,
            trend=trend,
            industry=industry_label,
            direction=direction,
        )
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.3,
        )
        result = resp.choices[0].message.content.strip()
        # Sanity: must be Korean-ish and short
        if result and 5 < len(result) < 80:
            return result
    except Exception:
        pass
    return None


@safe_execute(default="", log_prefix="ai_insight")
def generate_ai_insight(
    label: str,
    trend: str,
    industry_key: str,
    direction: str,
    industry_label: str = "",
    use_llm: bool = True,
) -> str:
    """
    거시경제 신호 + 산업 + 방향성 → 1문장 한국어 인사이트.

    LLM(Groq) 가용 시 AI 생성, 불가 시 규칙 기반 fallback.

    Args:
        label:          지표명 (e.g. "환율(원/$)")
        trend:          추세 ("▲" | "▼" | "→")
        industry_key:   산업 키
        direction:      "Positive" | "Neutral" | "Negative"
        industry_label: 산업 한국어명 (비어있으면 get_profile로 조회)
        use_llm:        Groq LLM 사용 여부 (False 시 규칙 기반만 사용)

    Returns:
        str: 1문장 한국어 인사이트
    """
    if not industry_label:
        try:
            from core.industry_config import get_profile
            industry_label = get_profile(industry_key).get("label", industry_key)
        except Exception:
            industry_label = industry_key

    if use_llm:
        llm_result = _groq_insight(label, trend, industry_label, direction)
        if llm_result:
            return llm_result

    return _rule_based_insight(label, industry_label, direction)
