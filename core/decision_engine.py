"""
core/decision_engine.py
수출기업 CEO/전략담당 대상 — 전략 옵션 3가지 자동 생성 엔진.
"""

from core.industry_config import get_profile

# ── 임계값 (today_signal.py와 동일) ──────────────────────
_THRESHOLDS = {
    "환율(원/$)": [
        (0,    1380, "normal"),
        (1380, 1450, "caution"),
        (1450, 1500, "warning"),
        (1500, 9999, "danger"),
    ],
    "소비자물가(CPI)": [
        (0,   2.0, "normal"),
        (2.0, 3.0, "caution"),
        (3.0, 9999, "danger"),
    ],
    "수출증가율": [
        (-9999, -10, "danger"),
        (-10,     0, "caution"),
        (0,      15, "normal"),
        (15,   9999, "caution"),
    ],
    "기준금리": [
        (0,   2.0, "caution"),
        (2.0, 3.5, "normal"),
        (3.5, 9999, "warning"),
    ],
}


def _get_status(label: str, value: float) -> str:
    for lo, hi, status in _THRESHOLDS.get(label, []):
        if lo <= value < hi:
            return status
    return "normal"


# ══════════════════════════════════════════════════════
# 전략 옵션 템플릿: {산업key: {상태: [옵션 3개]}}
# ══════════════════════════════════════════════════════
DECISION_TEMPLATES: dict[str, dict[str, list[dict]]] = {
    # ── 반도체 ────────────────────────────────────────
    "반도체": {
        "danger": [
            {"title": "환헤지 비중 긴급 확대", "rationale": "환율 급등 구간 — 수입 장비·소재 비용 급증 대비", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
            {"title": "수출 선물환 계약 체결", "rationale": "추가 환율 상승 리스크에 대비한 선물환 매도", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            {"title": "주요 고객사 납품 단가 재협상", "rationale": "원가 급등분 반영한 단가 조정 요청", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
        ],
        "warning": [
            {"title": "환리스크 모니터링 강화", "rationale": "경고 구간 진입 — 일별 환율 추적 및 헤지 타이밍 결정", "urgency": "즉시", "difficulty": "낮음", "impact": "중간"},
            {"title": "반도체 소재 선구매 검토", "rationale": "환율 추가 상승 시 수입 비용 증가 대비 선제 발주", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
            {"title": "AI 반도체 수출 확대 가속", "rationale": "환율 상승기 수출 채산성 개선 기회 활용", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
        "caution": [
            {"title": "환율 동향 주간 모니터링", "rationale": "주의 구간 — 주 1회 환율 리뷰 체계 점검", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "수출 물량 확대 계획 수립", "rationale": "현 환율 수준에서 수출 경쟁력 분석 후 물량 조정", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "R&D 투자 우선순위 재검토", "rationale": "비용 상승 추세 감안한 투자 효율성 재평가", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
        ],
        "normal": [
            {"title": "기존 환헤지 비중 유지", "rationale": "환율 안정 구간 — 현행 헤지 전략 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            {"title": "신규 수출 시장 탐색", "rationale": "안정기 활용한 신시장 개척 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "설비 투자·증설 타이밍 검토", "rationale": "안정적 환경에서 중장기 투자 계획 구체화", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
    },
    # ── 자동차 ────────────────────────────────────────
    "자동차": {
        "danger": [
            {"title": "수출 차량 가격 긴급 재산정", "rationale": "환율 급등으로 달러 기준 가격경쟁력 변동", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            {"title": "철강·부품 수입 비용 긴급 점검", "rationale": "수입 원자재 원가 급등 — 부품사 납품가 재협상", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
            {"title": "환헤지 포지션 확대", "rationale": "달러 결제 수출 계약 환율 리스크 긴급 대비", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
        ],
        "warning": [
            {"title": "해외 딜러 가격 정책 사전 조정", "rationale": "환율 경고 — 미국·EU 시장 판매가 사전 재산정", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
            {"title": "부품 공급망 원가 모니터링", "rationale": "환율 상승기 부품사 납품가 인상 요청 대비", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "전기차 수출 보조금 활용 검토", "rationale": "비용 상승분을 수출 지원금으로 상쇄 가능 여부 확인", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
        ],
        "caution": [
            {"title": "주간 환율 리뷰 체계 가동", "rationale": "주의 구간 — 주 1회 환율·철강가 동시 모니터링", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "미국 관세 정책 영향 시뮬레이션", "rationale": "환율 변동 + 관세 시나리오 조합 분석", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            {"title": "할부 금융 조건 재검토", "rationale": "금리·환율 연동 할부 판매 조건 최적화", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
        ],
        "normal": [
            {"title": "수출 물량 계획 정상 유지", "rationale": "환율 안정 — 기존 생산·수출 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            {"title": "신차 해외 론칭 일정 확정", "rationale": "안정적 환경에서 신차 수출 일정 구체화", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "중장기 EV 수출 전략 수립", "rationale": "안정기 활용한 전기차 해외 진출 로드맵 수립", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
    },
    # ── 화학 ──────────────────────────────────────────
    "화학": {
        "danger": [
            {"title": "나프타·원유 긴급 선구매", "rationale": "환율 급등 + 유가 연동 — 수입 원료 비용 급증 대비", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            {"title": "수출 제품 단가 긴급 인상 통보", "rationale": "원가 급등분 즉시 반영 — 마진 방어 필수", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
            {"title": "환헤지 비중 80% 이상 확대", "rationale": "환율 위험 구간 — 최대 헤지로 리스크 차단", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
        ],
        "warning": [
            {"title": "원유·나프타 가격 일별 추적 강화", "rationale": "환율 경고 — 원료 가격 상승 압력 사전 감지", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "중국·동남아 수출 단가 조정", "rationale": "경쟁 시장 원가 전가 가능 범위 사전 산출", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
            {"title": "탄소국경조정(CBAM) 대응 점검", "rationale": "비용 상승기 EU 탄소비용 추가 부담 시뮬레이션", "urgency": "이번 달", "difficulty": "높음", "impact": "중간"},
        ],
        "caution": [
            {"title": "주간 원가 모니터링 리뷰", "rationale": "주의 구간 — 나프타·환율 주간 리뷰 체계 점검", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "운전자본 효율화 점검", "rationale": "원가 상승 추세 대비 재고·매출채권 회전율 개선", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "고부가 특수화학 수출 비중 확대", "rationale": "원가 변동 민감도 낮은 고마진 제품 비중 확대", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
        "normal": [
            {"title": "기존 조달·헤지 전략 유지", "rationale": "안정 구간 — 현행 원료 조달 및 환헤지 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            {"title": "설비 증설 투자 타이밍 검토", "rationale": "원가 안정기 활용한 증설 투자 의사결정", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            {"title": "신규 수출 시장 진출 검토", "rationale": "안정적 원가 환경에서 신시장 개척 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
        ],
    },
    # ── 소비재 ────────────────────────────────────────
    "소비재": {
        "danger": [
            {"title": "수입 원료·포장재 대체 소싱 긴급 검토", "rationale": "환율 급등 — 수입 비용 급증 대비 국산 대체 탐색", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            {"title": "수출 판매가 긴급 조정", "rationale": "원가 급등분 해외 판매가에 즉시 반영", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
            {"title": "물류비 절감 방안 긴급 실행", "rationale": "해운운임·환율 동시 상승 — 물류 최적화 긴급 추진", "urgency": "즉시", "difficulty": "중간", "impact": "중간"},
        ],
        "warning": [
            {"title": "해외 채널별 가격 정책 재검토", "rationale": "환율 경고 — 시장별 판매 단가 사전 조정 준비", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
            {"title": "소비자 가격 민감도 분석", "rationale": "물가 상승기 소비자 반응 예측 및 가격 전략 수립", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "프로모션 전략 재설계", "rationale": "원가 상승분을 프로모션 구조 변경으로 부분 상쇄", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
        ],
        "caution": [
            {"title": "월간 원가·환율 리뷰 체계화", "rationale": "주의 구간 — 원료·포장재 비용 월별 추적 강화", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "글로벌 소비 트렌드 분석", "rationale": "소비 경기 둔화 신호 시 제품 포트폴리오 조정 준비", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "온라인 D2C 채널 확대", "rationale": "유통 비용 절감 + 마진 개선을 위한 직접 판매 확대", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
        "normal": [
            {"title": "기존 가격·유통 전략 유지", "rationale": "안정 구간 — 현행 전략 유지하며 시장 모니터링", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            {"title": "신규 해외 유통 채널 개척", "rationale": "안정기 활용한 신규 시장 진출 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "PB·고부가 제품 라인 확대", "rationale": "원가 안정기 활용한 프리미엄 제품 개발", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
    },
    # ── 배터리 ────────────────────────────────────────
    "배터리": {
        "danger": [
            {"title": "리튬·원료 긴급 선계약", "rationale": "환율 급등 — 수입 배터리 소재 비용 급증 대비 선제 확보", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            {"title": "고객사 납품 단가 긴급 재협상", "rationale": "원자재 + 환율 급등분 반영한 단가 조정 필수", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
            {"title": "환헤지 포지션 최대 확대", "rationale": "환율 위험 구간 — 달러 매출·원료 수입 양면 헤지", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
        ],
        "warning": [
            {"title": "리튬 가격 일별 모니터링", "rationale": "환율 경고 + 리튬 가격 연동 — 원가 상승 사전 감지", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "미국 IRA 보조금 활용 극대화", "rationale": "비용 상승분을 IRA 세액공제로 상쇄 전략", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
            {"title": "전기차 OEM 수주 조건 재검토", "rationale": "원가 변동분 반영한 장기 공급 계약 조건 조정", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
        "caution": [
            {"title": "주간 원자재·환율 리뷰", "rationale": "주의 구간 — 리튬·니켈·환율 주간 추적 체계 점검", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "차세대 배터리 R&D 투자 검토", "rationale": "비용 변동 대비 기술 경쟁력 확보 투자 우선순위 설정", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            {"title": "유럽 시장 진출 가속화", "rationale": "EU 배터리 규제 대응 + 현지 생산 비용 절감 검토", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
        "normal": [
            {"title": "기존 원료 조달 계획 유지", "rationale": "안정 구간 — 현행 소싱·헤지 전략 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            {"title": "신규 OEM 수주 확대", "rationale": "안정적 원가 환경에서 신규 고객 확보 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "생산라인 증설 투자 검토", "rationale": "안정기 활용한 설비 확장 의사결정", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
    },
    # ── 조선 ──────────────────────────────────────────
    "조선": {
        "danger": [
            {"title": "수주 계약 환율 조건 긴급 재검토", "rationale": "환율 급등 — 달러 수주 선가 환산 이익 변동 대응", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            {"title": "철강 조달 비용 긴급 점검", "rationale": "환율 + 철강가 동시 상승 — 후판 조달 단가 재협상", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
            {"title": "환헤지 포지션 긴급 확대", "rationale": "달러 수주 장기 계약 환율 리스크 최대 차단", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
        ],
        "warning": [
            {"title": "신규 수주 선가 재산정", "rationale": "환율 경고 — 원가 상승분 반영한 선가 조정", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
            {"title": "해운 운임 동향 분석", "rationale": "운임 상승 시 선박 수주 증가 가능성 — 생산 계획 조정", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "LNG선·특수선 수주 집중", "rationale": "고부가 선종 수주로 원가 상승분 상쇄", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
        "caution": [
            {"title": "주간 환율·철강가 모니터링", "rationale": "주의 구간 — 환율·후판가 주간 추적 체계 점검", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "기존 수주 잔고 환율 리스크 분석", "rationale": "수주 잔고 내 환율 손익 시뮬레이션 실행", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "친환경 선박 기술 투자 검토", "rationale": "IMO 규제 대응 + 수주 경쟁력 확보 투자", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
        "normal": [
            {"title": "기존 수주·생산 계획 유지", "rationale": "안정 구간 — 현행 건조 일정 및 수주 전략 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            {"title": "신규 선종 수주 타깃 확대", "rationale": "안정기 활용한 신규 선종 영업 강화", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "스마트 조선소 투자 추진", "rationale": "안정적 환경에서 디지털 조선 역량 강화 투자", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
    },
    # ── 철강 ──────────────────────────────────────────
    "철강": {
        "danger": [
            {"title": "철광석 긴급 선구매", "rationale": "환율 급등 — 수입 철광석 비용 급증 대비 선제 확보", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            {"title": "제품 출하가 긴급 인상", "rationale": "원가 급등분 즉시 반영 — 마진 방어 필수", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
            {"title": "환헤지 비중 최대 확대", "rationale": "수입 원료 + 수출 제품 양면 환율 리스크 차단", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
        ],
        "warning": [
            {"title": "철광석·석탄 가격 일별 추적", "rationale": "환율 경고 — 원료 가격 상승 압력 사전 감지", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "중국 철강 수출 동향 긴급 분석", "rationale": "중국산 저가 철강 유입 리스크 사전 대응", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
            {"title": "고부가 특수강 비중 확대", "rationale": "일반강 마진 압박 대비 고부가 제품 전환", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
        "caution": [
            {"title": "주간 원료·환율 리뷰", "rationale": "주의 구간 — 철광석·석탄·환율 주간 추적 강화", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "CBAM 대응 탄소 감축 로드맵 점검", "rationale": "EU 탄소국경조정 비용 영향 재산출", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "수출 시장 다변화 추진", "rationale": "중국 리스크 분산 — 동남아·인도 시장 확대", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
        "normal": [
            {"title": "기존 원료 조달 계획 유지", "rationale": "안정 구간 — 현행 소싱·헤지 전략 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            {"title": "전기로·친환경 설비 투자 검토", "rationale": "안정기 활용한 저탄소 설비 전환 추진", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            {"title": "고부가 강재 신규 수출처 개척", "rationale": "안정적 원가 환경에서 프리미엄 시장 진출", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
        ],
    },
    # ── 일반 (기본값) ─────────────────────────────────
    "일반": {
        "danger": [
            {"title": "환헤지 비중 긴급 확대", "rationale": "환율 급등 — 수출입 환율 리스크 최대 방어", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
            {"title": "수입 원자재 비용 긴급 점검", "rationale": "수입 원가 급등 대비 대체 소싱·선구매 검토", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            {"title": "수출 단가 긴급 재산정", "rationale": "원가 급등분 반영 — 수출 단가 즉시 조정", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
        ],
        "warning": [
            {"title": "환율 동향 일별 모니터링", "rationale": "경고 구간 — 일별 환율 추적 및 대응 체계 가동", "urgency": "즉시", "difficulty": "낮음", "impact": "중간"},
            {"title": "주요 수출 계약 환율 조건 재검토", "rationale": "기존 계약 환율 조건 점검 및 갱신 협상 준비", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
            {"title": "운전자본 조달 조건 사전 확인", "rationale": "금리·환율 상승기 차입 조건 사전 점검", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
        ],
        "caution": [
            {"title": "주간 거시지표 리뷰 체계 점검", "rationale": "주의 구간 — 환율·물가·금리 주간 모니터링", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
            {"title": "수출 시장 다변화 검토", "rationale": "시장 리스크 분산을 위한 신규 수출처 탐색", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "원가 절감 과제 재점검", "rationale": "비용 상승 추세 대비 원가 절감 과제 우선순위 조정", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
        ],
        "normal": [
            {"title": "현행 수출입 전략 유지", "rationale": "안정 구간 — 기존 전략 유지하며 시장 모니터링", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            {"title": "신규 시장·채널 개척", "rationale": "안정기 활용한 수출 시장 확대 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            {"title": "중장기 투자 계획 구체화", "rationale": "안정적 환경에서 설비·인력 투자 계획 수립", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
        ],
    },
}


def generate_decision_options(
    macro_data: dict,
    industry_key: str,
    signal: dict | None,
) -> list[dict]:
    """전략 옵션 3가지를 반환.

    Parameters:
        macro_data: 거시지표 딕셔너리
        industry_key: 산업 키 (반도체, 자동차 등)
        signal: today_signal.py의 generate_today_signal() 반환값

    Returns:
        [{"option": "A", "title", "rationale", "urgency", "difficulty", "impact"}, ...]
    """
    if not signal:
        return []

    # 핵심 신호의 상태 판정
    label = signal.get("label", "")
    try:
        val = float(str(signal.get("value", "0")).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        val = 0

    status = _get_status(label, val)

    # 산업별 템플릿 조회 (fallback: 일반)
    industry_templates = DECISION_TEMPLATES.get(
        industry_key, DECISION_TEMPLATES["일반"]
    )
    options = industry_templates.get(status, industry_templates.get("normal", []))

    # 옵션 라벨 부여
    result = []
    for idx, opt in enumerate(options[:3]):
        result.append({
            "option": chr(65 + idx),  # A, B, C
            **opt,
        })

    return result
