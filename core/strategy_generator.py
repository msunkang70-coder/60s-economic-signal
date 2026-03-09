"""
core/strategy_generator.py
5-파트 전략 시사점 카드 생성기.

각 거시경제 신호를 아래 구조로 변환:
  1. 거시경제 신호   (Macro Signal)
  2. 경제적 해석     (Economic Interpretation)
  3. 산업별 영향     (Industry Impact)
  4. 전략적 시사점   (Strategic Insight)
  5. 실행 체크리스트 (Action Checklist)
"""

import re
from core.macro_signal_engine import detect_macro_signals


def _build_strategic_insight(impact: str, risk: str, label: str) -> str:
    """impact + risk 문장을 결합해 전략적 시사점 1~2문장 생성."""
    if impact and risk and risk != "—":
        return f"{impact}. 단, {risk}를 감안한 선제적 대응 전략 검토 필요."
    if impact:
        return f"{impact}. {label} 동향을 지속 모니터링하며 포지션 조정 검토."
    return f"{label} 변화에 따른 전략적 영향 검토 필요."


def _parse_action_checklist(action_str: str, label: str) -> list[str]:
    """
    action 문자열을 2~3개 체크리스트 항목으로 분리.
    쉼표 또는 '.' 기준으로 분리. 최소 2개 보장.
    """
    if not action_str or action_str == "동향 모니터링":
        return [
            f"{label} 최신 동향 모니터링",
            f"{label} 관련 리스크 노출도 점검",
        ]

    # 쉼표 또는 마침표로 분리
    parts = re.split(r"[,，。]", action_str)
    items = [p.strip() for p in parts if p.strip()]

    # 너무 짧은 항목 병합
    cleaned = []
    for item in items:
        if len(item) < 5:
            if cleaned:
                cleaned[-1] += f", {item}"
            continue
        cleaned.append(item)

    # 최소 2개 보장
    if len(cleaned) < 2:
        cleaned.append(f"{label} 관련 리스크 노출도 점검")

    return cleaned[:3]  # 최대 3개


def generate_strategic_insight(signal_dict: dict, industry_key: str) -> dict:
    """
    단일 신호 dict → 5-파트 전략 카드.

    Args:
        signal_dict: detect_macro_signals() 반환 리스트의 항목 하나
        industry_key: 선택된 산업 키

    Returns:
        {
            "macro_signal":     str,   # "환율(원/$) ▲"
            "interpretation":   str,   # 경제적 해석
            "industry_impact":  str,   # 산업별 영향
            "strategic_insight":str,   # 전략적 시사점 (1~2문장)
            "action_checklist": list,  # 실행 체크리스트 2~3개
            "color":            str,
            "emoji":            str,
            "color_label":      str,
            "label":            str,   # 원 지표명
            "weight":           float,
        }
    """
    label     = signal_dict.get("label", "")
    trend     = signal_dict.get("trend", "→")
    signal    = signal_dict.get("signal", "")
    impact    = signal_dict.get("impact", "")
    risk      = signal_dict.get("risk", "")
    action    = signal_dict.get("action", "")
    color     = signal_dict.get("color", "yellow")
    emoji     = signal_dict.get("emoji", "🟡")
    color_label = signal_dict.get("color_label", "주의")

    macro_signal_str = f"{label} {trend}" if trend and trend != "→" else label

    return {
        "macro_signal":     macro_signal_str,
        "interpretation":   signal,
        "industry_impact":  impact,
        "strategic_insight": _build_strategic_insight(impact, risk, label),
        "action_checklist": _parse_action_checklist(action, label),
        "color":            color,
        "emoji":            emoji,
        "color_label":      color_label,
        "label":            label,
        "weight":           signal_dict.get("weight", 1.0),
    }


def generate_all_insights(
    macro_data: dict,
    industry_key: str,
    top_n: int = 3,
) -> list[dict]:
    """
    상위 top_n개 신호에 대한 5-파트 전략 카드 리스트 반환.

    Args:
        macro_data:   data/macro.json 내용
        industry_key: 선택된 산업 키
        top_n:        생성할 카드 수 (기본 3)

    Returns:
        list[dict]  — generate_strategic_insight() 형식 카드 리스트
    """
    signals = detect_macro_signals(macro_data, industry_key)
    results = []
    for sig in signals[:top_n]:
        card = generate_strategic_insight(sig, industry_key)
        results.append(card)
    return results
