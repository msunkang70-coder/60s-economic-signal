"""
core/macro_signal_engine.py
거시경제 신호 감지 엔진

기존 signal_interpreter.interpret_all_signals() 위에 색상 코드 레이어 추가.
  🟢 green  — 기회 (수출 유리, 비용 감소 등)
  🟡 yellow — 주의 (일부 불확실, 이중 효과 등)
  🔴 red    — 위험 (비용 상승, 수요 둔화, 규제 등)
"""

from core.signal_interpreter import interpret_all_signals

# ── 지표별 색상 판정 규칙 ─────────────────────────────────────────
# (label_keyword, favorable_trend) → green/yellow/red
# favorable_trend: trend 방향이 수출업에 우호적이면 True
_FAVORABLE = {
    "환율":   {"▲": "green",  "▼": "red",    "→": "yellow"},  # 환율 상승 = 수출 유리
    "수출증가율": {"▲": "green",  "▼": "red",    "→": "yellow"},
    "수출물가지수": {"▲": "green", "▼": "yellow", "→": "yellow"},
    "수입물가지수": {"▲": "red",   "▼": "green",  "→": "yellow"},
    "소비자물가": {"▲": "red",   "▼": "green",  "→": "yellow"},
    "기준금리":  {"▲": "yellow", "▼": "green",  "→": "yellow"},
    "엔":        {"▲": "yellow", "▼": "yellow", "→": "yellow"},  # 엔화 방향성 복잡
}

_COLOR_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
_COLOR_LABEL = {"green": "기회", "yellow": "주의", "red": "위험"}

# 임계값 상태별 색상 override
_STATUS_OVERRIDE = {
    "danger":  "red",
    "warning": "red",
    "caution": "yellow",
    "normal":  None,   # trend 기반 색상 그대로 사용
}


def _label_to_favor_key(label: str) -> str:
    """지표명 → 색상 룰 키 매핑."""
    if "환율" in label and "엔" not in label and "100엔" not in label:
        return "환율"
    if "수출증가율" in label or "수출 증가율" in label:
        return "수출증가율"
    if "수출물가" in label:
        return "수출물가지수"
    if "수입물가" in label:
        return "수입물가지수"
    if "소비자물가" in label or "CPI" in label:
        return "소비자물가"
    if "금리" in label:
        return "기준금리"
    if "엔" in label:
        return "엔"
    return "환율"  # fallback


def _get_threshold_status(label: str, value_str: str) -> str:
    """임계값 대비 상태 반환 (normal/caution/warning/danger)."""
    # app.py의 _THRESHOLDS 데이터를 여기서 재정의 (중복 방지)
    _THRESHOLDS = {
        "환율(원/$)":    (1_300, 1_380, 1_500, 1_600),
        "소비자물가(CPI)": (-1.0, 0.0, 2.5, 4.0),
        "수출증가율":    (-10.0, -5.0, 15.0, 25.0),
        "기준금리":      (1.0, 2.0, 3.5, 4.5),
        "원/100엔 환율": (780, 850, 1_050, 1_150),
        "수출물가지수":  (-5.0, -3.0, 5.0, 8.0),
        "수입물가지수":  (-5.0, -3.0, 5.0, 8.0),
    }
    try:
        val = float(str(value_str).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return "normal"

    thresholds = None
    for key, thr in _THRESHOLDS.items():
        if key in label or label in key:
            thresholds = thr
            break
    if thresholds is None:
        return "normal"

    lo_d, lo_c, hi_c, hi_d = thresholds
    if val <= lo_d or val >= hi_d:
        return "danger"
    if val <= lo_c or val >= hi_c:
        return "warning"
    if val < lo_c * 1.05 or val > hi_c * 0.95:  # near boundary
        return "caution"
    return "normal"


def _assign_color(label: str, value_str: str, trend: str) -> str:
    """신호 색상 결정: 임계값 상태 + 트렌드 방향 조합."""
    status = _get_threshold_status(label, value_str)
    override = _STATUS_OVERRIDE.get(status)
    if override:
        return override
    # threshold가 normal이면 trend 기반
    fkey = _label_to_favor_key(label)
    rule = _FAVORABLE.get(fkey, {})
    return rule.get(trend, "yellow")


def detect_macro_signals(macro_data: dict, industry_key: str) -> list[dict]:
    """
    interpret_all_signals() 결과에 color/emoji/color_label + as_of/source_name 추가.

    Returns:
        [{
            "label", "value", "trend", "unit", "weight",
            "signal", "impact", "risk", "action",
            "color",        # "green" | "yellow" | "red"
            "emoji",        # "🟢" | "🟡" | "🔴"
            "color_label",  # "기회" | "주의" | "위험"
            "as_of",        # "2026-03-06" (data date)
            "source_name",  # "한국은행 ECOS"
        }, ...]
    """
    raw_signals = interpret_all_signals(macro_data, industry_key)

    enriched = []
    for sig in raw_signals:
        label = sig["label"]
        raw_item = macro_data.get(label, {})

        color = _assign_color(label, sig["value"], sig["trend"])
        enriched.append({
            **sig,
            "color":       color,
            "emoji":       _COLOR_EMOJI[color],
            "color_label": _COLOR_LABEL[color],
            "as_of":       raw_item.get("as_of", ""),
            "source_name": raw_item.get("source_name", "한국은행 ECOS"),
        })
    return enriched


def get_signal_summary(signals: list[dict]) -> dict:
    """
    신호 목록을 3색 분류 + 3줄 요약 브리핑으로 집계.

    Returns:
        {
            "green":  [signal_dict, ...],
            "yellow": [signal_dict, ...],
            "red":    [signal_dict, ...],
            "executive_lines": [str, str, str],  # 번호 붙인 3줄
        }
    """
    green  = [s for s in signals if s["color"] == "green"]
    yellow = [s for s in signals if s["color"] == "yellow"]
    red    = [s for s in signals if s["color"] == "red"]

    # 각 색에서 weight 최고 신호의 impact 문장 추출
    def _top_impact(lst: list) -> str:
        if not lst:
            return ""
        best = max(lst, key=lambda x: x.get("weight", 1.0))
        return best["impact"]

    lines_raw = [
        _top_impact(green),
        _top_impact(yellow),
        _top_impact(red),
    ]
    # 부족한 줄은 weight 순 top 신호로 보완
    fallback = sorted(signals, key=lambda x: -x.get("weight", 1.0))
    fi = 0
    for i, line in enumerate(lines_raw):
        if not line and fi < len(fallback):
            lines_raw[i] = fallback[fi]["impact"]
            fi += 1

    executive_lines = [
        f"① {lines_raw[0]}" if lines_raw[0] else "① (신호 없음)",
        f"② {lines_raw[1]}" if lines_raw[1] else "② (신호 없음)",
        f"③ {lines_raw[2]}" if lines_raw[2] else "③ (신호 없음)",
    ]

    return {
        "green":  green,
        "yellow": yellow,
        "red":    red,
        "executive_lines": executive_lines,
    }
