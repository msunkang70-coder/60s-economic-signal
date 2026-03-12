"""
core/risk_index.py — 복합 리스크 지수 엔진
거시지표 데이터를 기반으로 산업별 가중 리스크 지수를 산출한다.
"""

from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime

from core.industry_config import get_profile
from core.today_signal import _THRESHOLDS

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RISK_LOG_PATH = pathlib.Path(_BASE) / "data" / "risk_log.json"

# threshold status → base risk score (0-25 scale per indicator)
_STATUS_RISK = {
    "normal": 0,
    "caution": 8,
    "warning": 16,
    "danger": 25,
}


def _get_status(label: str, value: float) -> str:
    """지표 label과 value로 threshold status를 반환."""
    for lo, hi, status, *_ in _THRESHOLDS.get(label, []):
        if lo <= value < hi:
            return status
    return "normal"


def _indicator_risk_score(label: str, value: float, prev_value: float) -> float:
    """개별 지표의 리스크 점수 (0-25 기본 + velocity bonus).

    - threshold 기반: normal=0, caution=8, warning=16, danger=25
    - velocity bonus: |delta_pct| >= 5% → +8, >= 3% → +5
    """
    status = _get_status(label, value)
    base = _STATUS_RISK.get(status, 0)

    # velocity bonus
    bonus = 0.0
    if prev_value != 0:
        delta_pct = abs((value - prev_value) / prev_value * 100)
        if delta_pct >= 5.0:
            bonus = 8.0
        elif delta_pct >= 3.0:
            bonus = 5.0

    return base + bonus


def calculate_risk_index(macro_data: dict, industry_key: str = "일반") -> dict:
    """복합 리스크 지수를 산출한다.

    Parameters:
        macro_data: macro.json 구조의 딕셔너리
        industry_key: 산업 키 (get_profile에 전달)

    Returns:
        {score, level, breakdown, drivers, generated_at, industry}
    """
    if not macro_data:
        result = {
            "score": 0,
            "level": "low",
            "breakdown": {},
            "drivers": [],
            "generated_at": datetime.now().isoformat(),
            "industry": industry_key,
        }
        return result

    profile = get_profile(industry_key)
    weights = profile.get("macro_weights", {})

    breakdown: dict[str, dict] = {}
    weighted_sum = 0.0
    total_weight = 0.0

    for label, data in macro_data.items():
        if label.startswith("_") or not isinstance(data, dict):
            continue

        try:
            value = float(str(data.get("value", "0")).replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            continue

        try:
            prev_value = float(str(data.get("prev_value", str(value))).replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            prev_value = value

        risk = _indicator_risk_score(label, value, prev_value)
        w = weights.get(label, 1.0)

        breakdown[label] = {
            "value": value,
            "prev_value": prev_value,
            "risk_score": risk,
            "weight": w,
            "weighted_score": risk * w,
        }

        weighted_sum += risk * w
        total_weight += w

    # 0-100 정규화: max possible per indicator = 33 (25+8), 가중합 / (max_per * total_weight) * 100
    if total_weight > 0:
        max_possible = 33.0 * total_weight
        score = round(min(100.0, (weighted_sum / max_possible) * 100), 1)
    else:
        score = 0.0

    # level 결정
    if score >= 75:
        level = "critical"
    elif score >= 50:
        level = "high"
    elif score >= 25:
        level = "medium"
    else:
        level = "low"

    # drivers: top 3 by weighted_score
    sorted_items = sorted(
        breakdown.items(),
        key=lambda x: x[1]["weighted_score"],
        reverse=True,
    )
    drivers = [
        {"label": label, "weighted_score": info["weighted_score"], "risk_score": info["risk_score"]}
        for label, info in sorted_items[:3]
    ]

    result = {
        "score": score,
        "level": level,
        "breakdown": breakdown,
        "drivers": drivers,
        "generated_at": datetime.now().isoformat(),
        "industry": industry_key,
    }

    _save_risk_log(industry_key, score, level)

    return result


def get_risk_trend(industry_key: str = "일반", days: int = 7) -> list[dict]:
    """data/risk_log.json에서 최근 N일 이력을 반환한다."""
    if not _RISK_LOG_PATH.exists():
        return []

    try:
        with open(_RISK_LOG_PATH, encoding="utf-8") as f:
            logs = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(logs, list):
        return []

    # industry_key 필터
    filtered = [entry for entry in logs if entry.get("industry") == industry_key]

    # 최근 days개
    return filtered[-days:]


def _save_risk_log(industry_key: str, score: float, level: str) -> None:
    """리스크 로그를 data/risk_log.json에 추가 저장한다."""
    try:
        if _RISK_LOG_PATH.exists():
            with open(_RISK_LOG_PATH, encoding="utf-8") as f:
                logs = json.load(f)
            if not isinstance(logs, list):
                logs = []
        else:
            logs = []

        logs.append({
            "industry": industry_key,
            "score": score,
            "level": level,
            "timestamp": datetime.now().isoformat(),
        })

        # 최대 500건 유지
        if len(logs) > 500:
            logs = logs[-500:]

        _RISK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_RISK_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
