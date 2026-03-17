"""
core/shock_detector.py — 충격 감지 엔진
거시지표의 급변(spike/plunge/reversal)을 감지한다.
"""

from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHOCK_LOG_PATH = pathlib.Path(_BASE) / "data" / "shock_log.json"


def _parse_value(raw) -> float | None:
    """안전한 float 파싱."""
    try:
        return float(str(raw).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return None


# V11.1: 지표별 맞춤 임계값 + 계산 방식 (거짓 양성 방지)
#
# use_absolute=True  → 절대변화량(Δ) 비교  (비율·지수 지표: CPI 2.3→2.0 = -0.3pp)
# use_absolute=False → 상대변화율(%) 비교  (레벨 지표: 환율 1350→1481 = +9.7%)
#
# unit: 알림 메시지에 표시할 단위 (%, %p, 원 등)
_INDICATOR_THRESHOLDS = {
    # 레벨 지표 — 상대% 사용
    "환율(원/$)":     {"minor": 1.5,  "major": 3.0,  "extreme": 5.0,  "use_absolute": False, "unit": "%"},
    "원/100엔 환율":  {"minor": 1.5,  "major": 3.0,  "extreme": 5.0,  "use_absolute": False, "unit": "%"},
    # 비율 지표 — 절대pp 사용 (상대% 사용 시 0.3%→13% 같은 거짓 양성 발생)
    "소비자물가(CPI)": {"minor": 0.3,  "major": 0.7,  "extreme": 1.5,  "use_absolute": True,  "unit": "%p"},
    "기준금리":        {"minor": 0.25, "major": 0.5,  "extreme": 1.0,  "use_absolute": True,  "unit": "%p"},
    "수출증가율":      {"minor": 4.0,  "major": 8.0,  "extreme": 15.0, "use_absolute": True,  "unit": "%p"},
    # 지수/YoY 비율 지표 — 절대pp 사용 (YoY % 변화율 간 비교이므로 절대 pp가 정확)
    "수출물가지수":    {"minor": 3.0,  "major": 6.0,  "extreme": 10.0, "use_absolute": True,  "unit": "%p"},
    "수입물가지수":    {"minor": 3.0,  "major": 6.0,  "extreme": 10.0, "use_absolute": True,  "unit": "%p"},
    "경상수지":        {"minor": 20.0, "major": 40.0, "extreme": 70.0, "use_absolute": False, "unit": "%"},
}
_DEFAULT_THRESHOLDS = {"minor": 2.0, "major": 5.0, "extreme": 8.0, "use_absolute": False, "unit": "%"}

# V11: 한국어 알림 메시지 (사용자 친화)
_SHOCK_TYPE_KR = {"spike": "급등", "plunge": "급락", "reversal": "추세 반전"}
_SEVERITY_KR = {"extreme": "심각", "major": "주의", "minor": "참고"}


def _check_velocity(label: str, current: float, previous: float) -> dict | None:
    """V11.1: 지표별 맞춤 임계값 + 계산방식(상대%/절대pp) + 신뢰도 + 한국어 메시지.

    거짓 양성 방지:
    - 비율 지표(CPI, 기준금리, 수출증가율): 절대변화량(pp) 비교
      예) CPI 2.3→2.0 = -0.3pp → "급락 0.3%p" (상대% 사용 시 -13% 거짓 양성 방지)
    - 레벨/지수 지표(환율, 물가지수): 상대변화율(%) 비교
    """
    if previous == 0:
        return None

    delta = current - previous
    thresholds = _INDICATOR_THRESHOLDS.get(label, _DEFAULT_THRESHOLDS)
    use_absolute = thresholds.get("use_absolute", False)
    unit = thresholds.get("unit", "%")

    # 비교값: 절대변화량 or 상대변화율
    compare_val = abs(delta) if use_absolute else abs(delta / previous * 100)
    delta_pct = abs(delta / previous * 100)  # 메시지용 (참고)

    if compare_val < thresholds["minor"]:
        return None

    shock_type = "spike" if delta > 0 else "plunge"

    if compare_val >= thresholds["extreme"]:
        severity = "extreme"
    elif compare_val >= thresholds["major"]:
        severity = "major"
    else:
        severity = "minor"

    # V11: 신뢰도
    confidence = "high" if compare_val >= thresholds["major"] else "medium"

    # V11.1: 한국어 알림 메시지 — 절대지표는 pp 단위 표시
    _type_kr = _SHOCK_TYPE_KR.get(shock_type, shock_type)
    _sev_kr = _SEVERITY_KR.get(severity, severity)
    if use_absolute:
        alert_msg = f"{label} {_type_kr} {compare_val:.2f}{unit} [{_sev_kr}]"
    else:
        alert_msg = f"{label} {_type_kr} {compare_val:.1f}{unit} [{_sev_kr}]"

    return {
        "indicator": label,
        "shock_type": shock_type,
        "magnitude": round(compare_val, 2),
        "magnitude_pct": round(delta_pct, 2),
        "severity": severity,
        "confidence": confidence,
        "alert_msg": alert_msg,
        "detected_at": datetime.now().isoformat(),
        "current_value": current,
        "previous_value": previous,
    }


def _check_reversal(label: str, current: float, previous: float, trend: str) -> dict | None:
    """V11: 추세 반전 감지 — 지표별 맞춤 임계값 + 신뢰도 + 한국어 메시지.

    trend가 '▲'인데 current < previous → reversal (하락 반전)
    trend가 '▼'인데 current > previous → reversal (상승 반전)

    거짓 양성 방지: 반전 폭이 지표별 minor 임계값 미만이면 무시.
    """
    if previous == 0:
        return None

    delta = current - previous
    delta_pct = abs(delta / previous * 100)

    reversal_detected = False
    if trend == "▲" and current < previous:
        reversal_detected = True
    elif trend == "▼" and current > previous:
        reversal_detected = True

    if not reversal_detected:
        return None

    # V11.1: 지표별 임계값 + 계산방식
    thresholds = _INDICATOR_THRESHOLDS.get(label, _DEFAULT_THRESHOLDS)
    use_absolute = thresholds.get("use_absolute", False)
    unit = thresholds.get("unit", "%")

    compare_val = abs(delta) if use_absolute else delta_pct

    if compare_val < thresholds["minor"]:
        return None

    # V11: severity by per-indicator thresholds
    if compare_val >= thresholds["extreme"]:
        severity = "extreme"
    elif compare_val >= thresholds["major"]:
        severity = "major"
    else:
        severity = "minor"

    # V11: 신뢰도
    confidence = "high" if compare_val >= thresholds["major"] else "medium"

    # V11.1: 한국어 알림 메시지 — 단위 표시
    _sev_kr = _SEVERITY_KR.get(severity, severity)
    _dir_kr = "하락 반전" if delta < 0 else "상승 반전"
    if use_absolute:
        alert_msg = f"{label} 추세 반전({_dir_kr}) {compare_val:.2f}{unit} [{_sev_kr}]"
    else:
        alert_msg = f"{label} 추세 반전({_dir_kr}) {compare_val:.1f}{unit} [{_sev_kr}]"

    return {
        "indicator": label,
        "shock_type": "reversal",
        "magnitude": round(compare_val, 2),
        "magnitude_pct": round(delta_pct, 2),
        "severity": severity,
        "confidence": confidence,
        "alert_msg": alert_msg,
        "detected_at": datetime.now().isoformat(),
        "current_value": current,
        "previous_value": previous,
    }


def detect_shocks(macro_data: dict, prev_macro: dict | None = None) -> list[dict]:
    """거시지표 충격을 감지하여 리스트로 반환.

    Parameters:
        macro_data: 현재 macro.json 구조
        prev_macro: 이전 시점 macro_data (없으면 각 항목의 prev_value 사용)

    Returns:
        [{indicator, shock_type, magnitude, severity, alert_msg, detected_at}, ...]
    """
    if not macro_data:
        return []

    shocks: list[dict] = []

    for label, data in macro_data.items():
        if label.startswith("_") or not isinstance(data, dict):
            continue

        current = _parse_value(data.get("value"))
        if current is None:
            continue

        # previous value 결정
        if prev_macro and label in prev_macro and isinstance(prev_macro[label], dict):
            previous = _parse_value(prev_macro[label].get("value"))
        else:
            previous = _parse_value(data.get("prev_value"))

        if previous is None:
            continue

        trend = data.get("trend", "→")

        # velocity check
        velocity_shock = _check_velocity(label, current, previous)
        if velocity_shock:
            shocks.append(velocity_shock)

        # reversal check
        reversal_shock = _check_reversal(label, current, previous, trend)
        if reversal_shock:
            shocks.append(reversal_shock)

    # severity 기준 내림차순 정렬 후 최대 3개로 슬라이싱
    _SEV_ORDER = {"extreme": 3, "major": 2, "minor": 1}
    shocks.sort(key=lambda s: _SEV_ORDER.get(s.get("severity", "minor"), 0), reverse=True)
    shocks = shocks[:3]

    # 동일 방향 shock가 3개 이상이면 첫 번째 항목의 alert_msg를 복합 패턴 메시지로 교체
    spike_shocks = [s for s in shocks if s["shock_type"] == "spike"]
    plunge_shocks = [s for s in shocks if s["shock_type"] == "plunge"]
    for group in (spike_shocks, plunge_shocks):
        if len(group) >= 3:
            indicators = "·".join(s["indicator"] for s in group)
            direction = "상승" if group[0]["shock_type"] == "spike" else "하락"
            group[0]["alert_msg"] = f"⚠️ 복합 충격: {indicators} 동반 {direction} — 수입 원가 상승 압력 경고"

    # save detected shocks
    for shock in shocks:
        _save_shock(shock)

    return shocks


def get_shock_history(days: int = 30) -> list[dict]:
    """data/shock_log.json에서 최근 이력을 반환한다."""
    if not _SHOCK_LOG_PATH.exists():
        return []

    try:
        with open(_SHOCK_LOG_PATH, encoding="utf-8") as f:
            logs = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(logs, list):
        return []

    return logs[-days:]


def _save_shock(shock: dict) -> None:
    """충격 이벤트를 data/shock_log.json에 추가 저장한다."""
    try:
        if _SHOCK_LOG_PATH.exists():
            with open(_SHOCK_LOG_PATH, encoding="utf-8") as f:
                logs = json.load(f)
            if not isinstance(logs, list):
                logs = []
        else:
            logs = []

        logs.append(shock)

        # 최대 1000건 유지
        if len(logs) > 1000:
            logs = logs[-1000:]

        _SHOCK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SHOCK_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
