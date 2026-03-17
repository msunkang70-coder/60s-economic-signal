"""
core/scenario_engine.py
시나리오 분석 엔진 — 거시경제 변수 변동 시 산업별 영향도 시뮬레이션.

프리셋 시나리오(환율 급등, 유가 급락 등)를 선택하면
현재 거시지표에 변화분을 적용하여 임팩트 스코어 변화를 산출한다.
"""

import copy

from core.utils import safe_execute

from core.impact_scorer import calculate_macro_impact_score
from core.industry_config import get_profile

# ── 시나리오 프리셋 ─────────────────────────────────────────────
SCENARIO_PRESETS = {
    "환율_급등": {"USD_KRW": +5.0, "설명": "원/달러 5% 상승"},
    "환율_급락": {"USD_KRW": -5.0, "설명": "원/달러 5% 하락"},
    "유가_급등": {"OIL_WTI": +10.0, "설명": "WTI 유가 10% 상승"},
    "유가_급락": {"OIL_WTI": -10.0, "설명": "WTI 유가 10% 하락"},
    "금리_인상": {"BOK_RATE": +0.25, "설명": "기준금리 0.25%p 인상"},
    "복합_위기": {"USD_KRW": +5.0, "OIL_WTI": +10.0, "설명": "환율 + 유가 동시 상승"},
}

# 시나리오 변수 → macro.json 지표명 매핑
_SCENARIO_TO_MACRO = {
    "USD_KRW": "환율(원/$)",
    "OIL_WTI": "수입물가지수",   # WTI → 수입물가 proxy
    "BOK_RATE": "기준금리",
}

# 시나리오별 권고 액션 템플릿
_ACTION_TEMPLATES: dict[str, list[str]] = {
    "환율_급등": ["환헷지 비중 점검", "달러 결제 비중 확대 검토", "수출 단가 경쟁력 재평가"],
    "환율_급락": ["환헷지 포지션 축소 검토", "원화 결제 전환 가능성 검토", "수입 원가 절감 기회 활용"],
    "유가_급등": ["원자재 선물 헷지 확대", "물류비 상승 대비 가격 정책 검토", "에너지 효율화 투자 검토"],
    "유가_급락": ["원가 절감분 마진 확보 전략", "재고 확보 적기 판단", "경쟁사 가격 인하 대응 준비"],
    "금리_인상": ["차입 비용 재산정", "투자 우선순위 재조정", "운전자본 최적화 검토"],
    "복합_위기": ["환헷지 비중 점검", "원자재 비용 상승 대비 가격 전략", "수출 시장 다변화 가속"],
}


@safe_execute(default={}, log_prefix="scenario")
def simulate_scenario(
    base_macro: dict,
    scenario_key: str,
    industry_key: str,
) -> dict:
    """
    base_macro에 시나리오 변화분을 적용하여 산업별 영향도 변화를 산출한다.

    Args:
        base_macro: 현재 거시지표 (macro.json 로드 결과)
        scenario_key: SCENARIO_PRESETS 키
        industry_key: 산업 키 (예: '반도체')

    Returns:
        {
          "scenario": str,
          "scenario_desc": str,
          "industry": str,
          "impact_delta": float,
          "before_score": float,
          "after_score": float,
          "affected_kpis": [{"kpi": str, "before": float, "after": float}],
          "action_recommendations": [str],
        }
    """
    preset = SCENARIO_PRESETS.get(scenario_key)
    if not preset:
        return {
            "scenario": scenario_key,
            "scenario_desc": "알 수 없는 시나리오",
            "industry": industry_key,
            "impact_delta": 0.0,
            "before_score": 0.0,
            "after_score": 0.0,
            "affected_kpis": [],
            "action_recommendations": [],
        }

    # 1. 현재 스코어 계산
    before_result = calculate_macro_impact_score(base_macro, industry_key)
    before_score = before_result["total"]

    # 2. 시나리오 적용된 macro 생성
    scenario_macro = copy.deepcopy(base_macro)
    affected_kpis = []

    for var_key, delta in preset.items():
        if var_key == "설명":
            continue
        macro_label = _SCENARIO_TO_MACRO.get(var_key)
        if not macro_label or macro_label not in scenario_macro:
            continue

        item = scenario_macro[macro_label]
        try:
            current_val = float(
                str(item.get("value", "0")).replace(",", "").replace("+", "")
            )
        except (ValueError, TypeError):
            continue

        # 변화분 적용 (%, %p 구분)
        if var_key == "BOK_RATE":
            # 금리: 절대값 가산 (%p)
            new_val = round(current_val + delta, 2)
        else:
            # 환율·유가: 퍼센트 변동
            new_val = round(current_val * (1 + delta / 100), 2)

        item["value"] = str(new_val)

        # trend 재계산
        try:
            prev = float(
                str(item.get("prev_value", current_val))
                .replace(",", "").replace("+", "")
            )
            item["trend"] = "▲" if new_val > prev else ("▼" if new_val < prev else "→")
        except (ValueError, TypeError):
            pass

        affected_kpis.append({
            "kpi": macro_label,
            "before": current_val,
            "after": new_val,
        })

    # 3. 시나리오 적용 후 스코어 재계산
    after_result = calculate_macro_impact_score(scenario_macro, industry_key)
    after_score = after_result["total"]

    impact_delta = round(after_score - before_score, 1)

    # 4. 권고 액션
    actions = _ACTION_TEMPLATES.get(scenario_key, ["추가 분석 필요"])

    return {
        "scenario": scenario_key,
        "scenario_desc": preset.get("설명", ""),
        "industry": industry_key,
        "impact_delta": impact_delta,
        "before_score": before_score,
        "after_score": after_score,
        "affected_kpis": affected_kpis,
        "action_recommendations": actions,
    }
