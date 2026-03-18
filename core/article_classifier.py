"""
core/article_classifier.py
기사 타입 분류 — macro / industry / company / general (V17.9)

파이프라인 위치:
  기사 수집 → **1차: 기사 타입 분류** → 2차: 산업 관련성 판정 → 3차: scoring → 노출

설계 원칙:
  - 산업 매칭 전에 기사 타입을 먼저 분류
  - macro 기사는 특정 산업 카드에 직접 배치되지 않음
  - industry_key="일반"일 때 macro 기사는 허용 (backward compatible)
  - keyword-based 분류, LLM 불필요 (성능: <1ms/article)
"""

from __future__ import annotations

import re
from core.industry_config import get_profile, INDUSTRY_PROFILES


# ══════════════════════════════════════════════════════════════
# 매크로 신호 키워드 (3단계)
# ══════════════════════════════════════════════════════════════

# 강한 매크로 구문 (+5.0 each) — 국가 전체 통계/거시 브리핑 징후
_MACRO_STRONG_PHRASES: list[str] = [
    "수출입 동향", "무역수지", "총수출", "총수입",
    "전년동월대비", "전년 동월 대비", "전년대비",
    "경제지표", "전체 품목", "전체 지역", "월간 수출입",
    "경제 브리핑", "경기동향", "경제전망", "경제성장률",
    "GDP", "국내총생산", "경상수지",
    "수출 실적", "수입 실적", "교역 실적",
    "대외경제", "경제동향",
]

# 매크로 제목 패턴 (+6.0 each, compiled regex)
_MACRO_TITLE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\d{4}년\s*\d{1,2}월\s*수출입\s*동향"),
    re.compile(r"\d{1,2}월\s*수출입\s*동향"),
    re.compile(r"\d{4}년.*경제\s*전망"),
    re.compile(r"\d{4}년.*경제\s*동향"),
    re.compile(r"\d{1,2}월\s*수출\s*실적"),
    re.compile(r"^\s*\d{4}년\s*\d{1,2}월\s*(수출|무역|경제)"),
]

# 보통 매크로 키워드 (+2.0 each) — 거시지표 관련
_MACRO_MODERATE_KW: list[str] = [
    "산업생산지수", "고용동향", "실업률",
    "소비자물가지수", "생산자물가지수",
    "수출물가지수", "수입물가지수",
    "국제수지", "외환보유액", "기준금리 결정",
    "통화정책", "금융통화위원회",
    "전산업생산", "경기종합지수",
]

# 매크로 본문 약신호 (+1.5 each) — 단독으로는 약하지만 누적 시 매크로 징후
_MACRO_BODY_WEAK_KW: list[str] = [
    "전체 수출", "전체 수입", "품목별", "지역별",
    "10대 품목", "15대 품목", "주요국별",
    "수출 증가율", "수입 증가율", "무역규모",
    "흑자", "적자", "교역규모",
]


# ══════════════════════════════════════════════════════════════
# 기업 → 산업 매핑
# ══════════════════════════════════════════════════════════════
_COMPANY_TO_INDUSTRY: dict[str, str] = {
    # 반도체
    "삼성전자": "반도체", "SK하이닉스": "반도체", "DB하이텍": "반도체",
    "한미반도체": "반도체", "주성엔지니어링": "반도체",
    # 자동차
    "현대차": "자동차", "현대자동차": "자동차", "기아": "자동차",
    "현대모비스": "자동차", "만도": "자동차", "HL만도": "자동차",
    # 소비재
    "아모레퍼시픽": "소비재", "LG생활건강": "소비재",
    "농심": "소비재", "삼양식품": "소비재", "CJ제일제당": "소비재",
    "오뚜기": "소비재", "풀무원": "소비재", "코웨이": "소비재",
    "코스알엑스": "소비재", "클리오": "소비재",
    # 배터리
    "LG에너지솔루션": "배터리", "삼성SDI": "배터리", "SK온": "배터리",
    "에코프로비엠": "배터리", "에코프로": "배터리", "포스코퓨처엠": "배터리",
    # 조선
    "HD현대중공업": "조선", "삼성중공업": "조선", "한화오션": "조선",
    # 철강
    "포스코": "철강", "현대제철": "철강", "고려아연": "철강",
    # 화학
    "LG화학": "화학", "롯데케미칼": "화학", "한화솔루션": "화학",
    "금호석유": "화학", "솔브레인": "화학",
}


# ══════════════════════════════════════════════════════════════
# 산업 키워드 (industry_config.py에서 동적 로딩)
# ══════════════════════════════════════════════════════════════
def _get_all_industry_keywords() -> dict[str, list[str]]:
    """전 산업 키워드를 {industry_key: [keywords]} 형태로 반환."""
    result: dict[str, list[str]] = {}
    for key in INDUSTRY_PROFILES:
        if key == "일반":
            continue  # 일반은 산업 신호 판정에서 제외
        profile = get_profile(key)
        result[key] = profile.get("keywords", [])
    return result


# ══════════════════════════════════════════════════════════════
# 분류 함수
# ══════════════════════════════════════════════════════════════
def classify_article_type(
    title: str,
    body_text: str = "",
    industry_key: str = "",
) -> dict:
    """기사 타입 분류 — macro / industry / company / general.

    Parameters:
        title: 기사 제목
        body_text: 기사 본문 (없으면 빈 문자열)
        industry_key: 현재 선택된 산업 키 (참고용)

    Returns:
        {
            "article_type": "macro" | "industry" | "company" | "general",
            "macro_confidence": float (0.0-1.0),
            "article_type_scores": {"macro": float, "industry": float, "company": float},
            "matched_signals": list[str],
        }
    """
    text = f"{title} {body_text[:2000]}" if body_text else title
    matched: list[str] = []

    # ── 1. 매크로 스코어 ──────────────────────────────────────
    macro_score = 0.0

    # 1a. 강한 매크로 구문 (제목 + 본문)
    for phrase in _MACRO_STRONG_PHRASES:
        if phrase in text:
            macro_score += 5.0
            matched.append(f"macro_strong:{phrase}")

    # 1b. 매크로 제목 패턴 (제목만)
    for pat in _MACRO_TITLE_PATTERNS:
        if pat.search(title):
            macro_score += 6.0
            matched.append(f"macro_pattern:{pat.pattern[:30]}")

    # 1c. 보통 매크로 키워드
    for kw in _MACRO_MODERATE_KW:
        if kw in text:
            macro_score += 2.0
            matched.append(f"macro_mod:{kw}")

    # 1d. 본문 약신호 (본문이 있을 때만)
    if body_text:
        _weak_count = 0
        for kw in _MACRO_BODY_WEAK_KW:
            if kw in body_text[:3000]:
                _weak_count += 1
        if _weak_count >= 2:
            macro_score += min(6.0, _weak_count * 1.5)
            matched.append(f"macro_body_weak:{_weak_count}kw")

    # 1e. 다산업 언급 (본문에 3개 이상 서로 다른 산업 키워드)
    if body_text:
        _all_ind_kws = _get_all_industry_keywords()
        _mentioned_industries = set()
        for ind_key, kws in _all_ind_kws.items():
            for kw in kws:
                if kw in body_text[:3000]:
                    _mentioned_industries.add(ind_key)
                    break
        if len(_mentioned_industries) >= 3:
            _multi_boost = min(6.0, len(_mentioned_industries) * 2.0)
            macro_score += _multi_boost
            matched.append(f"macro_multi_ind:{len(_mentioned_industries)}개")

    # ── 2. 산업 스코어 ──────────────────────────────────────
    industry_score = 0.0
    _all_ind_kws = _get_all_industry_keywords()

    for ind_key, kws in _all_ind_kws.items():
        for kw in kws:
            if kw in title:  # 제목에서 산업 키워드 매칭 (더 강한 신호)
                industry_score += 3.0
                matched.append(f"industry:{ind_key}/{kw}")

    # 본문에서도 산업 키워드 (약한 가중치)
    if body_text:
        for ind_key, kws in _all_ind_kws.items():
            for kw in kws:
                if kw in body_text[:2000] and kw not in title:
                    industry_score += 0.5
                    # matched에는 추가 안함 (너무 많아짐)

    industry_score = min(15.0, industry_score)

    # ── 3. 기업 스코어 ──────────────────────────────────────
    company_score = 0.0
    _matched_company: str | None = None
    _mapped_industry: str | None = None

    for company, ind in _COMPANY_TO_INDUSTRY.items():
        if company in title:
            company_score += 5.0
            _matched_company = company
            _mapped_industry = ind
            matched.append(f"company:{company}→{ind}")
            break  # 제목에 첫 번째 매칭만

    if not _matched_company and body_text:
        for company, ind in _COMPANY_TO_INDUSTRY.items():
            if company in body_text[:1000]:
                company_score += 2.0
                _matched_company = company
                _mapped_industry = ind
                matched.append(f"company_body:{company}→{ind}")
                break

    # ── 4. 판정 ──────────────────────────────────────────────
    article_type = _decide_type(macro_score, industry_score, company_score)
    macro_confidence = min(1.0, macro_score / 15.0)

    return {
        "article_type": article_type,
        "macro_confidence": macro_confidence,
        "article_type_scores": {
            "macro": round(macro_score, 1),
            "industry": round(industry_score, 1),
            "company": round(company_score, 1),
        },
        "matched_signals": matched[:10],  # 디버그용, 최대 10개
    }


def _decide_type(
    macro_score: float,
    industry_score: float,
    company_score: float,
) -> str:
    """스코어 기반 기사 타입 결정.

    우선순위:
    1. 기업명이 명확하고 매크로보다 강하면 → company
    2. 매크로가 산업의 1.5배 이상이면 → macro
    3. 산업 신호가 3.0 이상이면 → industry
    4. 매크로 신호가 있으면 → macro
    5. 나머지 → general
    """
    # 기업명이 제목에 있고 매크로보다 강한 경우
    if company_score >= 5.0 and company_score > macro_score:
        return "company"

    # 매크로가 산업의 1.5배 이상으로 압도
    if macro_score > 0 and macro_score > industry_score * 1.5:
        return "macro"

    # 산업 신호가 충분히 강한 경우
    if industry_score >= 3.0:
        return "industry"

    # 약한 매크로라도 산업보다 강하면
    if macro_score > 0 and macro_score > industry_score:
        return "macro"

    # 분류 불가
    return "general"
