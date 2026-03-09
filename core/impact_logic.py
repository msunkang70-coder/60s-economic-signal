"""
core/impact_logic.py
산업별 거시경제 신호 민감도 매핑 + 충격 방향 계산.

Functions:
  map_industry_sensitivity(industry_key) → sensitivity dict
  calculate_impact_direction(label, trend, industry_key, color) → "Positive" | "Neutral" | "Negative"
  get_direction_ko(direction) → Korean label

Items 8-9 of the dashboard intelligence spec.
"""

# ── 산업별 민감도 테이블 ─────────────────────────────────────────
# Values: "high" | "medium" | "low"
# Dimensions:
#   fx            — 환율(원/$) 변동
#   export_growth — 수출증가율
#   inflation     — 소비자물가 / 수입물가
#   interest_rate — 기준금리
#   regulation    — 규제·정책 변화 (reserved)
_SENSITIVITY_MAP: dict[str, dict[str, str]] = {
    "반도체·디스플레이": {
        "fx":            "high",    # 달러 결제 비중 매우 높음
        "export_growth": "high",    # 글로벌 수요 직결
        "inflation":     "medium",  # 원자재 일부 수입
        "interest_rate": "medium",  # R&D 투자 비용
        "regulation":    "high",    # 수출규제 노출
    },
    "자동차·부품": {
        "fx":            "high",
        "export_growth": "high",
        "inflation":     "medium",
        "interest_rate": "medium",
        "regulation":    "medium",
    },
    "2차전지·배터리": {
        "fx":            "high",
        "export_growth": "high",
        "inflation":     "high",    # 리튬·코발트 원자재 비용 민감
        "interest_rate": "medium",
        "regulation":    "high",
    },
    "조선·해양": {
        "fx":            "high",    # 달러 계약 비중 높음
        "export_growth": "medium",
        "inflation":     "medium",  # 후판·에너지 비용
        "interest_rate": "medium",
        "regulation":    "low",
    },
    "소비재·식품": {
        "fx":            "medium",
        "export_growth": "medium",
        "inflation":     "high",    # 식품 원자재 직결
        "interest_rate": "low",
        "regulation":    "low",
    },
    "석유화학·정밀화학": {
        "fx":            "high",
        "export_growth": "high",
        "inflation":     "high",    # 원유·나프타 원가 직결
        "interest_rate": "medium",
        "regulation":    "medium",
    },
    "철강·금속": {
        "fx":            "high",
        "export_growth": "high",
        "inflation":     "high",    # 원자재·에너지 비중 높음
        "interest_rate": "medium",
        "regulation":    "medium",
    },
    "일반 수출기업": {
        "fx":            "high",
        "export_growth": "high",
        "inflation":     "medium",
        "interest_rate": "medium",
        "regulation":    "medium",
    },
}

# ── 지표명 → 민감도 차원 매핑 ────────────────────────────────────
_LABEL_TO_DIM: dict[str, str] = {
    "환율":       "fx",
    "수출증가율":  "export_growth",
    "수출 증가율": "export_growth",
    "소비자물가":  "inflation",
    "CPI":        "inflation",
    "기준금리":   "interest_rate",
    "수입물가":   "inflation",
    "수출물가":   "fx",             # 수출 단가 — fx 차원에 포함
}

# ── 색상 → 기본 방향 매핑 ───────────────────────────────────────
_COLOR_TO_DIRECTION: dict[str, str] = {
    "green":  "Positive",
    "yellow": "Neutral",
    "red":    "Negative",
}

# ── 방향 한국어 레이블 ─────────────────────────────────────────
_DIRECTION_KO: dict[str, str] = {
    "Positive": "긍정적",
    "Neutral":  "중립",
    "Negative": "부정적",
}

# ── English signal type labels (items 4) ──────────────────────
_DIRECTION_EN: dict[str, str] = {
    "Positive": "Opportunity",
    "Neutral":  "Caution",
    "Negative": "Risk",
}

_DIRECTION_EMOJI: dict[str, str] = {
    "Positive": "🟢",
    "Neutral":  "🟡",
    "Negative": "🔴",
}


def map_industry_sensitivity(industry_key: str) -> dict[str, str]:
    """
    산업 키에 대한 민감도 dict 반환.
    매칭 불가 시 '일반 수출기업' 프로파일로 fallback.

    Returns:
        {"fx": "high"|"medium"|"low", "export_growth": ..., ...}
    """
    # 완전 일치 우선
    if industry_key in _SENSITIVITY_MAP:
        return _SENSITIVITY_MAP[industry_key]
    # 부분 문자열 매칭
    for key, profile in _SENSITIVITY_MAP.items():
        if key in industry_key or industry_key in key:
            return profile
    return _SENSITIVITY_MAP["일반 수출기업"]


def calculate_impact_direction(
    label: str,
    trend: str,
    industry_key: str,
    color: str,
) -> str:
    """
    신호 색상 + 산업 민감도 → 충격 방향 계산.

    Rule:
      - Base direction: color → Positive/Neutral/Negative
      - Low sensitivity dampens strong signals to Neutral
      - High sensitivity keeps or amplifies direction (no change here)

    Args:
        label:        지표명 (e.g. "환율(원/$)")
        trend:        "▲" | "▼" | "→"
        industry_key: 산업 키
        color:        "green" | "yellow" | "red"

    Returns:
        "Positive" | "Neutral" | "Negative"
    """
    base = _COLOR_TO_DIRECTION.get(color, "Neutral")
    sensitivity = map_industry_sensitivity(industry_key)

    # Resolve dimension
    dim = "fx"  # default
    for keyword, d in _LABEL_TO_DIM.items():
        if keyword in label:
            dim = d
            break

    sens_level = sensitivity.get(dim, "medium")

    # Low sensitivity → dampen to Neutral (edge cases only)
    if sens_level == "low" and base != "Neutral":
        return "Neutral"

    return base


def get_direction_ko(direction: str) -> str:
    """Returns Korean label: 긍정적 / 중립 / 부정적."""
    return _DIRECTION_KO.get(direction, "중립")


def get_direction_en(direction: str) -> str:
    """Returns English signal type: Opportunity / Caution / Risk."""
    return _DIRECTION_EN.get(direction, "Caution")


def get_direction_emoji(direction: str) -> str:
    """Returns emoji for direction."""
    return _DIRECTION_EMOJI.get(direction, "🟡")
