"""tests/test_shock_detector.py — 충격 감지 엔진 테스트 (11개 이상)."""
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
def macro_spike():
    """급등 데이터 — 5% 이상 변화."""
    return {
        "환율(원/$)": {"value": "1500", "prev_value": "1400", "trend": "▲"},
        "수출증가율": {"value": "20", "prev_value": "10", "trend": "▲"},
    }


@pytest.fixture
def macro_many_shocks():
    """다수 충격 데이터 — 5개 이상 shock 발생 가능."""
    return {
        "환율(원/$)": {"value": "1500", "prev_value": "1400", "trend": "▲"},
        "소비자물가(CPI)": {"value": "3.5", "prev_value": "3.0", "trend": "▲"},
        "수출증가율": {"value": "20", "prev_value": "10", "trend": "▲"},
        "기준금리": {"value": "4.0", "prev_value": "3.5", "trend": "▲"},
        "수입물가지수": {"value": "10", "prev_value": "5", "trend": "▲"},
    }


@pytest.fixture
def macro_stable():
    """안정 데이터 — 변화 없음."""
    return {
        "환율(원/$)": {"value": "1350", "prev_value": "1350", "trend": "→"},
        "소비자물가(CPI)": {"value": "1.8", "prev_value": "1.8", "trend": "→"},
        "기준금리": {"value": "2.5", "prev_value": "2.5", "trend": "→"},
    }


@pytest.fixture
def macro_reversal():
    """추세 반전 데이터 — trend ▲인데 실제 하락."""
    return {
        "환율(원/$)": {"value": "1380", "prev_value": "1420", "trend": "▲"},
    }


# ── 테스트 ───────────────────────────────────────────────────

class TestDetectShocks:
    """detect_shocks 함수 테스트."""

    def test_returns_list(self, macro_data):
        """정상 입력 시 리스트 반환."""
        from core.shock_detector import detect_shocks
        result = detect_shocks(macro_data)
        assert isinstance(result, list)

    def test_empty_macro_returns_empty(self):
        """빈 입력 시 빈 리스트."""
        from core.shock_detector import detect_shocks
        result = detect_shocks({})
        assert result == []

    def test_none_macro_returns_empty(self):
        """None 입력 시 빈 리스트."""
        from core.shock_detector import detect_shocks
        result = detect_shocks(None)
        assert result == []

    def test_spike_detected(self, macro_spike):
        """급등 데이터에서 shock 감지."""
        from core.shock_detector import detect_shocks
        result = detect_shocks(macro_spike)
        assert len(result) > 0, "급등 데이터에서 shock이 감지되어야 함"
        shock_types = [s["shock_type"] for s in result]
        assert "spike" in shock_types

    def test_stable_no_shocks(self, macro_stable):
        """안정 데이터에서 shock 없음."""
        from core.shock_detector import detect_shocks
        result = detect_shocks(macro_stable)
        assert len(result) == 0, f"안정 데이터에서 shock이 감지됨: {result}"

    def test_shock_required_keys(self, macro_spike):
        """shock dict에 필수 키 존재."""
        from core.shock_detector import detect_shocks
        result = detect_shocks(macro_spike)
        required = {"indicator", "shock_type", "magnitude", "severity", "alert_msg", "detected_at"}
        for shock in result:
            assert required.issubset(shock.keys()), f"누락 키: {required - shock.keys()}"

    def test_severity_values(self, macro_data):
        """severity 값이 허용 범위 내."""
        from core.shock_detector import detect_shocks
        result = detect_shocks(macro_data)
        valid = {"minor", "major", "extreme"}
        for shock in result:
            assert shock["severity"] in valid, f"잘못된 severity: {shock['severity']}"

    def test_shock_type_values(self, macro_data):
        """shock_type 값이 허용 범위 내."""
        from core.shock_detector import detect_shocks
        result = detect_shocks(macro_data)
        valid = {"spike", "plunge", "reversal"}
        for shock in result:
            assert shock["shock_type"] in valid, f"잘못된 shock_type: {shock['shock_type']}"

    def test_reversal_detected(self, macro_reversal):
        """추세 반전 감지."""
        from core.shock_detector import detect_shocks
        result = detect_shocks(macro_reversal)
        reversal_shocks = [s for s in result if s["shock_type"] == "reversal"]
        assert len(reversal_shocks) > 0, "추세 반전이 감지되어야 함"

    def test_with_prev_macro(self):
        """prev_macro 파라미터로 이전 데이터 전달."""
        from core.shock_detector import detect_shocks
        current = {
            "환율(원/$)": {"value": "1500", "trend": "▲"},
        }
        prev = {
            "환율(원/$)": {"value": "1400"},
        }
        result = detect_shocks(current, prev_macro=prev)
        assert len(result) > 0

    def test_magnitude_positive(self, macro_spike):
        """magnitude는 양수."""
        from core.shock_detector import detect_shocks
        result = detect_shocks(macro_spike)
        for shock in result:
            assert shock["magnitude"] > 0

    def test_max_3_shocks(self, macro_many_shocks):
        """detect_shocks는 최대 3개만 반환."""
        from core.shock_detector import detect_shocks
        result = detect_shocks(macro_many_shocks)
        assert len(result) <= 3, f"3개 초과 반환: {len(result)}개"

    def test_shocks_sorted_by_severity(self, macro_many_shocks):
        """반환된 shock가 severity 내림차순으로 정렬."""
        from core.shock_detector import detect_shocks
        sev_order = {"extreme": 3, "major": 2, "minor": 1}
        result = detect_shocks(macro_many_shocks)
        if len(result) >= 2:
            for i in range(len(result) - 1):
                assert sev_order[result[i]["severity"]] >= sev_order[result[i + 1]["severity"]]


class TestCheckVelocity:
    """_check_velocity 내부 함수 테스트."""

    def test_below_threshold_returns_none(self):
        """2% 미만 변화 시 None."""
        from core.shock_detector import _check_velocity
        # 1.48% 변화 → threshold(2%) 미만이므로 None
        result = _check_velocity("환율(원/$)", 1370, 1350)
        assert result is None

    def test_minor_shock(self):
        """2-5% 변화 시 minor."""
        from core.shock_detector import _check_velocity
        # 약 3.7% 변화 → minor
        result = _check_velocity("환율(원/$)", 1400, 1350)
        assert result is not None
        assert result["severity"] == "minor"

    def test_major_shock(self):
        """5-8% 변화 시 major."""
        from core.shock_detector import _check_velocity
        # 약 5.9% 변화 → major
        result = _check_velocity("환율(원/$)", 1430, 1350)
        assert result is not None
        assert result["severity"] == "major"

    def test_extreme_shock(self):
        """8% 이상 변화 시 extreme."""
        from core.shock_detector import _check_velocity
        # 약 8.1% 변화 → extreme
        result = _check_velocity("환율(원/$)", 1460, 1350)
        assert result is not None
        assert result["severity"] == "extreme"

    def test_zero_previous_returns_none(self):
        """이전값이 0이면 None."""
        from core.shock_detector import _check_velocity
        result = _check_velocity("기준금리", 2.5, 0)
        assert result is None


class TestCheckReversal:
    """_check_reversal 내부 함수 테스트."""

    def test_no_reversal(self):
        """추세와 같은 방향이면 None."""
        from core.shock_detector import _check_reversal
        result = _check_reversal("환율(원/$)", 1400, 1350, "▲")
        assert result is None

    def test_reversal_up_trend_down_actual(self):
        """▲ 추세인데 실제 하락 → reversal."""
        from core.shock_detector import _check_reversal
        result = _check_reversal("환율(원/$)", 1300, 1400, "▲")
        assert result is not None
        assert result["shock_type"] == "reversal"


class TestGetShockHistory:
    """get_shock_history 함수 테스트."""

    def test_returns_list(self):
        """항상 list 반환."""
        from core.shock_detector import get_shock_history
        result = get_shock_history(30)
        assert isinstance(result, list)
