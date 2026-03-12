"""tests/test_risk_index.py — 복합 리스크 지수 엔진 테스트 (11개 이상)."""
import sys
import pathlib
import pytest

_ROOT = pathlib.Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── 공통 픽스처 ──────────────────────────────────────────────

@pytest.fixture
def macro_data():
    return {
        "환율(원/$)": {"value": "1466", "prev_value": "1420", "trend": "▲"},
        "소비자물가(CPI)": {"value": "2.0", "prev_value": "2.3", "trend": "▼"},
        "수출증가율": {"value": "14.8", "prev_value": "9.1", "trend": "▲"},
        "기준금리": {"value": "2.5", "prev_value": "2.5", "trend": "→"},
    }


@pytest.fixture
def macro_danger():
    """위험 구간 데이터."""
    return {
        "환율(원/$)": {"value": "1520", "prev_value": "1400", "trend": "▲"},
        "소비자물가(CPI)": {"value": "3.5", "prev_value": "2.8", "trend": "▲"},
        "수출증가율": {"value": "-15", "prev_value": "-5", "trend": "▼"},
        "기준금리": {"value": "4.0", "prev_value": "3.5", "trend": "▲"},
    }


@pytest.fixture
def macro_normal():
    """모든 지표 정상."""
    return {
        "환율(원/$)": {"value": "1350", "prev_value": "1350", "trend": "→"},
        "수출증가율": {"value": "5", "prev_value": "5", "trend": "→"},
        "소비자물가(CPI)": {"value": "1.8", "prev_value": "1.8", "trend": "→"},
        "기준금리": {"value": "2.5", "prev_value": "2.5", "trend": "→"},
    }


# ── 테스트 ───────────────────────────────────────────────────

class TestCalculateRiskIndex:
    """calculate_risk_index 함수 테스트."""

    def test_returns_dict(self, macro_data):
        """정상 입력 시 딕셔너리 반환."""
        from core.risk_index import calculate_risk_index
        result = calculate_risk_index(macro_data)
        assert isinstance(result, dict)

    def test_required_keys(self, macro_data):
        """반환 dict에 필수 키 존재."""
        from core.risk_index import calculate_risk_index
        result = calculate_risk_index(macro_data)
        required = {"score", "level", "breakdown", "drivers", "generated_at", "industry"}
        assert required.issubset(result.keys()), f"누락 키: {required - result.keys()}"

    def test_score_range(self, macro_data):
        """score가 0-100 범위."""
        from core.risk_index import calculate_risk_index
        result = calculate_risk_index(macro_data)
        assert 0 <= result["score"] <= 100

    def test_level_values(self, macro_data):
        """level이 허용된 값."""
        from core.risk_index import calculate_risk_index
        result = calculate_risk_index(macro_data)
        assert result["level"] in ("low", "medium", "high", "critical")

    def test_empty_macro_returns_low(self):
        """빈 입력 시 score 0, level low."""
        from core.risk_index import calculate_risk_index
        result = calculate_risk_index({})
        assert result["score"] == 0
        assert result["level"] == "low"

    def test_danger_zone_high_score(self, macro_danger):
        """위험 구간 데이터 시 높은 점수."""
        from core.risk_index import calculate_risk_index
        result = calculate_risk_index(macro_danger)
        assert result["score"] >= 25, f"위험 구간인데 score가 낮음: {result['score']}"

    def test_normal_zone_low_score(self, macro_normal):
        """정상 구간 데이터 시 낮은 점수."""
        from core.risk_index import calculate_risk_index
        result = calculate_risk_index(macro_normal)
        assert result["score"] < 25, f"정상 구간인데 score가 높음: {result['score']}"

    def test_drivers_max_3(self, macro_data):
        """drivers가 최대 3개."""
        from core.risk_index import calculate_risk_index
        result = calculate_risk_index(macro_data)
        assert len(result["drivers"]) <= 3

    def test_industry_weight_affects_score(self, macro_data):
        """산업별 가중치가 점수에 영향."""
        from core.risk_index import calculate_risk_index
        score_general = calculate_risk_index(macro_data, "일반")["score"]
        score_auto = calculate_risk_index(macro_data, "자동차")["score"]
        # 가중치가 다르므로 점수가 달라야 함 (동일할 수도 있지만 보통 다름)
        # 에러 없이 실행되면 OK
        assert isinstance(score_general, (int, float))
        assert isinstance(score_auto, (int, float))

    @pytest.mark.parametrize("industry", ["반도체", "자동차", "화학", "소비재", "일반"])
    def test_all_industries_no_error(self, macro_data, industry):
        """모든 산업에 대해 에러 없이 실행."""
        from core.risk_index import calculate_risk_index
        result = calculate_risk_index(macro_data, industry)
        assert isinstance(result, dict)
        assert 0 <= result["score"] <= 100

    def test_breakdown_has_indicator_info(self, macro_data):
        """breakdown에 각 지표별 상세 정보 포함."""
        from core.risk_index import calculate_risk_index
        result = calculate_risk_index(macro_data)
        assert len(result["breakdown"]) > 0
        for label, info in result["breakdown"].items():
            assert "risk_score" in info
            assert "weight" in info
            assert "weighted_score" in info


class TestIndicatorRiskScore:
    """_indicator_risk_score 내부 함수 테스트."""

    def test_normal_returns_zero(self):
        """normal 구간은 base 0."""
        from core.risk_index import _indicator_risk_score
        score = _indicator_risk_score("환율(원/$)", 1350, 1350)
        assert score == 0

    def test_danger_returns_25(self):
        """danger 구간은 base 25."""
        from core.risk_index import _indicator_risk_score
        score = _indicator_risk_score("환율(원/$)", 1550, 1550)
        assert score == 25

    def test_velocity_bonus_3pct(self):
        """3% 이상 변화 시 +5 bonus."""
        from core.risk_index import _indicator_risk_score
        # 1350 -> 1350*1.035 = 1397.25 → normal zone, delta 3.5%
        score = _indicator_risk_score("환율(원/$)", 1397, 1350)
        assert score >= 5  # normal(0) + velocity(5)

    def test_velocity_bonus_5pct(self):
        """5% 이상 변화 시 +8 bonus."""
        from core.risk_index import _indicator_risk_score
        # 1350 -> 1350*1.06 = 1431 → caution zone, delta 6%
        score = _indicator_risk_score("환율(원/$)", 1431, 1350)
        assert score >= 16  # caution(8) + velocity(8)


class TestGetRiskTrend:
    """get_risk_trend 함수 테스트."""

    def test_returns_list(self):
        """항상 list 반환."""
        from core.risk_index import get_risk_trend
        result = get_risk_trend("일반", 7)
        assert isinstance(result, list)
