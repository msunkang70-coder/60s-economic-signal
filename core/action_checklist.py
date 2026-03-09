"""
core/action_checklist.py
전략 질문별 실행 체크리스트 2~3개 생성.

구현 방식:
  - 질문 내 키워드로 카테고리 분류 (규제/수요/비용/시장)
  - 산업별 × 카테고리별 체크리스트 템플릿
  - critical_variables & 기사 키워드 교차 분석 → {kw} 치환
"""

from core.industry_config import get_profile

# ── 카테고리 분류 키워드 ──────────────────────────────────────
_CATEGORY_KW = {
    "규제": ["규제", "제재", "통제", "관세", "금지", "제한", "법안", "수출통제", "CBAM", "IRA"],
    "수요": ["수요", "시장", "고객", "판매", "소비", "수주", "바이어", "경기"],
    "비용": ["비용", "원가", "원자재", "물류", "조달", "가격", "마진", "단가", "유가", "나프타"],
    "시장": ["시장", "진출", "수출", "경쟁", "점유", "기회", "확대", "다변화", "전략"],
}

# ── 산업별 × 카테고리별 체크리스트 템플릿 ──────────────────────
_TEMPLATES = {
    "반도체": {
        "규제": [
            "{kw} 규제 대상 품목에 자사 제품 포함 여부 확인",
            "대체 수출 경로·시장 확보 상태 점검",
            "규제 시행 일정과 유예 기간 확인",
        ],
        "수요": [
            "{kw} 시장 주요 고객 수요 동향 파악",
            "경쟁사 공급 능력 변화 모니터링",
            "자사 제품 가격 경쟁력 포지션 재검토",
        ],
        "비용": [
            "{kw} 관련 원자재·소재 조달 비용 변동 확인",
            "생산 단가 변동 시 마진 영향 시뮬레이션",
            "대체 조달처 확보 여부 점검",
        ],
        "시장": [
            "{kw} 관련 새 시장 진입 가능성 평가",
            "기존 주력 시장 점유율 변동 추이 확인",
            "경쟁사 시장 전략 변화 모니터링",
        ],
    },
    "자동차": {
        "규제": [
            "{kw} 관련 수출 차량·부품 인증 요건 변동 확인",
            "관세·비관세 장벽 변화에 따른 수출 원가 재산정",
            "규제 유예 기간 및 대응 일정 확인",
        ],
        "수요": [
            "{kw} 시장 완성차·부품 수요 변동 추이 확인",
            "전기차·내연기관 수요 비율 변화 모니터링",
            "주요 OEM 납품 물량 계획 재확인",
        ],
        "비용": [
            "{kw} 관련 철강·알루미늄 조달 비용 변동 확인",
            "부품 납품 단가 재협상 필요 여부 검토",
            "물류·운송비 변동에 따른 수출 원가 점검",
        ],
        "시장": [
            "{kw} 시장 신규 진입·확대 가능성 평가",
            "미국·EU·동남아 시장별 판매 전략 재검토",
            "경쟁사 시장 점유 변화 모니터링",
        ],
    },
    "화학": {
        "규제": [
            "{kw} 관련 화학물질 수출 규제 대상 여부 확인",
            "탄소국경조정(CBAM) 인증 준비 상태 점검",
            "규제 시행 일정과 기업 대응 계획 수립 여부 확인",
        ],
        "수요": [
            "{kw} 관련 석유화학 제품 수요 동향 확인",
            "중국·동남아 시장 재고 수준 모니터링",
            "주력 제품 수주 잔량 및 납기 일정 확인",
        ],
        "비용": [
            "{kw} 관련 원유·나프타 가격 연동 원가 재산정",
            "에너지 비용 변동에 따른 생산 단가 점검",
            "대체 원료 조달 가능성 및 비용 비교",
        ],
        "시장": [
            "{kw} 관련 신규 시장·용도 개발 가능성 평가",
            "주력 수출 시장 점유율 변동 추이 확인",
            "경쟁국 증설·감산 동향 모니터링",
        ],
    },
    "소비재": {
        "규제": [
            "{kw} 관련 수출 대상국 제품 인증·라벨링 규제 확인",
            "식품·화장품 안전 규정 변경 사항 점검",
            "규제 시행 일정과 대응 준비 상태 확인",
        ],
        "수요": [
            "{kw} 관련 글로벌 소비 트렌드 변화 파악",
            "주요 수출국 소비자 구매력 변동 모니터링",
            "온라인·오프라인 채널별 판매 동향 확인",
        ],
        "비용": [
            "{kw} 관련 포장재·원료 조달 비용 변동 확인",
            "물류·해운 운임 변동에 따른 수출 원가 점검",
            "판매 가격 조정 타이밍 및 마진 영향 시뮬레이션",
        ],
        "시장": [
            "{kw} 관련 신규 수출 시장 진입 가능성 평가",
            "K-뷰티·K-푸드 트렌드 활용 전략 재검토",
            "경쟁 브랜드 시장 전략 변화 모니터링",
        ],
    },
    "일반": {
        "규제": [
            "{kw} 관련 수출 규제 대상 여부 확인",
            "대체 수출 경로·시장 확보 상태 점검",
            "규제 시행 일정과 대응 계획 수립",
        ],
        "수요": [
            "{kw} 관련 주요 수출 시장 수요 동향 파악",
            "경쟁사 대비 가격 경쟁력 재검토",
            "신규 바이어 발굴 및 수주 파이프라인 점검",
        ],
        "비용": [
            "{kw} 관련 원자재·부품 조달 비용 변동 확인",
            "생산 단가 변동 시 마진 영향 점검",
            "대체 조달처 확보 여부 확인",
        ],
        "시장": [
            "{kw} 관련 새 시장 진입 가능성 평가",
            "기존 주력 시장 점유율 변동 추이 확인",
            "경쟁사 시장 전략 변화 모니터링",
        ],
    },
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
