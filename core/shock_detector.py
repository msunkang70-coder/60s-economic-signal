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


def _check_velocity(label: str, current: float, previous: float) -> dict | None:
    """변화율 기반 충격 감지.

    |delta_pct| >= 2% → shock
      2-5%: minor, 5-8%: major, 8%+: extreme
    """
    if previous == 0:
        return None

    delta = current - previous
    delta_pct = abs(delta / previous * 100)

    if delta_pct < 2.0:
        return None

    # shock_type
    shock_type = "spike" if delta > 0 else "plunge"

    # severity
    if delta_pct >= 8.0:
        severity = "extreme"
    elif delta_pct >= 5.0:
        severity = "major"
    else:
        severity = "minor"

    return {
        "indicator": label,
        "shock_type": shock_type,
        "magnitude": round(delta_pct, 2),
        "severity": severity,
        "alert_msg": f"{label}: {shock_type} {delta_pct:.1f}% ({severity})",
        "detected_at": datetime.now().isoformat(),
    }


def _check_reversal(label: str, current: float, previous: float, trend: str) -> dict | None:
    """추세 반전 감지.

    trend가 '▲'인데 current < previous → reversal (하락 반전)
    trend가 '▼'인데 current > previous → reversal (상승 반전)
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

    # severity by magnitude
    if delta_pct >= 5.0:
        severity = "extreme"
    elif delta_pct >= 3.0:
        severity = "major"
    else:
        severity = "minor"

    return {
        "indicator": label,
        "shock_type": "reversal",
        "magnitude": round(delta_pct, 2),
        "severity": severity,
        "alert_msg": f"{label}: 추세 반전 감지 ({trend} → 실제 역방향 {delta_pct:.1f}%)",
        "detected_at": datetime.now().isoformat(),
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
