"""
core/industry_mapper.py
거시경제 신호 → 산업별 영향 매핑 및 전 산업 비교표 생성.
"""

from core.macro_signal_engine import detect_macro_signals
from core.industry_config import INDUSTRY_PROFILES

_COLOR_TEXT = {
    "green":  "긍정적",
    "yellow": "중립",
    "red":    "부정적",
}
_DEMAND_TEXT = {
    "green":  "강세",
    "yellow": "보통",
    "red":    "약세",
}


def map_industry_impact(signals: list[dict], top_n: int = 3) -> list[dict]:
    """
    상위 N개 신호를 산업별 영향 카드 형식으로 변환.

    Args:
        signals: detect_macro_signals() 반환 리스트 (이미 weight 정렬됨)
        top_n:   반환할 카드 수 (기본 3)

    Returns:
        [{
            "macro_signal":    str,  # e.g. "환율(원/$) ▲"
            "interpretation":  str,  # signal 필드 (경제적 해석)
            "industry_impact": str,  # impact 필드 (산업별 영향)
            "risk":            str,  # risk 필드
            "color":           str,
            "emoji":           str,
            "color_label":     str,
            "as_of":           str,
            "source_name":     str,
        }, ...]
    """
    cards = []
    for sig in signals[:top_n]:
        trend_str = sig.get("trend", "→")
        cards.append({
            "macro_signal":    f"{sig['label']} {trend_str}",
            "interpretation":  sig.get("signal", ""),
            "industry_impact": sig.get("impact", ""),
            "risk":            sig.get("risk", ""),
            "color":           sig.get("color", "yellow"),
            "emoji":           sig.get("emoji", "🟡"),
            "color_label":     sig.get("color_label", "주의"),
            "as_of":           sig.get("as_of", ""),
            "source_name":     sig.get("source_name", "한국은행 ECOS"),
        })
    return cards


def get_industry_comparison(macro_data: dict) -> list[dict]:
    """
    전체 산업 × 3개 차원(환율/금리/수요) 비교표 생성.

    Returns:
        [{
            "key":           str,  # 산업 key
            "label":         str,  # "반도체·디스플레이"
            "icon":          str,  # "🔬"
            "fx_impact":     str,  # "긍정적" | "중립" | "부정적"
            "fx_color":      str,
            "rate_impact":   str,
            "rate_color":    str,
            "demand_trend":  str,  # "강세" | "보통" | "약세"
            "demand_color":  str,
        }, ...]
    """
    rows = []
    for ind_key, profile in INDUSTRY_PROFILES.items():
        signals = detect_macro_signals(macro_data, ind_key)
        sig_map = {s["label"]: s for s in signals}

        # 환율 신호
        fx_sig = _find_signal(sig_map, ["환율(원/$)", "환율"])
        fx_color = fx_sig.get("color", "yellow") if fx_sig else "yellow"

        # 금리 신호
        rate_sig = _find_signal(sig_map, ["기준금리", "금리"])
        rate_color = rate_sig.get("color", "yellow") if rate_sig else "yellow"

        # 수출증가율 → 수요 동향
        demand_sig = _find_signal(sig_map, ["수출증가율", "수출 증가율"])
        demand_color = demand_sig.get("color", "yellow") if demand_sig else "yellow"

        rows.append({
            "key":          ind_key,
            "label":        profile.get("label", ind_key),
            "icon":         profile.get("icon", "📦"),
            "fx_impact":    _COLOR_TEXT.get(fx_color, "중립"),
            "fx_color":     fx_color,
            "rate_impact":  _COLOR_TEXT.get(rate_color, "중립"),
            "rate_color":   rate_color,
            "demand_trend": _DEMAND_TEXT.get(demand_color, "보통"),
            "demand_color": demand_color,
        })
    return rows


def _find_signal(sig_map: dict, keys: list[str]) -> dict | None:
    """sig_map에서 keys 중 하나와 매칭되는 신호 반환."""
    for k in keys:
        if k in sig_map:
            return sig_map[k]
        # 부분 매칭
        for label, sig in sig_map.items():
            if k in label:
                return sig
    return None
