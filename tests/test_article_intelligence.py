"""
tests/test_article_intelligence.py
Agent 2 — Article Intelligence 기능 테스트 (14개)
"""

import pytest

from core.impact_scorer import score_article_v2, _macro_alignment_check, batch_score_and_rank
from core.summarizer import summarize_executive, generate_comparison_summary


# ── 공통 fixture ──────────────────────────────────────────────

@pytest.fixture
def sample_article():
    return {
        "title": "환율 급등에 수출기업 위기 대응 긴급 회의",
        "body": "원달러 환율이 1,500원을 돌파하며 수출 기업들의 긴급 대응이 요구되고 있다.",
    }


@pytest.fixture
def risk_article():
    return {
        "title": "경기 침체 우려 악화, 수출 부진 지속",
        "body": "하락세가 이어지며 위기감이 고조되고 있다. 감소 추세가 우려된다.",
    }


@pytest.fixture
def opportunity_article():
    return {
        "title": "수출 성장세 회복, 기회 확대 전망",
        "body": "수출 증가율 개선과 함께 회복 흐름이 확산되고 있다. 호조세 지속.",
    }


@pytest.fixture
def neutral_article():
    return {
        "title": "올해 경제 동향 분석 보고서 발간",
        "body": "한국은행이 경제 동향 보고서를 발간했다.",
    }


@pytest.fixture
def macro_negative():
    """부정적 거시 환경 (trend 하락)"""
    return {
        "환율(원/$)": {"value": "1500", "trend": "▲", "prev_value": "1400"},
        "수출증가율": {"value": "-5", "trend": "▼", "prev_value": "3"},
        "기준금리": {"value": "4.0", "trend": "▲", "prev_value": "3.5"},
        "소비자물가(CPI)": {"value": "3.5", "trend": "▲", "prev_value": "2.5"},
    }


@pytest.fixture
def macro_positive():
    """긍정적 거시 환경"""
    return {
        "환율(원/$)": {"value": "1300", "trend": "▼", "prev_value": "1400"},
        "수출증가율": {"value": "10", "trend": "▲", "prev_value": "3"},
        "기준금리": {"value": "2.5", "trend": "▼", "prev_value": "3.0"},
    }


# ── 1. score_article_v2 ──────────────────────────────────────

def test_score_article_v2_basic(sample_article):
    result = score_article_v2(sample_article, "일반")
    required_keys = {"score", "raw_score", "confidence", "factors", "policy_type", "macro_alignment"}
    assert required_keys <= set(result.keys())


def test_score_article_v2_score_range(sample_article):
    result = score_article_v2(sample_article, "일반")
    assert 1 <= result["score"] <= 5


def test_score_article_v2_with_signal(sample_article, macro_negative):
    without_signal = score_article_v2(sample_article, "일반", macro_data=macro_negative)
    with_signal = score_article_v2(sample_article, "일반", macro_data=macro_negative, signal={"direction": "up"})
    assert with_signal["confidence"] > without_signal["confidence"]
    assert with_signal["confidence"] == 0.9
    assert without_signal["confidence"] == 0.8


def test_score_article_v2_confidence_range(sample_article):
    result = score_article_v2(sample_article, "일반")
    assert 0.0 <= result["confidence"] <= 1.0


# ── 2. _macro_alignment_check ────────────────────────────────

def test_macro_alignment_aligned(risk_article, macro_negative):
    """리스크 기사 + 부정 거시 → aligned"""
    result = _macro_alignment_check(risk_article, macro_negative, "일반")
    assert result == "aligned"


def test_macro_alignment_contrary(opportunity_article, macro_negative):
    """기회 기사 + 부정 거시 → contrary"""
    result = _macro_alignment_check(opportunity_article, macro_negative, "일반")
    assert result == "contrary"


def test_macro_alignment_neutral(neutral_article, macro_negative):
    """중립 기사 → neutral"""
    result = _macro_alignment_check(neutral_article, macro_negative, "일반")
    assert result == "neutral"


# ── 3. batch_score_and_rank ──────────────────────────────────

def test_batch_score_and_rank_ordering():
    articles = [
        {"title": "일반 경제 뉴스", "body": "경제 동향 보고서"},
        {"title": "환율 급등 위기 대응 긴급 수출 금리 물가 관세", "body": "긴급 위기 대응 즉시 시행 발효 당장"},
        {"title": "수출 성장 확대 증가", "body": "성장세 지속"},
    ]
    ranked = batch_score_and_rank(articles, "일반")
    scores = [r["score"] for r in ranked]
    assert scores == sorted(scores, reverse=True)


def test_batch_score_and_rank_has_rank():
    articles = [
        {"title": "기사1 환율", "body": "내용1"},
        {"title": "기사2 수출", "body": "내용2"},
    ]
    ranked = batch_score_and_rank(articles, "일반")
    ranks = [r["rank"] for r in ranked]
    assert ranks[0] == 1
    assert ranks[-1] == len(ranked)


def test_batch_score_empty_list():
    result = batch_score_and_rank([], "일반")
    assert result == []


# ── 4. summarize_executive ────────────────────────────────────

def test_summarize_executive_keys():
    result = summarize_executive(
        text="환율 급등으로 수출 기업에 영향이 예상된다. 금리 인상으로 투자 위축 우려.",
        title="환율 급등과 수출 기업 영향",
    )
    required_keys = {"headline", "body", "recommendation", "urgency", "relevance_score"}
    assert required_keys <= set(result.keys())
    assert 0.0 <= result["relevance_score"] <= 1.0
    assert result["urgency"] in ("high", "medium", "low")


def test_summarize_executive_urgency():
    result = summarize_executive(
        text="긴급 위기 대응 조치 즉시 시행. 발효 즉시 적용. 당장 비상 조치 필요.",
        title="긴급 위기 대응",
    )
    assert result["urgency"] == "high"


# ── 5. generate_comparison_summary ────────────────────────────

def test_generate_comparison_summary_nonempty():
    articles = [
        {"title": "환율 상승과 수출 증가", "body": "원달러 환율이 상승하며 수출이 증가했다."},
        {"title": "금리 인하와 투자 확대", "body": "기준금리 인하로 투자가 확대되고 있다."},
    ]
    result = generate_comparison_summary(articles, "일반")
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_comparison_summary_single():
    articles = [
        {"title": "단일 기사 테스트", "body": "수출 관련 경제 뉴스 내용."},
    ]
    result = generate_comparison_summary(articles, "일반")
    assert isinstance(result, str)
    # 에러 없이 정상 반환되면 통과
