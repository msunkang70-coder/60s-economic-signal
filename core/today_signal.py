"""
core/today_signal.py
오늘 가장 중요한 거시지표 1개를 선정하고, 산업별 해석 + 확인 체크리스트를 생성.
"""

from core.industry_config import get_profile

# ── 간소화 임계값 (app.py _THRESHOLDS 참조) ──────────────────
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
    "원/100엔 환율": [
        (0,    800, "danger"),
        (800,  900, "caution"),
        (900, 1100, "normal"),
        (1100, 9999, "caution"),
    ],
    "수출물가지수": [
        (-9999, -5, "danger"),
        (-5,     0, "caution"),
        (0,      5, "normal"),
        (5,   9999, "caution"),
    ],
    "수입물가지수": [
        (-9999, -5, "caution"),
        (-5,     0, "normal"),
        (0,      5, "caution"),
        (5,   9999, "danger"),
    ],
}

_STATUS_SCORE = {"danger": 3, "warning": 2, "caution": 1, "normal": 0}

# ── 산업별 체크리스트 템플릿 ─────────────────────────────────
_CHECKLIST_MAP = {
    "환율": {
        "반도체": ["달러 결제 수출 계약 환율 조건 재확인", "수입 장비·소재 비용 영향 점검"],
        "자동차": ["달러 기준 수출 가격 경쟁력 재산정", "철강 등 수입 원자재 원가 영향 점검"],
        "화학": ["달러 결제 수출 마진 재계산", "나프타·원유 수입 비용 변동 점검"],
        "소비재": ["달러 결제 수출 단가 경쟁력 확인", "수입 포장재·원료 비용 점검"],
        "일반": ["주요 수출 거래의 환율 조건 확인", "수입 원자재 비용 영향 점검"],
    },
    "수출": {
        "반도체": ["주력 반도체 품목별 수출 실적 추이 확인", "주요 수출국 재고·수요 동향 점검"],
        "자동차": ["차종별 수출 실적 변동 확인", "미국·EU 시장 판매 동향 점검"],
        "화학": ["주력 석유화학 제품 수출 물량 변동 확인", "중국·동남아 수요 동향 점검"],
        "소비재": ["주력 제품 수출 실적 변동 확인", "글로벌 소비 경기 동향 점검"],
        "일반": ["주력 수출 품목 실적 변동 확인", "주요 수출 시장 수요 동향 점검"],
    },
    "물가": {
        "반도체": ["반도체 소재·가스 조달 비용 변동 확인", "완제품 판매 단가 조정 필요성 검토"],
        "자동차": ["철강·알루미늄 등 원자재 조달 비용 확인", "부품 납품 단가 재협상 필요 여부 검토"],
        "화학": ["원유·나프타 가격 연동 제품 단가 재산정", "물류·운송비 변동 영향 점검"],
        "소비재": ["포장재·원료 조달 비용 변동 확인", "소비자 판매 가격 조정 타이밍 검토"],
        "일반": ["주요 원자재 조달 비용 변동 확인", "제품 판매 단가 조정 필요성 검토"],
    },
    "금리": {
        "반도체": ["설비 투자·R&D 자금 조달 비용 재검토", "외화 차입 금리 조건 확인"],
        "자동차": ["설비 투자 차입 비용 재산정", "딜러 금융·할부 금리 영향 검토"],
        "화학": ["대규모 설비 투자 차입 비용 재산정", "운전자본 조달 조건 재검토"],
        "소비재": ["물류·재고 운전자본 금융 비용 확인", "소비자 할부 금리 영향 검토"],
        "일반": ["차입금 금리 부담 변동 확인", "신규 투자 자금 조달 조건 검토"],
    },
}

# ── 산업별 영향 해석 ─────────────────────────────────────────
_IMPACT_MAP = {
    "환율": {
        "high": {
            "반도체": "수출 채산성 개선 구간 — 달러 수금 환전 적기, 수입 장비·소재 비용 상승 주의",
            "자동차": "달러 수출 수익 증가 — 수입 철강·부품 원가 상승 동시 점검 필요",
            "화학": "달러 매출 환산 이익 증가 — 원유·나프타 수입 비용 상승 주의",
            "소비재": "수출 매출 환산 이익 증가 — 수입 원료·포장재 비용 상승 주의",
            "일반": "수출업 유리 구간 — 달러 수금 시 환전 적기, 원자재 수입 원가 상승 주의",
        },
        "low": {
            "반도체": "수출 가격경쟁력 약화 — 수출 단가 재검토 필요",
            "자동차": "수출 가격경쟁력 약화 — 해외 시장 판매 단가 재산정 필요",
            "화학": "수출 가격경쟁력 약화 — 달러 기준 수출 단가 재검토",
            "소비재": "수출 가격경쟁력 약화 — 해외 판매 단가 재산정 필요",
            "일반": "수출 가격경쟁력 약화 — 수출 단가 재검토 필요",
        },
    },
    "수출": {
        "high": {
            "반도체": "반도체 수출 호조 — 생산 증설 및 신규 수주 적극 검토 시점",
            "자동차": "완성차·부품 수출 반등세 — 생산·재고 확대 검토",
            "화학": "석유화학 수출 회복세 — 주력 제품 물량 확대 검토",
            "소비재": "소비재 수출 호조 — 주요 시장 신규 채널 확대 검토",
            "일반": "수출 호조 — 생산·재고 확대 검토 시점",
        },
        "low": {
            "반도체": "반도체 수출 감소 — 주요 수출국 수요 긴급 점검",
            "자동차": "자동차 수출 감소세 — 시장별 판매 전략 재점검",
            "화학": "화학제품 수출 감소 — 중국·동남아 수요 긴급 점검",
            "소비재": "소비재 수출 감소 — 글로벌 소비 경기 둔화 영향 점검",
            "일반": "수출 감소세 — 주요 수출 시장 수요 점검 필요",
        },
    },
    "물가": {
        "high": {
            "반도체": "고물가 지속 — 소재·가스 조달 비용 상승 압박 점검",
            "자동차": "고물가 — 원자재 조달·부품 비용 상승, 단가 전가 검토",
            "화학": "고물가 — 원유 연동 원가 상승, 제품 가격 재산정 필요",
            "소비재": "고물가 — 원료·물류 비용 상승, 판매가 조정 검토",
            "일반": "고물가 지속 — 원가 상승 반영한 단가 재산정 검토",
        },
        "low": {
            "반도체": "물가 안정 — 원가 부담 완화, 투자 여력 확보 기회",
            "자동차": "물가 안정 — 원자재 비용 완화 구간",
            "화학": "물가 안정 — 원가 부담 완화 국면",
            "소비재": "물가 안정 — 원가 부담 완화, 마진 개선 기회",
            "일반": "물가 안정 — 원가 부담 완화 국면",
        },
    },
    "금리": {
        "high": {
            "반도체": "고금리 — 설비 투자·R&D 자금 조달 비용 증가, 투자 우선순위 재검토",
            "자동차": "고금리 — 설비 투자 차입 부담 증가, 할부 판매 영향 점검",
            "화학": "고금리 — 대규모 설비 투자 차입 부담 심화",
            "소비재": "고금리 — 운전자본 금융 비용 증가, 재고 관리 효율화 필요",
            "일반": "고금리 — 금융 비용 부담 증가, 운전자본 조달 조건 재검토",
        },
        "low": {
            "반도체": "저금리 — 설비 투자·R&D 자금 조달 유리, 확장 투자 검토 적기",
            "자동차": "저금리 — 설비 확장 자금 조달 유리",
            "화학": "저금리 — 대규모 설비 투자 조달 유리",
            "소비재": "저금리 — 사업 확장 자금 조달 유리",
            "일반": "저금리 — 설비 투자·시설 확장 자금 조달 유리",
        },
    },
}


def _get_status(label: str, value: float) -> str:
    for lo, hi, status in _THRESHOLDS.get(label, []):
        if lo <= value < hi:
            return status
    return "normal"


def _label_to_category(label: str) -> str:
    """지표명 → 체크리스트 카테고리 매핑."""
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


def _get_impact(category: str, high_or_low: str, industry_key: str) -> str:
    cat_map = _IMPACT_MAP.get(category, _IMPACT_MAP["환율"])
    hl_map = cat_map.get(high_or_low, cat_map.get("high", {}))
    return hl_map.get(industry_key, hl_map.get("일반", "해당 지표 변동에 따른 영향 점검 필요"))


def generate_today_signal(macro_data: dict, industry_key: str) -> dict | None:
    """오늘 가장 중요한 경제 신호 1개를 선택하여 반환.

    Parameters:
        macro_data: app.py의 _MACRO 딕셔너리
        industry_key: "반도체", "자동차", "화학", "소비재", "일반" 중 하나

    Returns:
        {"label", "value", "trend", "impact", "checklist"} 또는 None
    """
    if not macro_data:
        return None

    profile = get_profile(industry_key)
    weights = profile.get("macro_weights", {})

    scored: list[tuple[float, str, dict]] = []

    for label, data in macro_data.items():
        if label.startswith("_"):
            continue
        if not isinstance(data, dict):
            continue

        weight = weights.get(label, 1.0)
        trend = data.get("trend", "→")

        try:
            val = float(str(data.get("value", "0")).replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            continue

        change_score = 2.0 if trend in ("▲", "▼") else 0.5
        status = _get_status(label, val)
        threshold_score = _STATUS_SCORE.get(status, 0)

        # 곱셈형 스코어: 변화 방향 × 산업 가중치 × 임계값 초과 점수
        # normal(0)이면 최소 0.5를 부여하여 전부 0이 되는 것을 방지
        total = change_score * weight * max(threshold_score, 0.5)
        scored.append((total, label, data))

    if not scored:
        return None

    # 모든 지표가 "→"이고 모두 normal이면 None
    all_stable = all(
        d.get("trend", "→") == "→" and _get_status(lbl, float(str(d.get("value", "0")).replace(",", "").replace("+", ""))) == "normal"
        for _, lbl, d in scored
        if isinstance(d, dict)
    )
    if all_stable:
        return None

    scored.sort(key=lambda x: -x[0])
    _, best_label, best_data = scored[0]

    val_str = str(best_data.get("value", ""))
    trend = best_data.get("trend", "→")
    category = _label_to_category(best_label)

    # impact 결정
    try:
        val_f = float(val_str.replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        val_f = 0
    status = _get_status(best_label, val_f)
    high_or_low = "high" if status in ("warning", "danger", "caution") and trend != "▼" else "low"
    # 특별 케이스: 수출증가율은 높을수록 좋음
    if "수출" in best_label and "물가" not in best_label:
        high_or_low = "high" if val_f > 0 else "low"
    # 금리: 높으면 high
    if "금리" in best_label:
        high_or_low = "high" if val_f >= 3.0 else "low"

    impact = _get_impact(category, high_or_low, industry_key)

    # checklist
    cl_map = _CHECKLIST_MAP.get(category, _CHECKLIST_MAP["환율"])
    checklist = cl_map.get(industry_key, cl_map.get("일반", []))

    return {
        "label": best_label,
        "value": val_str,
        "trend": trend,
        "impact": impact,
        "checklist": checklist,
    }
