"""tests/test_strategy_lab.py — Strategy Lab (Agent 3) 테스트."""
import pytest

from core.decision_engine import (
    _SCENARIO_PRESETS,
    compare_strategies,
    generate_scenario_strategies,
)


# ── generate_scenario_strategies 테스트 ───────────────────

class TestGenerateScenarioStrategies:
    """generate_scenario_strategies 함수 테스트."""

    def test_returns_list(self):
        result = generate_scenario_strategies({}, "반도체", "환율 1500 돌파")
        assert isinstance(result, list)

    def test_returns_3_options(self):
        result = generate_scenario_strategies({}, "반도체", "환율 1500 돌파")
        assert len(result) == 3

    def test_option_labels_abc(self):
        result = generate_scenario_strategies({}, "반도체", "환율 1500 돌파")
        labels = [o["option"] for o in result]
        assert labels == ["A", "B", "C"]

    def test_has_required_keys(self):
        result = generate_scenario_strategies({}, "반도체", "환율 1500 돌파")
        required = {"option", "title", "rationale", "urgency", "difficulty", "impact", "scenario"}
        for opt in result:
            assert required.issubset(opt.keys()), f"Missing keys: {required - opt.keys()}"

    def test_scenario_field_matches(self):
        result = generate_scenario_strategies({}, "자동차", "금리 인하")
        for opt in result:
            assert opt["scenario"] == "금리 인하"

    def test_unknown_scenario_returns_empty(self):
        result = generate_scenario_strategies({}, "반도체", "존재하지 않는 시나리오")
        assert result == []

    def test_fallback_industry_to_general(self):
        """등록되지 않은 산업은 '일반' 템플릿으로 fallback."""
        result = generate_scenario_strategies({}, "우주항공", "수출 급감")
        assert len(result) == 3
        # '일반' 템플릿의 수출 danger 전략이어야 함
        assert all(o.get("scenario") == "수출 급감" for o in result)

    def test_all_presets_produce_results(self):
        """모든 시나리오 프리셋이 반도체 산업에서 결과를 생성."""
        for scenario_name in _SCENARIO_PRESETS:
            result = generate_scenario_strategies({}, "반도체", scenario_name)
            assert len(result) > 0, f"No results for scenario: {scenario_name}"

    def test_yen_scenario_maps_to_fx(self):
        """엔저 심화 시나리오가 환율 카테고리로 매핑."""
        result = generate_scenario_strategies({}, "반도체", "엔저 심화")
        assert len(result) == 3


# ── compare_strategies 테스트 ─────────────────────────────

class TestCompareStrategies:
    """compare_strategies 함수 테스트."""

    def test_returns_required_keys(self):
        a = generate_scenario_strategies({}, "반도체", "환율 1500 돌파")
        b = generate_scenario_strategies({}, "반도체", "금리 인하")
        result = compare_strategies(a, b)
        assert "common_themes" in result
        assert "divergences" in result
        assert "urgency_shift" in result
        assert "risk_delta" in result

    def test_same_strategies_no_divergences(self):
        a = generate_scenario_strategies({}, "반도체", "환율 1500 돌파")
        result = compare_strategies(a, a)
        assert len(result["divergences"]) == 0
        assert result["urgency_shift"] == 0
        assert result["risk_delta"] == 0

    def test_empty_inputs(self):
        result = compare_strategies([], [])
        assert result["common_themes"] == []
        assert result["divergences"] == []
        assert result["urgency_shift"] == 0
        assert result["risk_delta"] == 0

    def test_divergences_detected(self):
        a = generate_scenario_strategies({}, "반도체", "환율 1500 돌파")
        b = generate_scenario_strategies({}, "반도체", "수출 급감")
        result = compare_strategies(a, b)
        # 환율 danger vs 수출 danger: 전략이 다르므로 divergences > 0
        assert len(result["divergences"]) > 0

    def test_urgency_shift_type(self):
        a = generate_scenario_strategies({}, "반도체", "환율 1500 돌파")
        b = generate_scenario_strategies({}, "반도체", "금리 인하")
        result = compare_strategies(a, b)
        assert isinstance(result["urgency_shift"], int)
        assert isinstance(result["risk_delta"], int)


# ── views import 테스트 ───────────────────────────────────

class TestStrategyLabImport:
    """views/strategy_lab.py가 정상 import 되는지 확인."""

    def test_import_render_strategy_lab(self):
        from views.strategy_lab import render_strategy_lab
        assert callable(render_strategy_lab)

    def test_import_render_scenario_selector(self):
        from views.strategy_lab import render_scenario_selector
        assert callable(render_scenario_selector)

    def test_import_render_strategy_comparison(self):
        from views.strategy_lab import render_strategy_comparison
        assert callable(render_strategy_comparison)

    def test_import_render_strategy_timeline(self):
        from views.strategy_lab import render_strategy_timeline
        assert callable(render_strategy_timeline)
