"""
core/action_checklist.py
전략 질문별 실행 체크리스트 2~3개 생성.

구현 방식:
  - 질문 내 키워드로 카테고리 분류 (규제/수요/비용/시장)
  - 산업별 × 카테고리별 체크리스트 템플릿
  - critical_variables & 기사 키워드 교차 분석 → {kw} 치환
"""

from core.industry_config import get_profile
from core.checklist_rules import ACTION_CHECKLIST_TEMPLATES as _TEMPLATES

# ── 카테고리 분류 키워드 ──────────────────────────────────────
_CATEGORY_KW = {
    "규제": ["규제", "제재", "통제", "관세", "금지", "제한", "법안", "수출통제", "CBAM", "IRA"],
    "수요": ["수요", "시장", "고객", "판매", "소비", "수주", "바이어", "경기"],
    "비용": ["비용", "원가", "원자재", "물류", "조달", "가격", "마진", "단가", "유가", "나프타"],
    "시장": ["시장", "진출", "수출", "경쟁", "점유", "기회", "확대", "다변화", "전략"],
}


def _classify_category(question: str) -> str:
    """질문 키워드 기반 카테고리 분류."""
    best_cat = "시장"
    best_count = 0
    for cat, kws in _CATEGORY_KW.items():
        count = sum(1 for kw in kws if kw in question)
        if count > best_count:
            best_count = count
            best_cat = cat
    return best_cat


def _extract_kw(question: str, article: dict, industry_key: str) -> str:
    """질문·기사·산업 프로필에서 가장 관련 높은 키워드 추출."""
    profile = get_profile(industry_key)
    crit_vars = profile.get("critical_variables", [])
    keywords = profile.get("keywords", [])

    text = question + " " + article.get("title", "")

    # critical_variables 우선 매칭
    for cv in crit_vars:
        clean = cv.replace("(", "").replace(")", "")
        if clean in text:
            return cv

    # 산업 keywords 매칭
    for kw in keywords:
        if kw in text:
            return kw

    # 질문에서 주요 명사 추출 (간이)
    for cat_kws in _CATEGORY_KW.values():
        for kw in cat_kws:
            if kw in question:
                return kw

    return profile.get("label", "수출")


def generate_checklist(
    question: str,
    article: dict,
    industry_key: str = "일반",
) -> list[str]:
    """전략 질문 1개에 대해 실행 가능한 체크리스트 2~3개 반환.

    Parameters:
        question: 전략 질문 텍스트
        article: 관련 기사 dict
        industry_key: 산업 키

    Returns:
        ["확인 항목 1", "확인 항목 2", "확인 항목 3"]
    """
    category = _classify_category(question)
    kw = _extract_kw(question, article, industry_key)

    ind_templates = _TEMPLATES.get(industry_key, _TEMPLATES["일반"])
    templates = ind_templates.get(category, ind_templates["시장"])

    return [t.format(kw=kw) for t in templates[:3]]


# 별칭 (alias)
generate_action_checklist = generate_checklist
