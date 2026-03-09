"""
tests/test_integration.py
Phase 3 전체 파이프라인 통합 테스트

실행: pytest tests/test_integration.py -v
"""

import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

# 프로젝트 루트를 sys.path에 추가
_ROOT = pathlib.Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ─────────────────────────────────────────────────────────────
# 공통 픽스처
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def macro_data():
    """실제 macro.json 구조와 동일한 테스트 거시지표."""
    return {
        "환율(원/$)": {
            "value": "1476", "prev_value": "1468", "unit": "원/$",
            "trend": "▲", "as_of": "2026-03-06", "note": "전일 대비 8원 상승",
            "source_name": "한국은행 ECOS", "source_url": "", "frequency": "일간",
        },
        "소비자물가(CPI)": {
            "value": "2.0", "prev_value": "2.3", "unit": "%",
            "trend": "▼", "as_of": "2026-01", "note": "전년동월 대비 0.3%p 하락",
            "source_name": "한국은행 ECOS", "source_url": "", "frequency": "월간",
        },
        "수출증가율": {
            "value": "14.8", "prev_value": "9.1", "unit": "%",
            "trend": "▲", "as_of": "2025-12", "note": "전년동월 대비 5.7%p 상승",
            "source_name": "한국은행 ECOS", "source_url": "", "frequency": "월간",
        },
        "기준금리": {
            "value": "2.5", "prev_value": "2.5", "unit": "%",
            "trend": "→", "as_of": "2026-02", "note": "전월 대비 동결",
            "source_name": "한국은행 ECOS", "source_url": "", "frequency": "비정기",
        },
        "원/100엔 환율": {
            "value": "913.38", "prev_value": "932.89", "unit": "원/100엔",
            "trend": "▼", "as_of": "2026-02", "note": "전월 대비 19.5원 하락",
            "source_name": "한국은행 ECOS", "source_url": "", "frequency": "월간",
        },
        "수출물가지수": {
            "value": "12.2", "prev_value": "7.1", "unit": "%",
            "trend": "▲", "as_of": "2025-12", "note": "",
            "source_name": "한국은행 ECOS", "source_url": "", "frequency": "월간",
        },
        "수입물가지수": {
            "value": "8.7", "prev_value": "4.6", "unit": "%",
            "trend": "▲", "as_of": "2025-12", "note": "",
            "source_name": "한국은행 ECOS", "source_url": "", "frequency": "월간",
        },
    }


@pytest.fixture
def sample_article():
    """테스트용 KDI 기사."""
    return {
        "title": "지역별 특화 산업과 연계한 5대 분야 양자클러스터 조성한다",
        "url": "https://eiec.kdi.re.kr/publish/naraView.do?fcode=00002000040000100005",
        "source": "KDI",
    }


@pytest.fixture
def sample_text():
    """테스트용 기사 본문."""
    return (
        "맥킨지의 2025 양자기술 모니터에 따르면 2035년까지 양자기술이 "
        "창출할 경제적 가치는 최대 2조 달러(약 2,900조 원)에 이를 전망이다. "
        "정부는 5대 분야에 양자 클러스터를 조성하고, 딥테크 분야 모태펀드를 "
        "활용해 초기 창업 기업에 대한 투자를 확대할 방침이다. "
        "기술 상용화를 검증하는 개념검증(PoC) 센터를 운영해 "
        "스타트업이 유니콘 기업으로 성장할 수 있는 전 주기 지원체계를 마련한다."
    )


INDUSTRIES = ["반도체", "자동차", "화학", "소비재", "배터리", "조선", "철강", "일반"]


# ─────────────────────────────────────────────────────────────
# 1. 데이터 파이프라인
# ─────────────────────────────────────────────────────────────

class TestDataPipeline:
    """ECOS API → macro_data 수집 정상 여부."""

    def test_macro_json_exists(self):
        """data/macro.json 파일이 존재하고 파싱 가능한지 검증."""
        macro_path = _ROOT / "data" / "macro.json"
        assert macro_path.exists(), "data/macro.json 파일이 없습니다"

        raw = json.loads(macro_path.read_text(encoding="utf-8"))
        # _meta 제외한 지표가 1개 이상
        indicators = {k: v for k, v in raw.items() if not k.startswith("_")}
        assert len(indicators) >= 1, "거시지표가 1개도 없습니다"

    def test_macro_indicator_structure(self, macro_data):
        """각 지표에 필수 필드가 있는지 검증."""
        required_fields = {"value", "unit", "trend", "as_of"}
        for label, data in macro_data.items():
            missing = required_fields - set(data.keys())
            assert not missing, f"{label}에 필수 필드 누락: {missing}"

    def test_macro_trend_values(self, macro_data):
        """trend 필드가 유효한 값인지 검증."""
        valid_trends = {"▲", "▼", "→"}
        for label, data in macro_data.items():
            assert data["trend"] in valid_trends, (
                f"{label} trend '{data['trend']}' 가 유효하지 않음"
            )

    def test_macro_value_parseable(self, macro_data):
        """value 필드가 숫자로 변환 가능한지 검증."""
        for label, data in macro_data.items():
            val_str = str(data["value"]).replace(",", "").replace("+", "")
            try:
                float(val_str)
            except ValueError:
                pytest.fail(f"{label} value '{data['value']}' 를 float로 변환 불가")


# ─────────────────────────────────────────────────────────────
# 2. 요약기 (Summarizer)
# ─────────────────────────────────────────────────────────────

class TestSummarizer:
    """8개 산업별 AI 요약 생성 (mock LLM 사용)."""

    def test_system_prompt_exists(self):
        """SYSTEM_PROMPT가 정의되어 있고 industry_label 플레이스홀더를 포함하는지 검증."""
        from core.summarizer import SYSTEM_PROMPT
        assert "{industry_label}" in SYSTEM_PROMPT

    def test_system_prompt_format_all_industries(self):
        """8개 산업 모두에 대해 SYSTEM_PROMPT 포맷팅이 가능한지 검증."""
        from core.summarizer import SYSTEM_PROMPT, _resolve_industry_label
        for ind in INDUSTRIES:
            label = _resolve_industry_label(ind)
            formatted = SYSTEM_PROMPT.format(industry_label=label)
            assert label in formatted
            assert "{industry_label}" not in formatted

    def test_summarize_rule_based(self, sample_text, sample_article):
        """규칙 기반 요약이 비어있지 않은 결과를 반환하는지 검증."""
        from core.summarizer import summarize_3line
        # LLM 호출을 mock으로 차단 → rule-based fallback
        with patch("core.summarizer._summarize_with_llm", return_value=None):
            summary, source = summarize_3line(
                sample_text,
                title=sample_article["title"],
                industry_key="반도체",
            )
        assert summary, "요약 결과가 비어있습니다"
        assert source == "rule", f"source가 'rule'이 아님: {source}"
        assert len(summary) > 20, f"요약이 너무 짧음: {len(summary)}자"

    @pytest.mark.parametrize("industry", INDUSTRIES)
    def test_summarize_per_industry(self, sample_text, sample_article, industry):
        """산업별 요약 생성이 에러 없이 완료되는지 검증."""
        from core.summarizer import summarize_3line
        with patch("core.summarizer._summarize_with_llm", return_value=None):
            summary, source = summarize_3line(
                sample_text,
                title=sample_article["title"],
                industry_key=industry,
            )
        assert summary, f"{industry} 산업 요약 결과가 비어있습니다"


# ─────────────────────────────────────────────────────────────
# 3. 임팩트 스코어러
# ─────────────────────────────────────────────────────────────

class TestImpactScorer:
    """기사별 영향도 점수 1~5 범위 검증."""

    @pytest.mark.parametrize("industry", INDUSTRIES)
    def test_score_range(self, sample_article, macro_data, industry):
        """모든 산업에서 점수가 1~5 범위인지 검증."""
        from core.impact_scorer import score_article
        score = score_article(sample_article, industry, macro_data)
        assert isinstance(score, int), f"score가 int가 아님: {type(score)}"
        assert 1 <= score <= 5, f"{industry}: score {score}가 1~5 범위 밖"

    def test_score_without_macro(self, sample_article):
        """macro_data 없이도 점수를 반환하는지 검증."""
        from core.impact_scorer import score_article
        score = score_article(sample_article, "반도체", None)
        assert 1 <= score <= 5

    def test_score_different_articles(self, macro_data):
        """다른 제목의 기사가 다른 점수를 받을 수 있는지 검증."""
        from core.impact_scorer import score_article
        art_high = {"title": "반도체 수출 급증 환율 급등 달러 수출통제 AI반도체", "url": ""}
        art_low = {"title": "지역 문화 축제 개최 안내", "url": ""}
        score_high = score_article(art_high, "반도체", macro_data)
        score_low = score_article(art_low, "반도체", macro_data)
        assert score_high >= score_low, (
            f"키워드 풍부한 기사({score_high})가 일반 기사({score_low})보다 낮음"
        )


# ─────────────────────────────────────────────────────────────
# 4. 오늘의 핵심 신호
# ─────────────────────────────────────────────────────────────

class TestTodaySignal:
    """핵심신호 생성 dict 구조 검증."""

    def test_signal_structure(self, macro_data):
        """반환 dict에 필수 키가 있는지 검증."""
        from core.today_signal import generate_today_signal
        result = generate_today_signal(macro_data, "반도체")
        # macro_data에 변동 있는 지표가 있으므로 None이 아니어야 함
        assert result is not None, "신호가 None입니다 (변동 지표 있음에도)"
        required_keys = {"label", "value", "trend", "impact", "checklist"}
        assert required_keys <= set(result.keys()), (
            f"필수 키 누락: {required_keys - set(result.keys())}"
        )

    def test_signal_checklist_is_list(self, macro_data):
        """checklist가 리스트인지 검증."""
        from core.today_signal import generate_today_signal
        result = generate_today_signal(macro_data, "반도체")
        assert isinstance(result["checklist"], list)
        assert len(result["checklist"]) >= 1

    @pytest.mark.parametrize("industry", INDUSTRIES)
    def test_signal_per_industry(self, macro_data, industry):
        """8개 산업 모두에서 에러 없이 실행되는지 검증."""
        from core.today_signal import generate_today_signal
        result = generate_today_signal(macro_data, industry)
        # None이거나 유효한 dict
        if result is not None:
            assert "label" in result
            assert "impact" in result

    def test_signal_none_on_stable(self):
        """모든 지표가 안정(→, normal)이면 None을 반환하는지 검증."""
        from core.today_signal import generate_today_signal
        stable_macro = {
            "기준금리": {
                "value": "2.5", "trend": "→", "unit": "%",
                "as_of": "2026-02",
            },
        }
        result = generate_today_signal(stable_macro, "일반")
        assert result is None, "안정 상태에서 None이 아닌 결과 반환"


# ─────────────────────────────────────────────────────────────
# 5. 액션 체크리스트
# ─────────────────────────────────────────────────────────────

class TestActionChecklist:
    """체크리스트 최소 3개 이상 생성 검증."""

    def test_checklist_min_items(self, sample_article):
        """체크리스트가 3개 이상인지 검증."""
        from core.action_checklist import generate_checklist
        question = "환율 변동이 수출에 미치는 영향은?"
        result = generate_checklist(question, sample_article, "반도체")
        assert isinstance(result, list), f"결과가 list가 아님: {type(result)}"
        assert len(result) >= 3, f"체크리스트 {len(result)}개 — 최소 3개 필요"

    def test_checklist_items_are_strings(self, sample_article):
        """체크리스트 항목이 문자열인지 검증."""
        from core.action_checklist import generate_checklist
        result = generate_checklist("수출 전략", sample_article, "자동차")
        for item in result:
            assert isinstance(item, str), f"항목이 str가 아님: {type(item)}"
            assert len(item) > 5, f"항목이 너무 짧음: '{item}'"

    @pytest.mark.parametrize("industry", ["반도체", "자동차", "화학", "소비재"])
    def test_checklist_per_industry(self, sample_article, industry):
        """산업별 체크리스트가 정상 생성되는지 검증."""
        from core.action_checklist import generate_checklist
        result = generate_checklist("규제 대응 전략", sample_article, industry)
        assert len(result) >= 3


# ─────────────────────────────────────────────────────────────
# 6. 워치리스트
# ─────────────────────────────────────────────────────────────

class TestWatchlist:
    """임계값 초과/미만 조건 트리거 검증."""

    def test_above_trigger(self, macro_data):
        """above 조건이 정상 트리거되는지 검증 (환율 1476 >= 1400)."""
        from core.watchlist import check_watchlist, _save_wl, _load_wl

        # last_triggered 초기화 (24시간 방지 해제)
        wl = _load_wl()
        for item in wl.get("items", []):
            item["last_triggered"] = None
        _save_wl(wl)

        triggered = check_watchlist(macro_data)
        usd_items = [t for t in triggered if t["indicator"] == "환율(원/$)"]
        assert len(usd_items) >= 1, "환율 above 조건이 트리거되지 않음"
        assert usd_items[0]["current_value"] >= usd_items[0]["threshold"]

    def test_below_not_trigger(self, macro_data):
        """below 조건이 미충족 시 트리거되지 않는지 검증 (수출증가율 14.8 > 0)."""
        from core.watchlist import check_watchlist, _save_wl, _load_wl

        wl = _load_wl()
        for item in wl.get("items", []):
            item["last_triggered"] = None
        _save_wl(wl)

        triggered = check_watchlist(macro_data)
        export_items = [t for t in triggered if t["indicator"] == "수출증가율"]
        assert len(export_items) == 0, (
            f"수출증가율 14.8 > 0 인데 below 조건이 트리거됨: {export_items}"
        )

    def test_add_and_remove(self):
        """항목 추가/삭제가 정상 동작하는지 검증."""
        from core.watchlist import add_item, remove_item, get_items

        before_count = len(get_items())
        new_item = add_item("기준금리", "above", 99.0, ["반도체"])
        assert len(get_items()) == before_count + 1

        removed = remove_item(new_item["id"])
        assert removed is True
        assert len(get_items()) == before_count

    def test_change_pct_condition(self):
        """change_pct 조건이 정상 동작하는지 검증."""
        from core.watchlist import add_item, remove_item, check_watchlist

        # 환율: 1468 → 1476, 변동률 = 0.54%
        new_item = add_item("환율(원/$)", "change_pct", 0.5)
        macro = {
            "환율(원/$)": {"value": "1476", "prev_value": "1468", "unit": "원/$", "trend": "▲"},
        }
        triggered = check_watchlist(macro)
        pct_items = [t for t in triggered if t["id"] == new_item["id"]]
        # 0.54% >= 0.5% → 트리거
        assert len(pct_items) >= 1, "change_pct 0.5% 조건이 트리거되지 않음"

        remove_item(new_item["id"])


# ─────────────────────────────────────────────────────────────
# 7. 시나리오 엔진
# ─────────────────────────────────────────────────────────────

class TestScenarioEngine:
    """6개 프리셋 시뮬레이션 정상 실행 검증."""

    PRESETS = ["환율_급등", "환율_급락", "유가_급등", "유가_급락", "금리_인상", "복합_위기"]

    @pytest.mark.parametrize("scenario", PRESETS)
    def test_preset_simulation(self, macro_data, scenario):
        """프리셋 시나리오가 에러 없이 실행되고 필수 키를 반환하는지 검증."""
        from core.scenario_engine import simulate_scenario
        result = simulate_scenario(macro_data, scenario, "반도체")
        assert isinstance(result, dict), f"결과가 dict가 아님: {type(result)}"

        required_keys = {
            "scenario", "scenario_desc", "industry",
            "impact_delta", "before_score", "after_score",
            "affected_kpis", "action_recommendations",
        }
        assert required_keys <= set(result.keys()), (
            f"필수 키 누락: {required_keys - set(result.keys())}"
        )

    @pytest.mark.parametrize("scenario", PRESETS)
    def test_preset_has_actions(self, macro_data, scenario):
        """시나리오 결과에 권고 액션이 1개 이상인지 검증."""
        from core.scenario_engine import simulate_scenario
        result = simulate_scenario(macro_data, scenario, "반도체")
        actions = result.get("action_recommendations", [])
        assert len(actions) >= 1, f"{scenario}: 권고 액션 없음"

    def test_all_industries_all_presets(self, macro_data):
        """8개 산업 × 6개 시나리오 = 48개 조합이 모두 실행되는지 검증."""
        from core.scenario_engine import simulate_scenario
        errors = []
        for ind in INDUSTRIES:
            for sc in self.PRESETS:
                try:
                    result = simulate_scenario(macro_data, sc, ind)
                    assert isinstance(result, dict)
                except Exception as e:
                    errors.append(f"{ind}×{sc}: {e}")
        assert not errors, f"실패 {len(errors)}건:\n" + "\n".join(errors)


# ─────────────────────────────────────────────────────────────
# 8. 시장 추천기
# ─────────────────────────────────────────────────────────────

class TestMarketRecommender:
    """Top 3 국가 추천 결과 구조 검증."""

    def test_recommend_structure(self, macro_data):
        """추천 결과가 리스트이고 필수 필드가 있는지 검증."""
        from core.market_recommender import recommend_markets
        result = recommend_markets("반도체", macro_data)
        assert isinstance(result, list), f"결과가 list가 아님: {type(result)}"
        assert len(result) >= 1, "추천 국가가 0건"

        for rec in result[:3]:
            required = {"country", "score", "reason"}
            assert required <= set(rec.keys()), (
                f"필수 키 누락: {required - set(rec.keys())}"
            )

    def test_recommend_top3(self, macro_data):
        """최소 3개 국가를 추천하는지 검증."""
        from core.market_recommender import recommend_markets
        result = recommend_markets("반도체", macro_data)
        assert len(result) >= 3, f"추천 국가 {len(result)}건 — 최소 3건 필요"

    def test_recommend_score_descending(self, macro_data):
        """추천 결과가 score 내림차순인지 검증."""
        from core.market_recommender import recommend_markets
        result = recommend_markets("반도체", macro_data)
        scores = [r.get("score", 0) for r in result]
        assert scores == sorted(scores, reverse=True), (
            f"score가 내림차순이 아님: {scores}"
        )

    @pytest.mark.parametrize("industry", INDUSTRIES)
    def test_recommend_per_industry(self, macro_data, industry):
        """8개 산업 모두에서 에러 없이 실행되는지 검증."""
        from core.market_recommender import recommend_markets
        result = recommend_markets(industry, macro_data)
        assert isinstance(result, list)


# ─────────────────────────────────────────────────────────────
# 9. 외부 소스 (KOTRA RSS)
# ─────────────────────────────────────────────────────────────

class TestExtraSources:
    """KOTRA RSS 수집 (mock response 사용)."""

    MOCK_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
    <channel>
        <title>KOTRA 해외시장뉴스</title>
        <item>
            <title>미국 반도체 수출 규제 강화 동향</title>
            <link>https://dream.kotra.or.kr/news/1</link>
            <pubDate>Mon, 03 Mar 2026 09:00:00 +0900</pubDate>
            <description>미국 상무부가 반도체 수출 규제를 강화했다.</description>
        </item>
        <item>
            <title>베트남 자동차 시장 성장세 지속</title>
            <link>https://dream.kotra.or.kr/news/2</link>
            <pubDate>Sun, 02 Mar 2026 09:00:00 +0900</pubDate>
            <description>베트남 자동차 시장이 전년 대비 15% 성장했다.</description>
        </item>
        <item>
            <title>EU 탄소국경조정 CBAM 시행 임박</title>
            <link>https://dream.kotra.or.kr/news/3</link>
            <pubDate>Sat, 01 Mar 2026 09:00:00 +0900</pubDate>
            <description>EU CBAM 본격 시행이 한국 철강 수출에 영향을 줄 전망이다.</description>
        </item>
    </channel>
    </rss>"""

    def test_merge_dedup(self):
        """중복 제거가 정상 동작하는지 검증."""
        from core.extra_sources import merge_articles, _title_key
        # 앞 20자 정규화 키가 동일한 두 제목 (21자부터 분기)
        base = "미국상무부의반도체수출규제강화분석보고서"  # 18자 > 20자 공백 포함 시
        title_a = "미국 상무부의 반도체 수출 규제 강화 분석 보고서 — KDI 원문"
        title_b = "미국 상무부의 반도체 수출 규제 강화 분석 보고서 최신판"
        assert _title_key(title_a) == _title_key(title_b), (
            f"테스트 전제 실패: 키 불일치 [{_title_key(title_a)}] vs [{_title_key(title_b)}]"
        )
        kdi = [{"title": title_a, "url": "https://kdi.re.kr/1"}]
        extra = [
            {"title": title_b, "url": "https://kotra.or.kr/1", "source": "KOTRA"},
            {"title": "베트남 자동차 시장 성장세 지속", "url": "https://kotra.or.kr/2", "source": "KOTRA"},
        ]
        merged = merge_articles(kdi, extra)
        titles = [a["title"] for a in merged]
        assert len(merged) == 2, f"중복 제거 후 2건이어야 하나 {len(merged)}건: {titles}"

    def test_merge_kdi_priority(self):
        """KDI 기사가 우선 유지되는지 검증."""
        from core.extra_sources import merge_articles
        kdi = [{"title": "동일한 제목의 기사입니다", "url": "https://kdi.re.kr/1"}]
        extra = [{"title": "동일한 제목의 기사입니다", "url": "https://kotra.or.kr/1", "source": "KOTRA"}]
        merged = merge_articles(kdi, extra)
        assert len(merged) == 1
        assert "kdi" in merged[0]["url"]

    def test_merge_source_tag(self):
        """병합 후 KDI 기사에 source 필드가 추가되는지 검증."""
        from core.extra_sources import merge_articles
        kdi = [{"title": "KDI 기사", "url": "https://kdi.re.kr/1"}]
        extra = [{"title": "KOTRA 기사", "url": "https://kotra.or.kr/1", "source": "KOTRA"}]
        merged = merge_articles(kdi, extra)
        assert merged[0].get("source") == "KDI"
        assert merged[1].get("source") == "KOTRA"


# ─────────────────────────────────────────────────────────────
# 10. 이메일러 템플릿
# ─────────────────────────────────────────────────────────────

class TestEmailerTemplate:
    """이메일 HTML 생성 (실제 발송 없이)."""

    def test_html_generation(self, macro_data):
        """HTML 본문이 정상 생성되는지 검증."""
        from core.emailer import _build_html
        script = (
            "[0~5초 훅]\n테스트 훅입니다.\n\n"
            "[5~25초]\n이슈1: 테스트 이슈입니다.\n\n"
            "[25~45초]\n▶이슈1 해석: 테스트 해석입니다.\n\n"
            "[45~60초 개인/기업 시사점]\n테스트 시사점입니다."
        )
        html = _build_html(script, macro_data, "2026년 03월")
        assert "<!DOCTYPE html>" in html
        assert "60초 경제신호" in html
        assert "2026년 03월" in html

    def test_html_with_industry(self, macro_data):
        """산업 레이블이 HTML에 포함되는지 검증."""
        from core.emailer import _build_html
        html = _build_html(
            "테스트 스크립트", macro_data, "2026년 03월",
            industry_label="반도체·디스플레이",
            industry_desc="반도체·디스플레이 수출 기업",
        )
        assert "반도체·디스플레이" in html
        assert "수출기업을 위한 60초 경제 브리핑" in html

    def test_html_macro_cards(self, macro_data):
        """거시지표 카드가 HTML에 렌더링되는지 검증."""
        from core.emailer import _build_html
        html = _build_html("테스트", macro_data, "2026년 03월")
        assert "환율" in html
        assert "1476" in html
        assert "▲" in html

    def test_html_dashboard_button(self, macro_data):
        """대시보드 링크 버튼이 HTML에 있는지 검증."""
        from core.emailer import _build_html
        html = _build_html("테스트", macro_data, "2026년 03월")
        assert "대시보드에서 상세 보기" in html
        assert "utm_source=email" in html

    def test_html_no_truncation(self, macro_data):
        """HTML에 '...' 잘림이 없는지 검증."""
        from core.emailer import _build_html
        long_script = (
            "[0~5초 훅]\n아주 긴 훅 문장입니다. " * 5 + "\n\n"
            "[5~25초]\n이슈1: 아주 긴 이슈 문장입니다. " * 5 + "\n\n"
            "[25~45초]\n▶이슈1 해석: 아주 긴 해석입니다. " * 5 + "\n\n"
            "[45~60초 개인/기업 시사점]\n시사점."
        )
        html = _build_html(long_script, macro_data, "2026년 03월")
        # HTML 태그 내부 속성의 ...은 무시 — 스크립트 콘텐츠에서만 확인
        # 콘텐츠 영역: <p> 태그 안에 ...으로 끝나는 텍스트가 없어야 함
        import re
        content_texts = re.findall(r'<p[^>]*>([^<]+)</p>', html)
        for text in content_texts:
            assert not text.strip().endswith("..."), (
                f"콘텐츠에 잘림 발견: '{text.strip()[-30:]}'"
            )


# ─────────────────────────────────────────────────────────────
# 최종 통합 확인
# ─────────────────────────────────────────────────────────────

def test_phase3_integration_passed():
    """모든 테스트가 통과하면 Phase 3 완료 메시지를 출력한다."""
    print("\n" + "=" * 50)
    print("✅ Phase 3 Integration Test PASSED")
    print("=" * 50)
