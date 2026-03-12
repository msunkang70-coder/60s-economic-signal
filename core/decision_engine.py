"""
core/decision_engine.py
수출기업 CEO/전략담당 대상 — 전략 옵션 3가지 자동 생성 엔진.
"""

from core.industry_config import get_profile
from core.utils import safe_execute

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


def _label_to_category(label: str) -> str:
    """지표명 → 전략 카테고리 (환율/수출/물가/금리) 매핑."""
    if "환율" in label and "100엔" not in label:
        return "환율"
    if "수출" in label and "물가" not in label:
        return "수출"
    if "물가" in label or "CPI" in label:
        return "물가"
    if "금리" in label:
        return "금리"
    if "100엔" in label:
        return "환율"
    if "수입물가" in label:
        return "물가"
    return "환율"  # fallback


# ══════════════════════════════════════════════════════
# 전략 옵션 템플릿: {산업key: {카테고리: {상태: [옵션 3개]}}}
# ══════════════════════════════════════════════════════
DECISION_TEMPLATES: dict[str, dict[str, dict[str, list[dict]]]] = {
    "반도체": {
        "환율": {
            "danger": [
                {"title": "환헤지 비중 긴급 확대", "rationale": "환율 급등 — 수입 장비·소재 비용 급증 대비", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "수출 선물환 계약 체결", "rationale": "추가 환율 상승 리스크에 대비한 선물환 매도", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "주요 고객사 납품 단가 재협상", "rationale": "원가 급등분 반영한 단가 조정 요청", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
            ],
            "warning": [
                {"title": "환리스크 모니터링 강화", "rationale": "경고 구간 — 일별 환율 추적 및 헤지 타이밍 결정", "urgency": "즉시", "difficulty": "낮음", "impact": "중간"},
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
        "수출": {
            "danger": [
                {"title": "주요 수출국 긴급 수요 점검", "rationale": "수출 급감 — 국가별 바이어 재고·주문 현황 즉시 파악", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "대체 시장 신속 탐색", "rationale": "주력 시장 수요 급감 시 동남아·인도 시장 대안 점검", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "재고·생산 계획 긴급 조정", "rationale": "수출 급감에 따른 재고 적체 방지 — 생산라인 조율", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            ],
            "warning": [
                {"title": "수출 감소 원인 분석", "rationale": "수요 둔화인지 경쟁 심화인지 원인 구분 후 대응", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "신규 바이어 발굴 착수", "rationale": "수출 감소 선제 대응 — 신규 거래처 파이프라인 구축", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "제품 가격경쟁력 재점검", "rationale": "경쟁사 대비 가격 포지션 재산정 후 전략 조정", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "수출 증가세 지속 여부 모니터링", "rationale": "15% 이상 급증 — 생산 병목·재고 부족 가능성 점검", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "생산·공급 능력 검토", "rationale": "수출 급증 지속 시 대응 가능한 생산 여력 확인", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "신규 수출 계약 확대", "rationale": "수출 호조 지속 활용 — 장기 공급 계약 체결 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "normal": [
                {"title": "현행 수출 전략 유지", "rationale": "수출 안정 구간 — 기존 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "수출 시장 다변화 검토", "rationale": "안정기 활용 — 신규 국가·채널 탐색", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "차세대 제품 수출 준비", "rationale": "HBM·AI반도체 신제품 수출 전략 수립", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "물가": {
            "danger": [
                {"title": "반도체 소재·가스 조달 원가 긴급 점검", "rationale": "고물가 지속 — 핵심 소재 비용 급등 영향 즉시 산출", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "고객사 납품 단가 인상 협상", "rationale": "원가 급등분 즉시 반영 — 마진 방어 필수", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
                {"title": "대체 소재·공급처 긴급 발굴", "rationale": "가격 급등 소재 대체 옵션 확보로 원가 리스크 분산", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "원자재·소재 비용 일별 추적", "rationale": "물가 경고 — 핵심 소재 가격 상승 사전 감지", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "단가 전가 가능성 분석", "rationale": "원가 상승분을 고객사에 전가 가능한 수준 분석", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "고마진 제품 비중 확대", "rationale": "원가 상승 압박 대비 고부가 제품 믹스 개선", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "caution": [
                {"title": "월간 원가 구조 리뷰", "rationale": "주의 구간 — 소재·에너지 비용 월별 추적 체계 점검", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "장기 소재 공급 계약 재검토", "rationale": "가격 변동 리스크 최소화를 위한 고정가 계약 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "원가 절감 R&D 과제 점검", "rationale": "공정 효율화·대체 소재 개발 투자 우선순위 설정", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "normal": [
                {"title": "현행 조달 전략 유지", "rationale": "물가 안정 — 기존 소재 조달 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "원가 절감 기회 탐색", "rationale": "안정기 활용 — 소재·공정 최적화 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "설비 투자 본격 추진", "rationale": "원가 안정기 — 증설·효율화 투자 의사결정 적기", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "금리": {
            "warning": [
                {"title": "설비투자 자금 조달 비용 재산정", "rationale": "고금리 — 차입 이자 부담 증가분 투자 수익성에 반영", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "투자 우선순위 재검토", "rationale": "금리 상승기 ROI 낮은 투자 과제 연기 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "고정금리 차입 전환 검토", "rationale": "추가 금리 상승 리스크 차단 — 변동금리 고정 전환", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "danger": [
                {"title": "신규 차입 계획 긴급 재검토", "rationale": "고금리 — 신규 차입 비용 급증, 계획 재조정 필수", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "운전자본 최소화", "rationale": "금리 부담 최소화를 위한 재고·매출채권 긴축 관리", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "대규모 설비투자 연기 검토", "rationale": "고금리 환경에서 대규모 차입 투자 타이밍 재고", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
            ],
            "caution": [
                {"title": "차입 구조 최적화 점검", "rationale": "주의 구간 — 단기·장기 차입 비율 및 금리 조건 점검", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "투자 타이밍 시나리오 분석", "rationale": "금리 방향성 분석 후 투자 집행 타이밍 결정", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현금흐름 관리 강화", "rationale": "금리 변동기 유동성 버퍼 확보 전략 점검", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
            ],
            "normal": [
                {"title": "현행 금융 전략 유지", "rationale": "금리 안정 — 기존 차입·투자 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "장기 설비투자 계획 구체화", "rationale": "안정적 금리 환경 활용 — 투자 실행 계획 수립", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "자금 조달 다변화 검토", "rationale": "안정기 활용 — 회사채·정책자금 등 조달 채널 확대", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
        },
    },
    "자동차": {
        "환율": {
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
        "수출": {
            "danger": [
                {"title": "시장별 판매 실적 긴급 분석", "rationale": "자동차 수출 급감 — 미국·EU·중동 시장별 원인 파악", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "생산 계획 긴급 조정", "rationale": "수출 급감 — 완성차 재고 적체 방지 위해 생산 축소 검토", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
                {"title": "신흥 시장 신속 대응", "rationale": "주력 시장 수요 감소 대비 동남아·중동 시장 확대 추진", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "차종별 수출 부진 원인 분석", "rationale": "수출 감소 원인 규명 — 가격·품질·경쟁사 요인 구분", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "딜러 인센티브 정책 재검토", "rationale": "해외 딜러 판매 촉진을 위한 인센티브 조정 검토", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "전기차 전환 가속 여부 재평가", "rationale": "내연기관 수출 감소 시 EV 전환 속도 조정", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
            "caution": [
                {"title": "생산·재고 부담 모니터링", "rationale": "수출 급증 — 생산 병목 및 재고 부족 가능성 점검", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "수출 호조 시장 집중 공략", "rationale": "수출 증가 시장 추가 물량 배분 및 채널 확대", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "부품 공급망 안정성 점검", "rationale": "수출 급증 지속 시 부품 수급 차질 방지", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "normal": [
                {"title": "현행 판매·수출 계획 유지", "rationale": "수출 안정 — 기존 계획 유지하며 시장 모니터링", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "신규 수출 시장 개척", "rationale": "안정기 활용 — 미개척 시장 진출 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "EV 수출 비중 확대 계획", "rationale": "안정적 환경에서 전기차 수출 로드맵 구체화", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "물가": {
            "danger": [
                {"title": "철강·알루미늄 조달 비용 긴급 점검", "rationale": "고물가 — 자동차 핵심 원자재 비용 급등 영향 즉시 산출", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "부품 납품 단가 재협상", "rationale": "원가 급등분 즉시 반영 — 부품사와 긴급 단가 조정 협상", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
                {"title": "판매가 인상 타이밍 검토", "rationale": "원가 급등 → 판매가 조정 필요성 및 시장 수용 가능성 분석", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "원자재 가격 일별 모니터링", "rationale": "물가 경고 — 철강·비철금속 비용 상승 사전 감지", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "대체 소재 검토", "rationale": "원가 상승 대비 경량 소재·대체재 활용 가능성 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "원가 절감 부품 개발 가속", "rationale": "물가 상승기 내재화·공정 개선으로 원가 압박 완화", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
            "caution": [
                {"title": "월간 원가 구조 점검", "rationale": "주의 구간 — 주요 원자재 비용 월별 추적 강화", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "장기 공급 계약 재검토", "rationale": "가격 변동 리스크 최소화 위한 고정가 조달 계약 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "원가 경쟁력 강화 과제 발굴", "rationale": "물가 상승 추세 대비 원가 절감 R&D 과제 우선순위 설정", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
            "normal": [
                {"title": "현행 조달 전략 유지", "rationale": "물가 안정 — 기존 원자재 조달 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "원가 절감 기회 탐색", "rationale": "안정기 활용 — 원자재·공정 최적화 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "신모델 개발 투자 확대", "rationale": "원가 안정기 — EV·자율주행 신모델 투자 적기", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "금리": {
            "danger": [
                {"title": "할부 금리 인상 영향 긴급 분석", "rationale": "고금리 — 소비자 할부 부담 증가로 판매 둔화 가능성 점검", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "설비투자 차입 계획 긴급 재검토", "rationale": "차입 비용 급증 — 신규 생산라인 투자 타이밍 재조정", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "금리 보조 프로모션 검토", "rationale": "고금리 판매 둔화 대응 — 제조사 금리 지원 프로그램 검토", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "금리 민감 판매 채널 모니터링", "rationale": "금리 경고 — 할부 판매 비중 높은 시장 판매 동향 추적", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "투자 우선순위 재검토", "rationale": "금리 상승기 ROI 낮은 설비투자 연기 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "고정금리 차입 전환", "rationale": "추가 금리 상승 리스크 차단 — 변동금리 고정 전환 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "금리 변동 시나리오 분석", "rationale": "주의 구간 — 금리 추가 상승 시 판매·투자 영향 시뮬레이션", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "차입 구조 최적화", "rationale": "단기·장기 차입 비율 조정으로 금리 리스크 분산", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "EV 정책자금 활용 검토", "rationale": "고금리 대비 정부 EV 투자 지원 자금 우선 활용", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
            ],
            "normal": [
                {"title": "현행 금융 전략 유지", "rationale": "금리 안정 — 기존 차입·투자 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "장기 설비투자 계획 구체화", "rationale": "안정적 금리 — 신공장·EV 라인 투자 실행 계획 수립", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "자금 조달 채널 다변화", "rationale": "안정기 활용 — 회사채·정책자금 조달 채널 확보", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
        },
    },
    "화학": {
        "환율": {
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
        "수출": {
            "danger": [
                {"title": "중국·동남아 수요 긴급 점검", "rationale": "석유화학 수출 급감 — 주요 수출국 재고·수요 현황 즉시 파악", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "생산 물량 긴급 조정", "rationale": "수출 급감에 따른 재고 적체 방지 — 가동률 조정 검토", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "대체 시장 신속 탐색", "rationale": "주력 시장 수요 감소 시 인도·중동 시장 대안 점검", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "수출 감소 원인 규명", "rationale": "가격 경쟁력 vs 수요 둔화 원인 구분 후 맞춤 대응", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "주력 제품 수출 단가 재검토", "rationale": "경쟁사 대비 가격 포지션 재산정", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "고부가 제품 수출 비중 확대", "rationale": "범용 제품 수요 감소 대비 특수화학 비중 전환", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
            "caution": [
                {"title": "수출 급증 지속 가능성 점검", "rationale": "15% 이상 급증 — 일시적 수요인지 구조적 증가인지 분석", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "생산 능력 확대 검토", "rationale": "수출 증가세 지속 시 가동률·증설 여부 의사결정", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "신규 수출 계약 확대 추진", "rationale": "수출 호조 활용 — 장기 공급 계약 체결 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "normal": [
                {"title": "현행 수출 전략 유지", "rationale": "수출 안정 — 기존 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "신규 수출 시장 탐색", "rationale": "안정기 활용 — 신규 국가·용도 개발 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "고부가 특수화학 제품 개발", "rationale": "안정적 환경에서 프리미엄 제품 개발 추진", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "물가": {
            "danger": [
                {"title": "나프타·원유 연동 원가 긴급 재산정", "rationale": "고물가 — 석유화학 핵심 원료 비용 급등 영향 즉시 산출", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "제품 판매가 즉시 인상 통보", "rationale": "원가 급등분 즉시 반영 — 마진 방어", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
                {"title": "에너지 비용 절감 긴급 과제 실행", "rationale": "에너지 집약 산업 — 에너지 비용 급등 대비 절감 조치 즉시 실행", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "원료 가격 일별 추적 강화", "rationale": "물가 경고 — 나프타·원유 연동 원가 상승 사전 감지", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "단가 전가 가능성 분석", "rationale": "원가 상승분을 고객사에 전가 가능 수준 분석", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "CBAM 대응 비용 추가 산정", "rationale": "물가 상승기 EU 탄소비용 추가 부담 통합 시뮬레이션", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "월간 원가 구조 리뷰", "rationale": "주의 구간 — 원료·에너지 비용 월별 추적 강화", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "장기 원료 공급 계약 재검토", "rationale": "가격 변동 리스크 최소화 위한 고정가 조달 계약 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "에너지 효율화 투자 검토", "rationale": "장기 에너지 비용 절감 위한 설비 효율화 투자", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
            "normal": [
                {"title": "현행 조달 전략 유지", "rationale": "물가 안정 — 기존 원료 조달 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "원가 절감 기회 탐색", "rationale": "안정기 활용 — 원료·에너지 최적화 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "고부가 제품 비중 확대 투자", "rationale": "원가 안정기 — 특수화학 증설 투자 적기", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "금리": {
            "danger": [
                {"title": "대규모 설비투자 연기 검토", "rationale": "고금리 — 석유화학 대규모 플랜트 차입 비용 급증, 투자 타이밍 재고", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "운전자본 최소화 조치", "rationale": "금리 부담 최소화 — 재고·매출채권 긴축 관리", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "고정금리 차입 전환 긴급 추진", "rationale": "추가 금리 상승 리스크 차단 — 변동금리 즉시 고정 전환", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "설비투자 수익성 재검토", "rationale": "금리 경고 — 차입 비용 반영한 플랜트 투자 수익성 재산정", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "투자 우선순위 재조정", "rationale": "ROI 낮은 증설 과제 연기 — 고마진 제품 투자 우선", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "정책자금 활용 최대화", "rationale": "고금리 대비 정부 화학 산업 지원 자금 우선 활용", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
            ],
            "caution": [
                {"title": "차입 구조 최적화 점검", "rationale": "주의 구간 — 단기·장기 차입 비율 및 금리 조건 점검", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "투자 타이밍 재검토", "rationale": "금리 방향성 분석 후 증설 투자 집행 타이밍 결정", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현금흐름 관리 강화", "rationale": "금리 변동기 유동성 버퍼 확보", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
            ],
            "normal": [
                {"title": "현행 금융 전략 유지", "rationale": "금리 안정 — 기존 차입·투자 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "플랜트 증설 투자 계획 구체화", "rationale": "안정적 금리 — 대규모 증설 투자 실행 계획 수립", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "자금 조달 다변화 검토", "rationale": "안정기 활용 — 회사채·정책자금 조달 채널 확보", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
        },
    },
    "소비재": {
        "환율": {
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
        "수출": {
            "danger": [
                {"title": "주요 수출국 소비 동향 긴급 점검", "rationale": "소비재 수출 급감 — 국가별 소비 심리·재고 현황 파악", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "재고·생산 계획 긴급 조정", "rationale": "수출 급감 — 재고 적체 방지 위해 생산 계획 축소 검토", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "대체 시장 신속 탐색", "rationale": "주력 시장 수요 감소 — 동남아·중동 신흥 시장 대안 점검", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "수출 감소 원인 분석", "rationale": "소비 둔화 vs 브랜드 경쟁력 약화 원인 구분", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "현지화 마케팅 전략 점검", "rationale": "수출 감소 시장의 소비자 니즈 재파악 후 전략 수정", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "프리미엄 제품 비중 확대", "rationale": "소비 양극화 대응 — 고마진 프리미엄 라인 집중 추진", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
            "caution": [
                {"title": "수출 급증 지속 여부 모니터링", "rationale": "15% 이상 급증 — 일시적인지 트렌드 전환인지 분석", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "생산·물류 능력 점검", "rationale": "수출 증가 지속 시 공급 대응 능력 확인", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "수출 호조 시장 집중 공략", "rationale": "인기 시장 추가 물량 배분 및 채널 확대", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "normal": [
                {"title": "현행 수출 전략 유지", "rationale": "수출 안정 — 기존 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "K-뷰티·K-푸드 신시장 개척", "rationale": "안정기 활용 — 한류 콘텐츠 연계 신규 시장 진출", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "D2C 수출 채널 확대", "rationale": "안정적 환경에서 온라인 직판 채널 구축 추진", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "물가": {
            "danger": [
                {"title": "원료·포장재 조달 비용 긴급 점검", "rationale": "고물가 — 핵심 원료·포장재 비용 급등 영향 즉시 산출", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "판매가 긴급 조정", "rationale": "원가 급등분 즉시 반영 — 마진 방어", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
                {"title": "고마진 제품 우선 생산 전환", "rationale": "원가 압박 최소화 위해 마진 높은 제품 생산 비중 확대", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "원료 가격 주간 추적 강화", "rationale": "물가 경고 — 핵심 원료·포장재 비용 상승 사전 감지", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "판매가 조정 타이밍 분석", "rationale": "소비자 가격 민감도 감안 — 가격 인상 최적 타이밍 산출", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "저원가 대체 원료 탐색", "rationale": "원가 상승 대비 대체 원료·공급처 발굴", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "월간 원가 구조 리뷰", "rationale": "주의 구간 — 원료·물류 비용 월별 추적 강화", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "장기 공급 계약 재검토", "rationale": "가격 변동 리스크 최소화 위한 고정가 조달 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "프리미엄 제품 비중 확대", "rationale": "원가 압박 대비 고마진 프리미엄 라인 전환 가속", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
            "normal": [
                {"title": "현행 조달 전략 유지", "rationale": "물가 안정 — 기존 원료 조달 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "신제품 개발 투자 확대", "rationale": "원가 안정기 — 고부가 신제품 R&D 투자 적기", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "브랜드 마케팅 투자 강화", "rationale": "안정적 환경에서 브랜드 가치 제고 투자 집행", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "금리": {
            "danger": [
                {"title": "운전자본 금융 비용 긴급 점검", "rationale": "고금리 — 재고·매출채권 금융 비용 급증 영향 산출", "urgency": "즉시", "difficulty": "낮음", "impact": "중간"},
                {"title": "재고 최소화 긴축 운영", "rationale": "금리 부담 최소화 — 재고 수준 긴축 관리", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "소비자 구매력 영향 분석", "rationale": "고금리 → 소비자 할부·대출 부담 증가 → 수요 둔화 가능성 분석", "urgency": "이번 주", "difficulty": "낮음", "impact": "높음"},
            ],
            "warning": [
                {"title": "재고 회전율 개선 추진", "rationale": "금리 경고 — 재고 금융 비용 절감 위한 회전율 개선", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "소비자 수요 변화 모니터링", "rationale": "금리 상승 → 소비 위축 가능성 — 시장별 수요 동향 추적", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "마케팅 ROI 재검토", "rationale": "금리 상승기 마케팅 비용 효율화 — ROI 낮은 채널 축소", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "운전자본 관리 점검", "rationale": "주의 구간 — 재고·매출채권 금융 비용 월별 추적", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "소비 트렌드 변화 대응 준비", "rationale": "금리 상승 시 소비 양극화 대응 전략 사전 수립", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "가격 전략 최적화 검토", "rationale": "금리 변동기 소비자 가격 민감도 변화 반영 전략 수립", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "normal": [
                {"title": "현행 금융·운영 전략 유지", "rationale": "금리 안정 — 기존 운전자본·마케팅 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "브랜드·채널 투자 확대", "rationale": "안정적 금리 환경 — 해외 마케팅·유통 투자 집행", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "신제품 라인 확장 추진", "rationale": "안정기 활용 — 프리미엄 신제품 개발·출시 계획 구체화", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
    },
    "조선": {
        "환율": {
            "danger": [
                {"title": "수주 잔고 환헤지 긴급 확대", "rationale": "환율 급등 시 원화 수주분 채산성 악화 — 즉시 선물환 비중 확대", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "후판 조달 선제 계약", "rationale": "원자재(후판·철강) 수입 비용 급등 대비 장기 계약 체결", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "선가 재협상 조건 검토", "rationale": "환율 변동분을 반영한 에스컬레이션 조항 활용 가능성 검토", "urgency": "이번 달", "difficulty": "높음", "impact": "중간"},
            ],
            "warning": [
                {"title": "환헤지 비중 점검", "rationale": "환율 상승 추세 — 기존 헤지 비율 재점검", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "달러 매출 비중 확대 검토", "rationale": "원화 약세 시 달러 기반 수주 확대로 자연 헤지", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "원자재 대체 조달선 확보", "rationale": "국내산 후판 비중 확대로 환율 노출 축소", "urgency": "이번 달", "difficulty": "높음", "impact": "낮음"},
            ],
            "caution": [
                {"title": "환율 모니터링 강화", "rationale": "주간 단위 환율 추이 점검 체계 가동", "urgency": "이번 주", "difficulty": "낮음", "impact": "낮음"},
                {"title": "수주 계약 통화 다변화", "rationale": "유로·엔화 수주 확대로 달러 편중 리스크 분산", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현행 헤지 전략 유지", "rationale": "환율 안정 구간 — 현 수준 유지하되 모니터링 지속", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "현행 환 관리 유지", "rationale": "환율 안정 — 기존 전략 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "중장기 수주 전략 수립", "rationale": "안정적 환율 환경에서 대형 수주 적극 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "설비 투자 타이밍 검토", "rationale": "환율 안정 시 해외 장비 도입 비용 절감 가능", "urgency": "이번 달", "difficulty": "높음", "impact": "중간"},
            ],
        },
        "수출": {
            "danger": [
                {"title": "수주잔고 조기 인도 협의", "rationale": "글로벌 발주 급감 — 기존 수주 조기 인도로 매출 확보", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "방산·해양플랜트 다각화", "rationale": "상선 수요 급감 시 방산·해양 부문 수주 확대", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
                {"title": "원가 절감 태스크포스 가동", "rationale": "수주 절벽 대비 선제적 원가 구조 개선", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "수주 파이프라인 점검", "rationale": "예상 발주 건 실현 가능성 재평가", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "선종별 수익성 분석", "rationale": "수익성 낮은 선종 수주 자제, 고부가 선종 집중", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "해외 조선소 경쟁 동향 파악", "rationale": "중국·일본 조선소 가격 경쟁력 변화 모니터링", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "caution": [
                {"title": "수주 목표 재설정", "rationale": "시장 둔화 조짐 — 연간 수주 목표 보수적 조정", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "기술 차별화 투자", "rationale": "친환경 선박·자율운항 기술로 경쟁 우위 확보", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "현행 수주 전략 유지", "rationale": "시장 안정 — 기존 전략 유지하며 기회 포착", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "적극적 수주 확대", "rationale": "수출 호조 — 대형 프로젝트 적극 입찰", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "고부가 선종 포트폴리오 강화", "rationale": "LNG선·컨테이너선 등 고수익 선종 비중 확대", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "생산 효율화 투자", "rationale": "호황기 수익으로 디지털 야드 전환 투자", "urgency": "이번 달", "difficulty": "높음", "impact": "중간"},
            ],
        },
        "물가": {
            "danger": [
                {"title": "후판·강재 장기 계약 체결", "rationale": "원자재 가격 급등 — 주요 자재 장기 고정가 확보", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "원가 상승분 선가 반영 협의", "rationale": "에스컬레이션 조항으로 원가 상승분 발주처 공유", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
                {"title": "대체 자재·공법 검토", "rationale": "고가 자재 대체품 또는 공법 변경으로 원가 절감", "urgency": "이번 달", "difficulty": "높음", "impact": "중간"},
            ],
            "warning": [
                {"title": "자재비 추이 주간 모니터링", "rationale": "후판·도료·용접봉 가격 변동 주시", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "재고 선확보 검토", "rationale": "추가 인상 전 주요 자재 선매입", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "VE(가치공학) 프로그램 가동", "rationale": "설계 단계 원가 절감 기회 발굴", "urgency": "이번 달", "difficulty": "높음", "impact": "중간"},
            ],
            "caution": [
                {"title": "원자재 가격 모니터링", "rationale": "후판 가격 추이 월간 점검", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "조달 계획 재검토", "rationale": "분기별 자재 조달 물량·시기 최적화", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현행 조달 전략 유지", "rationale": "물가 안정 — 기존 전략 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "전략적 재고 축소", "rationale": "물가 안정 시 재고 비용 최소화", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "장기 공급 계약 재협상", "rationale": "유리한 시장에서 공급 계약 조건 개선", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "설비 현대화 투자", "rationale": "안정적 비용 환경에서 생산성 향상 투자", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "금리": {
            "danger": [
                {"title": "차입 구조 긴급 재편", "rationale": "금리 급등 — 변동금리 차입분 고정금리 전환", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "프로젝트 파이낸싱 조건 재검토", "rationale": "금융 비용 증가분 반영한 수주 수익성 재계산", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
                {"title": "운전자본 효율화", "rationale": "매출채권 회수 가속 + 재고 최적화로 차입 축소", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "금리 리스크 점검", "rationale": "차입금 금리 구조(변동/고정) 비율 재점검", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "선수금 확보 강화", "rationale": "발주처 선수금 비율 확대 협상으로 차입 의존도 축소", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "투자 우선순위 재설정", "rationale": "금리 상승기 비필수 투자 이연 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "낮음"},
            ],
            "caution": [
                {"title": "금리 동향 모니터링", "rationale": "한국은행 기준금리 결정 일정 주시", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "차입 만기 분산", "rationale": "차입금 만기를 분산하여 리파이낸싱 리스크 축소", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현행 금융 전략 유지", "rationale": "금리 안정 — 기존 전략 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "저금리 활용 장기 차입", "rationale": "유리한 금리 환경에서 장기 차입 확대", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "설비 투자 확대", "rationale": "낮은 자금 조달 비용 활용한 시설 투자", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "R&D 투자 강화", "rationale": "저금리 환경에서 친환경·자율운항 기술 개발 가속", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
    },
    "배터리": {
        "환율": {
            "danger": [
                {"title": "리튬·니켈 수입 헤지 긴급 확대", "rationale": "환율 급등 — 핵심 원자재 수입 비용 급증 대비 선물환 확대", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "양극재·전해질 조달 단가 재협상", "rationale": "환율 급등분 반영 — 소재 공급사 납품가 긴급 재협상", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
                {"title": "북미 현지 생산 가속 검토", "rationale": "환율 리스크 축소 위해 현지화 투자 일정 앞당기기 검토", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
            ],
            "warning": [
                {"title": "원자재 환율 연동 모니터링", "rationale": "환율 경고 — 리튬·니켈·코발트 수입 비용 일별 추적", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "IRA/AMPC 보조금 환율 영향 분석", "rationale": "환율 상승 시 달러 기반 보조금 원화 환산 효과 분석", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "수출 채산성 재검토", "rationale": "환율 상승기 수출 마진 변동 시뮬레이션", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "caution": [
                {"title": "환율 추이 주간 점검", "rationale": "주의 구간 — 주 1회 환율·원자재 연동 리뷰", "urgency": "이번 주", "difficulty": "낮음", "impact": "낮음"},
                {"title": "유럽 CRMA 규제 비용 시뮬레이션", "rationale": "환율 변동 + 유럽 규제 비용 복합 영향 사전 분석", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현행 헤지 전략 유지", "rationale": "환율 안정 구간 — 기존 헤지 비율 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "현행 환 관리 유지", "rationale": "환율 안정 — 기존 전략 유지하며 모니터링", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "북미 JV 투자 본격화", "rationale": "안정적 환율에서 북미 합작법인 투자 실행", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "전고체 배터리 R&D 투자 확대", "rationale": "환율 안정기 활용한 차세대 기술 R&D 가속", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "수출": {
            "danger": [
                {"title": "주요 OEM 수주 현황 긴급 점검", "rationale": "배터리 수출 급감 — OEM별 발주 계획 즉시 확인", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "ESS·산업용 배터리 수요 다각화", "rationale": "EV 수요 급감 대비 ESS·전력저장 시장 확대 추진", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "생산 라인 가동률 조정", "rationale": "수출 급감 시 재고 적체 방지 — 가동률 탄력 운영", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            ],
            "warning": [
                {"title": "전기차 보조금 정책 변동 추적", "rationale": "주요 수출국 EV 보조금 축소 시 수요 영향 분석", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "OEM 장기 계약 확보 추진", "rationale": "수출 감소기 장기 공급 계약으로 물량 확보", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "LFP 라인업 확장 검토", "rationale": "저가 배터리 수요 증가 대응 — LFP 제품 포트폴리오 확대", "urgency": "이번 달", "difficulty": "높음", "impact": "중간"},
            ],
            "caution": [
                {"title": "수출 증가세 지속 여부 분석", "rationale": "일시적 호조인지 구조적 성장인지 판단", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "북미 생산 캐파 확충 계획", "rationale": "수출 호조 시 IRA 보조금 활용 북미 증설 가속", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "기술 로드맵 점검", "rationale": "전고체·리튬황 차세대 기술 상용화 일정 재검토", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "normal": [
                {"title": "수출 확대 적극 추진", "rationale": "수출 안정 — OEM 신규 수주 적극 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "유럽 시장 공략 강화", "rationale": "CRMA 규제 충족 기반 유럽 OEM 공급 확대", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "차세대 배터리 양산 준비", "rationale": "안정적 환경에서 전고체 배터리 파일럿 라인 구축", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "물가": {
            "danger": [
                {"title": "리튬·니켈 장기 조달 계약 체결", "rationale": "핵심 원자재 가격 급등 — 장기 고정가 확보 필수", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "양극재 내재화 가속", "rationale": "원자재 급등 시 양극재 자체 생산으로 원가 방어", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
                {"title": "배터리 판매가 인상 협의", "rationale": "원자재 급등분 OEM 판매가에 반영 협상", "urgency": "이번 주", "difficulty": "높음", "impact": "중간"},
            ],
            "warning": [
                {"title": "원자재 가격 주간 모니터링", "rationale": "리튬·니켈·코발트 시세 변동 주시", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "대체 소재 R&D 가속", "rationale": "고가 원자재 대체 — 망간계·나트륨이온 기술 개발", "urgency": "이번 달", "difficulty": "높음", "impact": "중간"},
                {"title": "폐배터리 리사이클 확대", "rationale": "원자재 재활용으로 신규 조달 비용 절감", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "원자재 가격 추이 모니터링", "rationale": "리튬·니켈 가격 월간 추적 강화", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "조달처 다변화 검토", "rationale": "호주·칠레·인도네시아 등 공급원 분산", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현행 조달 전략 유지", "rationale": "물가 안정 — 기존 원자재 조달 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "전략적 재고 최적화", "rationale": "원자재 안정기 — 재고 수준 합리화로 비용 절감", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "공급망 수직 계열화 투자", "rationale": "안정적 원가 환경에서 광산·정련 지분 투자", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "차세대 소재 R&D 강화", "rationale": "원가 안정기 활용한 전고체·실리콘 음극 개발 가속", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "금리": {
            "danger": [
                {"title": "대규모 설비투자 차입 재검토", "rationale": "고금리 — 북미·유럽 공장 투자 차입 비용 급증, 일정 재조정", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "고정금리 전환 긴급 추진", "rationale": "추가 금리 상승 차단 — 변동금리 차입분 즉시 고정 전환", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "정책자금·보조금 활용 극대화", "rationale": "IRA/AMPC 보조금으로 고금리 차입 부담 상쇄", "urgency": "이번 주", "difficulty": "낮음", "impact": "높음"},
            ],
            "warning": [
                {"title": "투자 수익성 재검토", "rationale": "금리 상승분 반영한 신규 공장 ROI 재산정", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "JV 파트너 자금 분담 협상", "rationale": "금리 부담 분산 — 합작 파트너 투자 비율 재협상", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "투자 우선순위 재조정", "rationale": "ROI 높은 프로젝트 우선 — 저수익 투자 이연", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "차입 구조 최적화", "rationale": "단기·장기 차입 비율 점검 — 금리 리스크 분산", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "정부 지원 프로그램 탐색", "rationale": "배터리 산업 특화 정책자금·세제 혜택 활용", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "현행 금융 전략 유지", "rationale": "금리 안정 — 기존 투자·차입 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "저금리 활용 대규모 증설", "rationale": "유리한 금리에서 북미·유럽 기가팩토리 투자 실행", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "전고체 배터리 R&D 투자 확대", "rationale": "낮은 자금 조달 비용 활용한 차세대 기술 개발 가속", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "자금 조달 다변화", "rationale": "안정기 활용 — 녹색채권·정책자금 등 다양한 조달 채널 확보", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
        },
    },
    "철강": {
        "환율": {
            "danger": [
                {"title": "철광석·원료탄 수입 헤지 긴급 확대", "rationale": "환율 급등 — 수입 원료 비용 급증 대비 선물환 확대", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "수출 강재 단가 긴급 재산정", "rationale": "환율 급등분 반영 — 수출 제품 단가 즉시 조정", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
                {"title": "스크랩 국내 조달 비중 확대", "rationale": "수입 의존도 축소 — 국내 스크랩 조달로 환율 리스크 완화", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "원료 수입 비용 일별 추적", "rationale": "환율 경고 — 철광석·원료탄·스크랩 수입 비용 모니터링", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "수출 채산성 재검토", "rationale": "환율 상승기 수출 마진 변동 분석 후 물량 조정", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "CBAM 대응 비용 재산정", "rationale": "환율 변동 + 유럽 탄소비용 복합 영향 시뮬레이션", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "주간 환율·원료가 리뷰", "rationale": "주의 구간 — 환율과 원료 가격 연동 주간 점검", "urgency": "이번 주", "difficulty": "낮음", "impact": "낮음"},
                {"title": "고부가 강종 수출 비중 확대", "rationale": "환율 변동에 덜 민감한 고부가 제품 수출 확대", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현행 환 관리 전략 유지", "rationale": "환율 안정 구간 — 기존 헤지 전략 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "현행 수출입 전략 유지", "rationale": "환율 안정 — 기존 전략 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "전기로 전환 투자 검토", "rationale": "안정적 환율에서 전기로 전환 해외 장비 도입 적기", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "신시장 수출 개척", "rationale": "안정기 활용한 인도·동남아 시장 진출 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
        },
        "수출": {
            "danger": [
                {"title": "중국산 저가 공세 긴급 대응", "rationale": "중국 과잉 생산 덤핑 — 가격 방어 및 무역 구제 조치 검토", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "고부가 강종 전환 가속", "rationale": "범용 강재 수출 급감 — 자동차강판·고합금강 비중 확대", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
                {"title": "생산·재고 긴급 조정", "rationale": "수출 급감 시 재고 적체 방지 — 고로 가동률 조정", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
            ],
            "warning": [
                {"title": "수출 시장별 경쟁 현황 분석", "rationale": "중국산 점유율 변화 및 가격 차이 모니터링", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "반덤핑 관세 대응 준비", "rationale": "주요 수출국 반덤핑 조사 대비 자료 준비", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "자동차강판 수출 확대", "rationale": "고부가 자동차강판으로 수출 포트폴리오 고도화", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
            "caution": [
                {"title": "수출 증가세 지속성 분석", "rationale": "일시적 호조인지 구조적 변화인지 판단", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "CBAM 인증 체계 구축", "rationale": "유럽 수출 지속 위한 탄소발자국 인증 준비", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "현행 수출 전략 유지", "rationale": "시장 안정 — 기존 계획 유지하며 기회 포착", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "적극적 수출 확대", "rationale": "수출 호조 — 고부가 강종 중심 수출 물량 확대", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "그린스틸 브랜드 구축", "rationale": "저탄소 철강 브랜드로 유럽·일본 시장 프리미엄 확보", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "전기로 전환 로드맵 구체화", "rationale": "호황기 수익으로 전기로 전환 투자 가속", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "물가": {
            "danger": [
                {"title": "철광석·원료탄 장기 계약 체결", "rationale": "원자재 가격 급등 — 핵심 원료 장기 고정가 확보", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "강재 판매가 인상 통보", "rationale": "원가 급등분 즉시 반영 — 수요가 단가 조정", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
                {"title": "스크랩 조달 비중 확대", "rationale": "고가 철광석 대체 — 전기로용 스크랩 조달 확대", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "원료 가격 주간 모니터링", "rationale": "철광석·원료탄·스크랩 시세 변동 추적", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "에너지 비용 절감 추진", "rationale": "고로 에너지 효율화 — 전력·가스 비용 절감 과제 실행", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "고부가 강종 비중 확대", "rationale": "원가 상승기 마진 방어 — 자동차강판·전기강판 비중 확대", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
            "caution": [
                {"title": "원자재 가격 추이 점검", "rationale": "철광석·스크랩 가격 월간 추적", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "조달 다변화 검토", "rationale": "호주·브라질 외 인도·아프리카 공급원 확보", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현행 조달 전략 유지", "rationale": "물가 안정 — 기존 원료 조달 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "전략적 재고 최적화", "rationale": "원료 안정기 — 재고 수준 합리화로 비용 절감", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "장기 공급 계약 재협상", "rationale": "유리한 시장에서 원료 공급 조건 개선", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "전기로 전환 투자 확대", "rationale": "안정적 비용 환경에서 탄소중립 설비 투자", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "금리": {
            "danger": [
                {"title": "대규모 설비투자 차입 재검토", "rationale": "고금리 — 전기로 전환·증설 차입 비용 급증, 일정 재조정", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "고정금리 전환 긴급 추진", "rationale": "추가 금리 상승 차단 — 변동금리 차입분 즉시 고정", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
                {"title": "운전자본 긴축 관리", "rationale": "금리 부담 최소화 — 재고·매출채권 회전율 개선", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "설비투자 수익성 재산정", "rationale": "금리 상승분 반영한 전기로 전환 ROI 재계산", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "정책자금 활용 극대화", "rationale": "탄소중립 전환 정부 지원 자금 우선 활용", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "투자 우선순위 재조정", "rationale": "ROI 높은 프로젝트 우선 — 저수익 투자 이연", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "차입 구조 최적화", "rationale": "단기·장기 차입 비율 점검 — 금리 리스크 분산", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "탄소중립 투자 일정 재검토", "rationale": "금리 변동 감안한 설비 투자 타이밍 최적화", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현행 금융 전략 유지", "rationale": "금리 안정 — 기존 차입·투자 계획 유지", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
            ],
            "normal": [
                {"title": "저금리 활용 전기로 전환 가속", "rationale": "유리한 금리에서 대규모 탈탄소 설비 투자 실행", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "R&D 투자 강화", "rationale": "수소환원제철·고효율 전기로 기술 개발 가속", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "자금 조달 다변화", "rationale": "안정기 활용 — 녹색채권·정책자금 등 조달 채널 확보", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
        },
    },
    "일반": {
        "환율": {
            "danger": [
                {"title": "환헤지 비중 긴급 확대 (70% 이상)", "rationale": "환율 급등 — 수출입 환율 리스크 최대 방어, 선물환·옵션 즉시 체결", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "수입 원자재 대체 소싱 긴급 검토", "rationale": "수입 원가 급등 대비 국내산 대체·선구매·장기계약 검토", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "수출 단가 긴급 재산정 및 통보", "rationale": "원가 급등분 반영 — 바이어별 수출 단가 즉시 조정 통보", "urgency": "이번 주", "difficulty": "높음", "impact": "높음"},
            ],
            "warning": [
                {"title": "환율 동향 일별 모니터링 체계 가동", "rationale": "경고 구간 — 일별 환율 추적 + 헤지 타이밍 대시보드 운영", "urgency": "즉시", "difficulty": "낮음", "impact": "중간"},
                {"title": "주요 수출 계약 환율 조건 재검토", "rationale": "기존 계약 환율 조건 점검, 에스컬레이션 조항 활용 협상", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "공급망 원가 구조 재점검", "rationale": "환율 상승에 따른 수입 부품·소재 비용 상승 전체 영향 산출", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "주간 거시지표 리뷰 체계 점검", "rationale": "주의 구간 — 환율·물가·금리 주간 모니터링 체계 정비", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "수출 시장 다변화 전략 수립", "rationale": "특정 시장 편중 리스크 분산을 위한 신규 수출처 구체적 탐색", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "원가 절감 과제 우선순위 재설정", "rationale": "비용 상승 추세 대비 원가 절감 과제 Top 5 선정 및 실행", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "normal": [
                {"title": "현행 환헤지 비중 유지", "rationale": "환율 안정 구간 — 기존 헤지 전략 유지하며 시장 모니터링", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "신규 시장·채널 개척 추진", "rationale": "안정기 활용한 수출 시장 확대 — 디지털 무역 채널 포함", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "중장기 디지털 전환 투자 계획 수립", "rationale": "안정적 환경에서 설비·인력·IT 투자 로드맵 구체화", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "수출": {
            "danger": [
                {"title": "주요 수출 시장 긴급 점검", "rationale": "수출 급감 — 국가별 수요·재고·바이어 주문 현황 즉시 파악", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "생산·재고 계획 긴급 조정", "rationale": "수출 급감에 따른 재고 적체 방지 — 생산라인·물량 즉시 조율", "urgency": "즉시", "difficulty": "중간", "impact": "높음"},
                {"title": "대체 시장·바이어 신속 탐색", "rationale": "주력 시장 수요 급감 시 동남아·인도·중동 시장 구체적 대안 점검", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "수출 감소 근본 원인 분석", "rationale": "수요 둔화 vs 가격경쟁력 약화 vs 규제 변화 — 원인별 맞춤 대응", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "신규 바이어 파이프라인 구축", "rationale": "수출 감소 선제 대응 — KOTRA·무역협회 활용 신규 거래처 발굴", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "가격·비가격 경쟁력 종합 재점검", "rationale": "경쟁사 대비 가격·품질·납기·AS 포지션 종합 재산정", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
            "caution": [
                {"title": "수출 급증 지속 가능성 분석", "rationale": "15% 이상 급증 — 일시적 수요인지 구조적 성장인지 판단", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "생산·공급 능력 한계 점검", "rationale": "수출 급증 지속 시 대응 가능 생산 여력·물류 능력 확인", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
                {"title": "장기 수출 계약 확대 추진", "rationale": "수출 호조 활용 — 핵심 바이어 장기 공급 계약 체결 추진", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "normal": [
                {"title": "현행 수출 전략 유지", "rationale": "수출 안정 — 기존 계획 유지하며 시장 동향 모니터링", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "수출 시장·제품 다변화 검토", "rationale": "안정기 활용 — 신규 국가·채널·제품군 탐색으로 리질리언스 강화", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "수출 디지털 전환 추진", "rationale": "안정적 환경에서 온라인 B2B·크로스보더 이커머스 채널 구축", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "물가": {
            "danger": [
                {"title": "핵심 원자재 조달 비용 긴급 점검", "rationale": "고물가 — 주요 원자재·소재 비용 급등 영향 즉시 전수 산출", "urgency": "즉시", "difficulty": "낮음", "impact": "높음"},
                {"title": "판매가 긴급 조정 및 통보", "rationale": "원가 급등분 즉시 반영 — 바이어별 단가 조정 통보로 마진 방어", "urgency": "즉시", "difficulty": "높음", "impact": "높음"},
                {"title": "대체 원자재·공급처 긴급 발굴", "rationale": "비용 급등 원자재 대체 옵션 확보로 공급망 리질리언스 강화", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "원자재 가격 주간 추적 체계 가동", "rationale": "물가 경고 — 핵심 원자재 비용 상승 사전 감지 대시보드 운영", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "단가 전가 가능 범위 분석", "rationale": "원가 상승분을 고객·바이어에 전가 가능한 수준 정밀 분석", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "고부가 제품 비중 확대 전략", "rationale": "원가 압박 대비 고부가·고마진 제품 믹스 개선 로드맵 수립", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "caution": [
                {"title": "월간 원가 구조 정밀 리뷰", "rationale": "주의 구간 — 주요 원자재·에너지·물류 비용 월별 추적 강화", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "장기 공급 계약 조건 재검토", "rationale": "가격 변동 리스크 최소화 위한 고정가·에스컬레이션 조달 검토", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "원가 절감 과제 Top 5 선정 실행", "rationale": "물가 상승 추세 대비 원가 절감 우선순위 구체적 설정 및 실행", "urgency": "이번 달", "difficulty": "중간", "impact": "높음"},
            ],
            "normal": [
                {"title": "현행 조달 전략 유지", "rationale": "물가 안정 — 기존 원자재 조달 계획 유지하며 시장 모니터링", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "원가 절감 기회 적극 탐색", "rationale": "안정기 활용 — 원자재·공정·물류 최적화 통한 원가 경쟁력 강화", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "설비 효율화 투자 실행", "rationale": "원가 안정기 — 생산성 향상·에너지 효율화 설비 투자 적기", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
            ],
        },
        "금리": {
            "danger": [
                {"title": "전체 차입금 금리 부담 긴급 점검", "rationale": "고금리 — 변동·고정 차입금 이자 비용 급증 영향 전수 산출", "urgency": "즉시", "difficulty": "낮음", "impact": "중간"},
                {"title": "신규 차입·투자 계획 긴급 재검토", "rationale": "차입 비용 급증 — 신규 투자·운전자본 차입 계획 우선순위 재조정", "urgency": "이번 주", "difficulty": "중간", "impact": "높음"},
                {"title": "고정금리 전환 긴급 추진", "rationale": "추가 금리 상승 리스크 차단 — 변동금리 차입분 즉시 고정 전환", "urgency": "이번 주", "difficulty": "중간", "impact": "중간"},
            ],
            "warning": [
                {"title": "차입 비용 사업 수익성 영향 분석", "rationale": "금리 경고 — 차입 이자 증가분의 사업별 수익성 영향 정밀 분석", "urgency": "이번 주", "difficulty": "낮음", "impact": "중간"},
                {"title": "투자 우선순위 ROI 기반 재조정", "rationale": "금리 상승기 ROI 낮은 투자 과제 이연, 고수익 프로젝트 집중", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "정책자금·지원 프로그램 활용 극대화", "rationale": "고금리 대비 정부 지원 자금·세제 혜택 우선 활용 전략 수립", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
            ],
            "caution": [
                {"title": "차입 구조 최적화 정밀 점검", "rationale": "주의 구간 — 단기·장기 차입 비율, 금리 조건, 만기 분산 점검", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
                {"title": "투자 집행 타이밍 시나리오 분석", "rationale": "금리 방향성·정책 변화 분석 후 투자 집행 최적 타이밍 결정", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
                {"title": "현금흐름 관리 체계 강화", "rationale": "금리 변동기 유동성 버퍼 확보 — 매출채권·재고 회전율 개선", "urgency": "이번 달", "difficulty": "낮음", "impact": "중간"},
            ],
            "normal": [
                {"title": "현행 금융 전략 유지", "rationale": "금리 안정 — 기존 차입·투자 계획 유지하며 시장 모니터링", "urgency": "이번 달", "difficulty": "낮음", "impact": "낮음"},
                {"title": "중장기 설비·디지털 전환 투자 실행", "rationale": "안정적 금리 환경 활용 — 투자 실행 계획 구체화 및 착수", "urgency": "이번 달", "difficulty": "높음", "impact": "높음"},
                {"title": "자금 조달 채널 다변화 구축", "rationale": "안정기 활용 — 회사채·정책자금·ESG채권 등 다양한 조달 채널 확보", "urgency": "이번 달", "difficulty": "중간", "impact": "중간"},
            ],
        },
    },
}


_INDICATOR_TYPE_TO_CATEGORY = {
    "fx_usd_rise": "환율",
    "fx_usd_stable": "환율",
    "fx_jpy": "환율",
    "export_drop": "수출",
    "export_surge": "수출",
    "export_normal": "수출",
    "cpi_surge": "물가",
    "cpi_caution": "물가",
    "cpi_normal": "물가",
    "rate_high": "금리",
    "rate_normal": "금리",
    "export_price_drop": "물가",
    "export_price_rise": "물가",
    "import_price_surge": "물가",
    "import_price_normal": "물가",
}


def _indicator_to_category(indicator_type: str) -> str | None:
    """indicator_type → 전략 카테고리. 매핑 없으면 None."""
    return _INDICATOR_TYPE_TO_CATEGORY.get(indicator_type)


@safe_execute(default=[], log_prefix="decision_engine")
def generate_decision_options(
    macro_data: dict,
    industry_key: str,
    signal: dict | None = None,
    company_profile: dict | None = None,
) -> list[dict]:
    """전략 옵션 3가지를 반환.
    indicator_type + status + industry 3축으로 결정.

    Parameters:
        macro_data: 거시지표 딕셔너리
        industry_key: 산업 키 (반도체, 자동차 등)
        signal: today_signal.py의 generate_today_signal() 반환값

    Returns:
        [{"option": "A", "title", "rationale", "urgency", "difficulty", "impact"}, ...]
    """
    if not signal:
        return []

    label = signal.get("label", "")
    try:
        val = float(str(signal.get("value", "0")).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        val = 0

    # indicator_type 우선, 없으면 _label_to_category fallback
    indicator_type = signal.get("indicator_type", "")
    category = _indicator_to_category(indicator_type) if indicator_type else None
    if not category:
        category = _label_to_category(label)

    status = _get_status(label, val)

    # 산업별 템플릿 조회 (fallback: 일반)
    industry_templates = DECISION_TEMPLATES.get(industry_key, DECISION_TEMPLATES["일반"])
    # 카테고리별 조회 (fallback: 환율)
    category_templates = industry_templates.get(category, industry_templates.get("환율", {}))
    # 상태별 조회 (fallback: normal)
    options = category_templates.get(status, category_templates.get("normal", []))

    result = []
    for idx, opt in enumerate(options[:3]):
        result.append({
            "option": chr(65 + idx),
            **opt,
        })

    # Company Profile 기반 전략 difficulty 필터
    if company_profile and result:
        company_size = company_profile.get("company_size", "")
        if company_size == "스타트업/소기업":
            difficulty_order = {"낮음": 0, "중간": 1, "높음": 2}
            result = sorted(result, key=lambda x: difficulty_order.get(x.get("difficulty", "중간"), 1))
        elif company_size == "중견·대기업":
            difficulty_order = {"높음": 0, "중간": 1, "낮음": 2}
            result = sorted(result, key=lambda x: difficulty_order.get(x.get("difficulty", "중간"), 1))

        # segment 기반 전략 타이틀 prefix 추가
        segment = company_profile.get("segment", "전체")
        if segment and segment != "전체" and result:
            # 전략 rationale에 세그먼트 컨텍스트 주입
            for opt in result:
                if "rationale" in opt and segment not in opt["rationale"]:
                    opt["rationale"] = f"[{segment}] {opt['rationale']}"

    return result


# ══════════════════════════════════════════════════════
# 시나리오 기반 전략 생성 (Agent 3 — Strategy Engine)
# ══════════════════════════════════════════════════════

_SCENARIO_PRESETS = {
    "환율 1500 돌파": {"label": "환율(원/$)", "value": 1510},
    "금리 인하": {"label": "기준금리", "value": 2.0},
    "수출 급감": {"label": "수출증가율", "value": -15},
    "고물가 지속": {"label": "소비자물가(CPI)", "value": 3.5},
    "엔저 심화": {"label": "원/100엔 환율", "value": 780},
}


def generate_scenario_strategies(
    macro_data: dict, industry_key: str, scenario: str
) -> list[dict]:
    """시나리오 기반 전략 생성.

    _SCENARIO_PRESETS 에서 매칭 → _label_to_category → _get_status
    → DECISION_TEMPLATES 조회.

    Parameters:
        macro_data:    거시지표 딕셔너리 (현재 사용되지 않지만 확장성 확보)
        industry_key:  산업 키 (반도체, 자동차 등)
        scenario:      시나리오 이름 (_SCENARIO_PRESETS 키)

    Returns:
        [{"option": "A", "title", "rationale", "urgency", "difficulty",
          "impact", "scenario": str}, ...]
    """
    preset = _SCENARIO_PRESETS.get(scenario)
    if not preset:
        return []

    label = preset["label"]
    value = preset["value"]

    category = _label_to_category(label)
    status = _get_status(label, value)

    industry_templates = DECISION_TEMPLATES.get(
        industry_key, DECISION_TEMPLATES["일반"]
    )
    category_templates = industry_templates.get(
        category, industry_templates.get("환율", {})
    )
    options = category_templates.get(status, category_templates.get("normal", []))

    result = []
    for idx, opt in enumerate(options[:3]):
        result.append({
            "option": chr(65 + idx),
            **opt,
            "scenario": scenario,
        })
    return result


_URGENCY_RANK = {"즉시": 3, "이번 주": 2, "이번 달": 1}
_IMPACT_RANK = {"높음": 3, "중간": 2, "낮음": 1}


def compare_strategies(
    options_a: list[dict], options_b: list[dict]
) -> dict:
    """두 전략 세트 비교.

    Returns:
        {
            "common_themes":  list[str],  # 양쪽 모두에 등장하는 전략 title
            "divergences":    list[str],  # 한쪽에만 있는 title
            "urgency_shift":  int,        # B평균긴급도 - A평균긴급도 (양수=B가 더 긴급)
            "risk_delta":     int,        # B평균영향도 - A평균영향도
        }
    """
    titles_a = {opt.get("title", "") for opt in options_a}
    titles_b = {opt.get("title", "") for opt in options_b}

    common = sorted(titles_a & titles_b)
    divergent = sorted((titles_a | titles_b) - (titles_a & titles_b))

    def _avg(opts: list[dict], key: str, rank_map: dict) -> float:
        if not opts:
            return 0.0
        total = sum(rank_map.get(o.get(key, ""), 1) for o in opts)
        return total / len(opts)

    urgency_a = _avg(options_a, "urgency", _URGENCY_RANK)
    urgency_b = _avg(options_b, "urgency", _URGENCY_RANK)

    impact_a = _avg(options_a, "impact", _IMPACT_RANK)
    impact_b = _avg(options_b, "impact", _IMPACT_RANK)

    return {
        "common_themes": common,
        "divergences": divergent,
        "urgency_shift": round(urgency_b - urgency_a),
        "risk_delta": round(impact_b - impact_a),
    }
