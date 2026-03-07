"""
core/signal_interpreter.py
거시지표 → 산업별 4단계 해석 엔진

구조: Signal → Impact → Risk/Opportunity → Suggested Action
같은 지표라도 산업에 따라 다른 해석을 반환.
"""

from core.industry_config import get_profile

# ── 산업별 지표 해석 매트릭스 ─────────────────────────────────
# 구조: _INTERPRET[지표카테고리][high/low][산업] = {signal, impact, risk, action}
_INTERPRET = {
    "환율": {
        "high": {  # 원화 약세 (환율 높음)
            "반도체": {
                "signal": "원화 약세 — 수출 가격경쟁력 개선",
                "impact": "달러 기준 수출 마진 확대 가능",
                "risk": "수입 장비·소재(EUV, 웨이퍼) 비용 상승",
                "action": "수출 계약 환율 조건 재확인, 수입 장비 비용 헤지 검토",
            },
            "자동차": {
                "signal": "원화 약세 — 수출 수익성 개선",
                "impact": "완성차·부품 달러 매출 환산 이익 증가",
                "risk": "수입 철강·알루미늄 원자재 원가 상승",
                "action": "수출 가격 경쟁력 재산정, 원자재 조달 비용 모니터링",
            },
            "화학": {
                "signal": "원화 약세 — 수출 환산 이익 증가",
                "impact": "달러 매출 환산 시 마진 개선",
                "risk": "원유·나프타 수입 비용 상승 (이중 효과)",
                "action": "수출 마진 재계산, 원유 연동 원가 구조 점검",
            },
            "소비재": {
                "signal": "원화 약세 — 수출 매출 증가 효과",
                "impact": "해외 판매 환산 이익 증가",
                "risk": "수입 원료·포장재 비용 상승",
                "action": "해외 판매 단가 재검토, 수입 원료 대체 조달처 확인",
            },
            "배터리": {
                "signal": "원화 약세 — 배터리 수출 마진 개선",
                "impact": "달러 기준 수출 채산성 개선",
                "risk": "리튬·니켈 등 수입 원자재 비용 상승",
                "action": "수출 계약 환율 조건 확인, 원재료 헤지 전략 점검",
            },
            "조선": {
                "signal": "원화 약세 — 수주 경쟁력 강화",
                "impact": "달러 기준 선가 경쟁력 개선, 기수주 잔량 환산 이익 증가",
                "risk": "수입 후판·기자재 비용 상승",
                "action": "신규 수주 가격 경쟁력 활용, 원자재 조달 비용 점검",
            },
            "철강": {
                "signal": "원화 약세 — 수출 가격경쟁력 개선",
                "impact": "달러 기준 수출 단가 경쟁력 강화",
                "risk": "철광석·원료탄 수입 비용 상승",
                "action": "수출 물량 확대 검토, 원료 조달 비용 헤지 점검",
            },
            "일반": {
                "signal": "원화 약세 — 수출 유리 환경",
                "impact": "수출 가격경쟁력 개선",
                "risk": "원자재 수입 비용 상승",
                "action": "수출 계약 환율 조건 확인, 수입 원가 영향 점검",
            },
        },
        "low": {  # 원화 강세
            "반도체": {
                "signal": "원화 강세 — 수출 가격경쟁력 약화",
                "impact": "달러 기준 수출 단가 상대적 상승",
                "risk": "가격 경쟁력 약화로 수주 감소 가능",
                "action": "수출 단가 재검토, 비가격 경쟁력(기술력) 강화 검토",
            },
            "자동차": {
                "signal": "원화 강세 — 수출 마진 압박",
                "impact": "해외 시장 가격 경쟁력 약화",
                "risk": "일본차 대비 가격 우위 축소",
                "action": "수출 가격 조정 검토, 원가 절감 방안 점검",
            },
            "화학": {
                "signal": "원화 강세 — 수출 채산성 악화",
                "impact": "달러 기준 수출 마진 축소",
                "risk": "중국산 저가 화학제품 대비 가격 열위",
                "action": "수출 단가 조정, 고부가 제품 비중 확대 검토",
            },
            "소비재": {
                "signal": "원화 강세 — 가격 경쟁력 약화",
                "impact": "해외 판매 마진 축소",
                "risk": "현지 브랜드 대비 가격 우위 축소",
                "action": "해외 판매 단가 재조정, 브랜드 가치 마케팅 강화",
            },
            "배터리": {
                "signal": "원화 강세 — 수출 채산성 악화",
                "impact": "달러 기준 배터리 수출 마진 축소",
                "risk": "중국산 LFP 대비 가격 경쟁력 약화",
                "action": "원가 절감 방안 점검, 기술 차별화 전략 강화",
            },
            "조선": {
                "signal": "원화 강세 — 수주 가격 경쟁력 약화",
                "impact": "달러 기준 선가 경쟁력 축소",
                "risk": "중국·일본 조선사 대비 가격 열위",
                "action": "고부가 선종(LNG선) 수주 집중, 원가 절감 검토",
            },
            "철강": {
                "signal": "원화 강세 — 수출 마진 압박",
                "impact": "달러 기준 수출 단가 경쟁력 약화",
                "risk": "중국산 저가 철강 대비 가격 열위 심화",
                "action": "고부가 강종 수출 비중 확대, 내수 전환 검토",
            },
            "일반": {
                "signal": "원화 강세 — 수출 불리 환경",
                "impact": "수출 가격경쟁력 약화",
                "risk": "수주 감소 가능",
                "action": "수출 단가 재검토 필요",
            },
        },
    },
    "수출": {
        "high": {
            "반도체": {"signal": "수출 증가세", "impact": "반도체 수요 회복 신호", "risk": "과잉 재고 리스크", "action": "생산 증설 및 신규 수주 적극 검토"},
            "자동차": {"signal": "수출 증가세", "impact": "완성차·부품 수출 반등", "risk": "물류 병목 가능", "action": "생산·출하 계획 상향 조정 검토"},
            "화학": {"signal": "수출 증가세", "impact": "석유화학 수출 회복", "risk": "원자재 가격 동반 상승", "action": "주력 제품 물량 확대 검토"},
            "소비재": {"signal": "수출 증가세", "impact": "소비재 수출 호조", "risk": "물류비 상승 동반 가능", "action": "신규 채널·시장 확대 검토"},
            "배터리": {"signal": "수출 증가세", "impact": "배터리 수출 호조", "risk": "원자재 수급 병목 가능", "action": "생산 확대 및 원자재 확보 검토"},
            "조선": {"signal": "수출 증가세", "impact": "해운 물동량 증가 → 선박 수요 증가", "risk": "후판 등 원자재 수급 압박", "action": "수주 확대 및 건조 일정 점검"},
            "철강": {"signal": "수출 증가세", "impact": "철강 수출 물량 회복", "risk": "중국 수출 경쟁 심화", "action": "주력 수출 시장 점유율 방어 전략 검토"},
            "일반": {"signal": "수출 증가세", "impact": "수출 경기 회복", "risk": "과잉 생산 리스크", "action": "생산·재고 확대 검토"},
        },
        "low": {
            "반도체": {"signal": "수출 감소세", "impact": "글로벌 반도체 수요 위축", "risk": "재고 누적·가격 하락", "action": "주요 수출국 수요 긴급 점검"},
            "자동차": {"signal": "수출 감소세", "impact": "해외 판매 둔화", "risk": "재고 증가·수익성 악화", "action": "시장별 판매 전략 재점검"},
            "화학": {"signal": "수출 감소세", "impact": "화학제품 수출 위축", "risk": "가동률 하락·단가 하락", "action": "중국·동남아 수요 긴급 점검"},
            "소비재": {"signal": "수출 감소세", "impact": "글로벌 소비 위축", "risk": "재고 누적·마진 악화", "action": "주요 시장 소비 동향 점검"},
            "배터리": {"signal": "수출 감소세", "impact": "전기차 수요 둔화 → 배터리 수출 위축", "risk": "재고 누적·가격 하락", "action": "주요 시장 전기차 판매 동향 긴급 점검"},
            "조선": {"signal": "수출 감소세", "impact": "해운 물동량 감소 → 신조선 발주 둔화", "risk": "수주잔량 소진 우려", "action": "수주 파이프라인 점검, 해양플랜트 수주 검토"},
            "철강": {"signal": "수출 감소세", "impact": "철강 수출 물량 감소", "risk": "가동률 하락·수익성 악화", "action": "내수 전환 및 수출 시장 다변화 검토"},
            "일반": {"signal": "수출 감소세", "impact": "수출 경기 둔화", "risk": "매출 감소", "action": "주요 시장 수요 동향 점검"},
        },
    },
    "물가": {
        "high": {
            "반도체": {"signal": "고물가 지속", "impact": "소재·가스 조달 비용 상승", "risk": "마진 압박", "action": "완제품 판매 단가 조정 검토"},
            "자동차": {"signal": "고물가 지속", "impact": "원자재·부품 비용 상승", "risk": "납품 단가 인상 압력", "action": "단가 재협상 및 원가 절감 검토"},
            "화학": {"signal": "고물가 지속", "impact": "원유 연동 원가 상승", "risk": "제품 가격 전가 어려울 수 있음", "action": "제품 단가 재산정 필요"},
            "소비재": {"signal": "고물가 지속", "impact": "원료·물류 비용 상승", "risk": "소비자 구매력 약화", "action": "판매가 조정 타이밍 검토"},
            "배터리": {"signal": "고물가 지속", "impact": "리튬·니켈 등 원자재 비용 상승", "risk": "배터리 단가 상승 → 전기차 가격 인상", "action": "원자재 장기계약 조건 재검토"},
            "조선": {"signal": "고물가 지속", "impact": "후판·기자재 조달 비용 상승", "risk": "건조 원가 상승 → 수주 마진 압박", "action": "자재 조달 단가 재협상 검토"},
            "철강": {"signal": "고물가 지속", "impact": "철광석·에너지 비용 상승", "risk": "생산 원가 상승 → 마진 압박", "action": "제품 판매가 인상 및 원가 절감 검토"},
            "일반": {"signal": "고물가 지속", "impact": "원가 상승 압박", "risk": "마진 축소", "action": "단가 재산정 검토"},
        },
        "low": {
            "반도체": {"signal": "물가 안정", "impact": "원가 부담 완화", "risk": "—", "action": "투자 여력 확보 기회 활용"},
            "자동차": {"signal": "물가 안정", "impact": "원자재 비용 안정", "risk": "—", "action": "원가 경쟁력 재확인"},
            "화학": {"signal": "물가 안정", "impact": "원가 부담 완화", "risk": "—", "action": "마진 개선 기회 활용"},
            "소비재": {"signal": "물가 안정", "impact": "원가 안정", "risk": "—", "action": "마진 개선 기회 활용"},
            "배터리": {"signal": "물가 안정", "impact": "원자재 비용 안정", "risk": "—", "action": "원가 경쟁력 확보 기회 활용"},
            "조선": {"signal": "물가 안정", "impact": "건조 원가 안정", "risk": "—", "action": "수주 마진 개선 기회 활용"},
            "철강": {"signal": "물가 안정", "impact": "원료 비용 안정", "risk": "—", "action": "마진 개선 기회 활용"},
            "일반": {"signal": "물가 안정", "impact": "원가 부담 완화", "risk": "—", "action": "비용 안정 국면 활용"},
        },
    },
    "금리": {
        "high": {
            "반도체": {"signal": "고금리 환경", "impact": "설비 투자·R&D 자금 조달 비용 증가", "risk": "투자 지연", "action": "투자 우선순위 재검토"},
            "자동차": {"signal": "고금리 환경", "impact": "설비 차입 부담 증가", "risk": "할부 판매 영향", "action": "투자 일정 조정 검토"},
            "화학": {"signal": "고금리 환경", "impact": "대규모 설비 투자 부담 심화", "risk": "증설 지연", "action": "자금 조달 조건 재검토"},
            "소비재": {"signal": "고금리 환경", "impact": "운전자본 금융 비용 증가", "risk": "재고 부담 증가", "action": "재고 효율화, 조달 조건 검토"},
            "배터리": {"signal": "고금리 환경", "impact": "대규모 생산라인 투자 부담 심화", "risk": "증설 투자 지연", "action": "투자 일정 재검토, 정부 보조금 활용 검토"},
            "조선": {"signal": "고금리 환경", "impact": "선박 금융 비용 증가 → 발주 위축 가능", "risk": "신규 수주 감소", "action": "기발주 잔량 관리 강화, 금융 조건 모니터링"},
            "철강": {"signal": "고금리 환경", "impact": "설비 투자·운전자본 부담 증가", "risk": "증설 투자 지연", "action": "자금 조달 조건 재검토"},
            "일반": {"signal": "고금리 환경", "impact": "금융 비용 부담 증가", "risk": "투자 위축", "action": "운전자본 조달 조건 재검토"},
        },
        "low": {
            "반도체": {"signal": "저금리 환경", "impact": "설비 투자·R&D 자금 조달 유리", "risk": "—", "action": "확장 투자 검토 적기"},
            "자동차": {"signal": "저금리 환경", "impact": "설비 확장 자금 조달 유리", "risk": "—", "action": "설비 투자 검토"},
            "화학": {"signal": "저금리 환경", "impact": "대규모 증설 자금 조달 유리", "risk": "—", "action": "증설 투자 검토"},
            "소비재": {"signal": "저금리 환경", "impact": "사업 확장 자금 유리", "risk": "—", "action": "사업 확장 검토"},
            "배터리": {"signal": "저금리 환경", "impact": "생산라인 증설 자금 조달 유리", "risk": "—", "action": "증설 투자 적극 검토"},
            "조선": {"signal": "저금리 환경", "impact": "선박 금융 비용 감소 → 발주 촉진", "risk": "—", "action": "수주 확대 기회 활용"},
            "철강": {"signal": "저금리 환경", "impact": "설비 투자 자금 조달 유리", "risk": "—", "action": "고부가 설비 투자 검토"},
            "일반": {"signal": "저금리 환경", "impact": "자금 조달 유리", "risk": "—", "action": "설비 투자 검토"},
        },
    },
}


def _label_to_category(label: str) -> str:
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
    return "환율"


def _determine_high_low(label: str, value: float) -> str:
    """지표값으로 high/low 판단."""
    _MIDPOINTS = {
        "환율": 1400, "수출": 0, "물가": 2.5, "금리": 3.0,
    }
    cat = _label_to_category(label)
    mid = _MIDPOINTS.get(cat, 0)
    return "high" if value >= mid else "low"


def interpret_signal(label: str, value: float, trend: str, industry_key: str) -> dict:
    """
    거시지표 1개 → 산업별 4단계 해석 반환.

    Returns:
        {
            "signal": str,     # 신호 요약
            "impact": str,     # 산업 영향
            "risk": str,       # 리스크/기회
            "action": str,     # 제안 조치
        }
    """
    cat = _label_to_category(label)
    hl = _determine_high_low(label, value)
    cat_data = _INTERPRET.get(cat, _INTERPRET["환율"])
    hl_data = cat_data.get(hl, {})
    return hl_data.get(industry_key, hl_data.get("일반", {
        "signal": f"{label} 변동", "impact": "점검 필요", "risk": "—", "action": "동향 모니터링",
    }))


def interpret_all_signals(macro_data: dict, industry_key: str) -> list[dict]:
    """
    전체 거시지표 → 산업별 해석 리스트 반환 (중요도 내림차순).

    Returns:
        [{
            "label": str, "value": str, "trend": str,
            "signal": str, "impact": str, "risk": str, "action": str,
            "weight": float,
        }, ...]
    """
    profile = get_profile(industry_key)
    weights = profile.get("macro_weights", {})

    results = []
    for label, data in macro_data.items():
        if label.startswith("_") or not isinstance(data, dict):
            continue
        try:
            val = float(str(data.get("value", "0")).replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            continue
        trend = data.get("trend", "→")
        w = weights.get(label, 1.0)
        interp = interpret_signal(label, val, trend, industry_key)
        results.append({
            "label": label,
            "value": data.get("value", ""),
            "trend": trend,
            "unit": data.get("unit", ""),
            "weight": w,
            **interp,
        })
    # 가중치 × 변동성 기준 내림차순 정렬
    results.sort(key=lambda x: -(x["weight"] * (2.0 if x["trend"] != "→" else 0.5)))
    return results
