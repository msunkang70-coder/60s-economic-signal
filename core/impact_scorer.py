"""
core/impact_scorer.py
기사별 산업 가중 임팩트 스코어 (1~5).

산출 구조 (총 100점):
  A. 키워드 매칭 (0~30): keywords × 3 + critical_variables × 5
  B. 거시지표 연동 (0~30): 임계값 초과 지표 수 × macro_weights
  C. 정책 유형 (0~20): 위기대응=20, 규제=15, 구조개편=12, 지원=8, 미분류=5
  D. 시급성 (0~20): 키워드당 +4 (상한 20)
"""

import copy

from core.industry_config import get_profile

# ── 정책 유형 키워드 ────────────────────────────────────────
_POLICY_KW = {
    "위기대응": (["위기", "대응", "긴급", "안정화", "방어", "보호", "충격"], 20),
    "규제":     (["규제", "제한", "금지", "강화", "단속", "처벌", "통제"], 15),
    "구조개편": (["개편", "구조", "혁신", "개혁", "전환", "재편"], 12),
    "지원":     (["지원", "보조", "혜택", "육성", "지원금", "보조금"], 8),
}
_POLICY_DEFAULT = 5

# ── 시급성 키워드 (키워드당 +4, 상한 20) ─────────────────────
_URGENCY_KW = [
    "즉시", "긴급", "시행", "발효", "폐지",
    "단기", "올해", "분기", "당장", "확대", "강화", "변경",
]

# ── 거시지표 normal 범위 (벗어나면 가산점) ──────────────────
_MACRO_THRESHOLDS = {
    "환율(원/$)":      (1380, 1500),
    "소비자물가(CPI)": (0, 2.5),
    "수출증가율":      (-5, 15),
    "기준금리":        (2.0, 3.5),
    "수입물가지수":    (-3, 5),
    "수출물가지수":    (-3, 5),
    "원/100엔 환율":   (850, 1050),
}


def _keyword_score(text: str, industry_key: str) -> float:
    """A. 산업 keywords × 3 + critical_variables × 5 (상한 30)."""
    profile = get_profile(industry_key)
    keywords = profile.get("keywords", [])
    crit_vars = profile.get("critical_variables", [])

    score = 0.0
    for kw in keywords:
        if kw in text:
            score += 3.0
    for cv in crit_vars:
        if cv in text:
            score += 5.0
    return min(30.0, score)


def _macro_score(text: str, macro_data: dict | None, industry_key: str) -> float:
    """B. 기사 내 거시지표 키워드가 언급되고 + 해당 지표가 임계값 초과일 때만 가산."""
    if not macro_data:
        return 0.0

    profile = get_profile(industry_key)
    weights = profile.get("macro_weights", {})

    _MACRO_KW_MAP = {
        "환율(원/$)": ["환율", "원달러", "달러", "원화"],
        "소비자물가(CPI)": ["물가", "CPI", "인플레이션"],
        "수출증가율": ["수출", "수출액", "수출 증가"],
        "기준금리": ["금리", "기준금리", "한은"],
        "수입물가지수": ["수입물가", "수입 원가"],
        "수출물가지수": ["수출물가", "수출 단가"],
        "원/100엔 환율": ["엔화", "엔환율", "100엔"],
    }

    score = 0.0
    for indicator, weight in weights.items():
        kw_list = _MACRO_KW_MAP.get(indicator, [])
        if not any(kw in text for kw in kw_list):
            continue
        data = macro_data.get(indicator)
        if not data or not isinstance(data, dict):
            continue
        try:
            val = float(str(data.get("value", "0")).replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            continue
        lo, hi = _MACRO_THRESHOLDS.get(indicator, (None, None))
        if lo is not None and hi is not None:
            if val < lo or val > hi:
                score += 5.0 * weight
    return min(30.0, score)


def _policy_score(text: str) -> float:
    """C. 정책 유형 점수 (0~20)."""
    best = 0.0
    for _, (kws, pts) in _POLICY_KW.items():
        if any(kw in text for kw in kws):
            best = max(best, pts)
    if best == 0.0:
        best = _POLICY_DEFAULT
    return min(20.0, best)


def _urgency_score(text: str) -> float:
    """D. 시급성 키워드당 +4 (상한 20)."""
    score = 0.0
    for kw in _URGENCY_KW:
        if kw in text:
            score += 4.0
    return min(20.0, score)


def _score_to_stars(total: float) -> int:
    """100점 만점 → 1~5 매핑."""
    if total >= 70:
        return 5
    if total >= 50:
        return 4
    if total >= 30:
        return 3
    if total >= 15:
        return 2
    return 1


def score_article(
    article: dict,
    industry_key: str,
    macro_data: dict | None = None,
) -> int:
    """기사 1건의 산업별 임팩트 점수 (1~5).

    Parameters:
        article: {"title": str, "date": str, ...} — body는 있을 수도 없을 수도
        industry_key: 산업 키
        macro_data: app.py의 _MACRO (선택, 없으면 거시 연동 점수 0)

    Returns:
        int: 1~5
    """
    text = article.get("title", "")
    text += " " + article.get("body", "")
    text += " " + article.get("body_text", "")
    text += " " + article.get("summary_3lines", "")

    a = _keyword_score(text, industry_key)
    b = _macro_score(text, macro_data, industry_key)
    c = _policy_score(text)
    d = _urgency_score(text)

    return _score_to_stars(a + b + c + d)


def score_articles(
    articles: list[dict],
    industry_key: str,
    macro_data: dict | None = None,
) -> list[dict]:
    """기사 리스트에 'impact_score' 키를 추가하여 반환 (원본 수정 X, 복사).

    점수 내림차순 정렬.
    """
    scored = []
    for art in articles:
        art_copy = copy.copy(art)
        art_copy["impact_score"] = score_article(art, industry_key, macro_data)
        scored.append(art_copy)
    scored.sort(key=lambda x: -x["impact_score"])
    return scored


# ── 하위 호환 ──────────────────────────────────────────────
def calculate_impact_score(
    article: dict,
    macro_data: dict,
    industry_key: str = "일반",
) -> int:
    """기존 호출 호환용 래퍼."""
    return score_article(article, industry_key, macro_data)
