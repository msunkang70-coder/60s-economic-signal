"""
core/today_signal.py
오늘 가장 중요한 거시지표 1개를 선정하고, 산업별 해석 + 확인 체크리스트를 생성.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

_log = logging.getLogger(__name__)

# Fix C: N/A 파싱 실패 warn-once — 세션당 지표별 1회만 경고
# (generate_today_signal이 렌더링당 4+ 회 호출되어 로그 폭주 방지)
_na_warned_labels: set[str] = set()

from core.industry_config import get_profile
from core.checklist_rules import MACRO_CHECKLIST_MAP as _CHECKLIST_MAP
from core.utils import safe_execute
from core.constants import thresholds_simple, STATUS_SCORE

# ── 임계값: core/constants.py 단일 소스에서 로드 ──────────────────
_THRESHOLDS = thresholds_simple()
_STATUS_SCORE = STATUS_SCORE

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


# ── Impact / Risk / Opportunity 3분류 ────────────────────
_IMPACT_DETAIL_MAP = {
    "환율": {
        "high": {
            # '고환율 구간' 현상 기술 — 방향성 단어('상승으로') 제거해 trend 표시와 충돌 방지
            "impact": "현재 고환율 구간 — 원화 약세로 수출 채산성이 유리한 상태입니다",
            "risk": "수입 원자재·부품 비용이 동반 상승할 수 있습니다",
            "opportunity": "달러 수금 완전 적기 — 환율 이득 확보 가능",
        },
        "low": {
            "impact": "현재 저환율 구간 — 원화 강세로 수출 가격경쟁력이 약화된 상태입니다",
            "risk": "해외 시장 점유율 하락 위험이 있습니다",
            "opportunity": "수입 원자재 조달 비용 절감 기회",
        },
    },
    "수출": {
        "high": {
            "impact": "수출 증가세 유지 — 매출 확대가 기대됩니다",
            "risk": "급증 시 생산 병목·재고 관리 부담 발생 가능",
            "opportunity": "신규 시장 확대 및 생산 증설 검토 적기",
        },
        "low": {
            "impact": "수출 감소세 지속 — 매출·수주 축소가 우려됩니다",
            "risk": "주요 시장 수요 둔화가 장기화될 수 있습니다",
            "opportunity": "내수 전환·신시장 개척으로 리스크 분산 가능",
        },
    },
    "물가": {
        "high": {
            "impact": "고물가 지속 — 원자재·운영 비용 증가 압박 상태입니다",
            "risk": "소비 위축과 판매 단가 전가 어려움이 동시 발생할 수 있습니다",
            "opportunity": "선제적 단가 재산정으로 마진 방어 가능",
        },
        "low": {
            "impact": "물가 안정 구간 — 원가 부담이 완화된 상태입니다",
            "risk": "디플레이션 장기화 시 제품 가격 하락 압력",
            "opportunity": "원가 절감분을 투자·마진 확대에 활용 가능",
        },
    },
    "금리": {
        "high": {
            "impact": "고금리 구간 — 차입·금융 비용 부담이 높은 상태입니다",
            "risk": "설비 투자·운전자본 조달 부담이 심화될 수 있습니다",
            "opportunity": "고금리 예금·단기 운용으로 여유 자금 수익 극대화",
        },
        "low": {
            "impact": "저금리 구간 — 자금 조달 환경이 유리한 상태입니다",
            "risk": "금리 반등 시 변동금리 차입 부담 급증 가능",
            "opportunity": "장기 고정금리 차입 전환 및 설비 확장 검토 적기",
        },
    },
}


def _get_impact_detail(category: str, high_or_low: str, industry_key: str = "일반") -> dict:
    """Impact/Risk/Opportunity 3분류 반환 — V5: 산업별 분화 적용."""
    cat_map = _IMPACT_DETAIL_MAP.get(category, _IMPACT_DETAIL_MAP["환율"])
    base = cat_map.get(high_or_low, cat_map.get("high", {
        "impact": "해당 지표 변동에 따른 직접 영향 점검 필요",
        "risk": "관련 리스크 요인 모니터링 필요",
        "opportunity": "변동 상황에서 활용 가능한 기회 탐색 필요",
    }))

    # V5: 산업별 interpretation_frames로 Impact/Risk/Opp 차별화
    if industry_key and industry_key != "일반":
        try:
            from core.industry_config import get_profile
            profile = get_profile(industry_key)
            label = profile.get("label", industry_key)
            frames = profile.get("interpretation_frames", {})
            if frames:
                _impact_frame = frames.get("impact", "")
                _risk_frame = frames.get("risk", "")
                _opp_frame = frames.get("opportunity", "")
                return {
                    "impact": f"{base['impact']}. <strong>{label}</strong>: {_impact_frame}" if _impact_frame else base["impact"],
                    "risk": f"{base['risk']}. <strong>{label}</strong>: {_risk_frame}" if _risk_frame else base["risk"],
                    "opportunity": f"{base['opportunity']}. <strong>{label}</strong>: {_opp_frame}" if _opp_frame else base["opportunity"],
                }
        except Exception as e:
            _log.warning("Profile interpretation_frames extraction failed for industry_key '%s': %s", industry_key, e)

    return base


def _classify_indicator_type(label: str, value: float) -> str:
    """지표명 + 값 → 세분화된 indicator_type 문자열 반환."""
    if label == "환율(원/$)":
        return "fx_usd_rise" if value >= 1380 else "fx_usd_stable"
    if label == "원/100엔 환율":
        return "fx_jpy"
    if "수출" in label and "물가" not in label:
        if value < 0:
            return "export_drop"
        if value >= 15:
            return "export_surge"
        return "export_normal"
    if "소비자물가" in label or "CPI" in label:
        if value >= 3.0:
            return "cpi_surge"
        if value >= 2.0:
            return "cpi_caution"
        return "cpi_normal"
    if "기준금리" in label:
        return "rate_high" if value >= 3.5 else "rate_normal"
    if "수출물가" in label:
        return "export_price_drop" if value < 0 else "export_price_rise"
    if "수입물가" in label:
        return "import_price_surge" if value >= 5 else "import_price_normal"
    return "other"


# ── 복합 신호 감지 패턴 ──────────────────────────────────────
_COMPOSITE_PATTERNS: list[dict] = [
    {
        "name": "스태그플레이션 위험",
        "conditions": {
            "수출증가율": lambda v, t: v < 0,
            "소비자물가(CPI)": lambda v, t: v >= 3.0,
        },
        "severity": "high",
        "message": "수출 감소 + 고물가 동시 발생 — 스태그플레이션 위험 신호",
    },
    {
        "name": "수출 호황",
        "conditions": {
            "수출증가율": lambda v, t: v >= 10,
            "환율(원/$)": lambda v, t: v >= 1350,
        },
        "severity": "positive",
        "message": "수출 증가 + 원화 약세 — 수출 호황 구간 진입 가능",
    },
    {
        "name": "금융 긴축",
        "conditions": {
            "기준금리": lambda v, t: v >= 3.5,
            "소비자물가(CPI)": lambda v, t: v >= 3.0,
        },
        "severity": "high",
        "message": "고금리 + 고물가 — 금융 긴축 환경, 투자·조달 비용 부담 심화",
    },
    {
        "name": "엔저 리스크",
        "conditions": {
            "원/100엔 환율": lambda v, t: v < 900,
            "수출증가율": lambda v, t: v < 5,
        },
        "severity": "medium",
        "message": "엔화 약세 + 수출 둔화 — 일본 경쟁사 가격 경쟁력 상승 주의",
    },
    {
        "name": "원가 압박",
        "conditions": {
            "수입물가지수": lambda v, t: v >= 3,
            "환율(원/$)": lambda v, t: v >= 1400,
        },
        "severity": "high",
        "message": "수입물가 상승 + 원화 약세 — 원자재 수입 원가 이중 압박",
    },
]


def _detect_composite_signals(macro_data: dict) -> list[dict]:
    """거시지표 복합 패턴을 감지하여 리스트로 반환."""
    if not macro_data:
        return []

    # 지표별 value/trend 파싱
    parsed: dict[str, tuple[float, str]] = {}
    for label, data in macro_data.items():
        if label.startswith("_") or not isinstance(data, dict):
            continue
        try:
            val = float(str(data.get("value", "0")).replace(",", "").replace("+", ""))
            trend = data.get("trend", "→")
            parsed[label] = (val, trend)
        except (ValueError, TypeError):
            _log.debug("Failed to parse macro data for label '%s': %r", label, data.get("value"))
            continue

    detected: list[dict] = []
    for pattern in _COMPOSITE_PATTERNS:
        conditions = pattern["conditions"]
        all_met = True
        for indicator, check_fn in conditions.items():
            if indicator not in parsed:
                all_met = False
                break
            val, trend = parsed[indicator]
            if not check_fn(val, trend):
                all_met = False
                break
        if all_met:
            detected.append({
                "name": pattern["name"],
                "severity": pattern["severity"],
                "message": pattern["message"],
                "indicators": list(conditions.keys()),
            })
    return detected


def _calculate_confidence(data: dict, macro_data: dict) -> float:
    """신호 신뢰도 (0.0~1.0) 산출.

    기준:
      - 데이터 신선도 (as_of 날짜): 7일 이내 1.0, 30일 이내 0.7, 이후 0.4
      - 보강 신호 수: 같은 방향 추세 지표가 많을수록 +0.1 (최대 +0.2)
    """
    base_confidence = 0.7  # 기본값

    # 1) 데이터 신선도 기반
    as_of = data.get("as_of", "") if isinstance(data, dict) else ""
    if as_of:
        try:
            # "2024.01.15", "2024-01-15", "2024/01/15" 등 파싱
            date_str = as_of.replace(".", "-").replace("/", "-").strip()
            # "2024-01" 같은 짧은 형식도 허용
            if len(date_str) <= 7:
                date_str += "-01"
            data_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
            days_old = (datetime.now() - data_date).days
            if days_old <= 7:
                base_confidence = 1.0
            elif days_old <= 30:
                base_confidence = 0.7
            elif days_old <= 90:
                base_confidence = 0.5
            else:
                base_confidence = 0.4
        except (ValueError, TypeError):
            _log.debug("Failed to parse as_of date or calculate confidence: %r", data.get("as_of") if isinstance(data, dict) else data)

    # 2) 보강 신호 (같은 방향 추세 지표 수)
    if macro_data:
        main_trend = data.get("trend", "→") if isinstance(data, dict) else "→"
        if main_trend in ("▲", "▼"):
            same_direction = sum(
                1 for lbl, d in macro_data.items()
                if not lbl.startswith("_") and isinstance(d, dict) and d.get("trend") == main_trend
            )
            # 본인 포함이므로 -1, 보강 지표 2개 이상이면 보너스
            corroborating = max(0, same_direction - 1)
            base_confidence = min(1.0, base_confidence + corroborating * 0.1)

    return round(min(1.0, max(0.0, base_confidence)), 2)


@safe_execute(default=None, log_prefix="today_signal")
def generate_today_signal(macro_data: dict, industry_key: str, company_profile: dict | None = None) -> dict | None:
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
    primary_indicators = profile.get("primary_indicators", [])

    scored: list[tuple[float, str, dict]] = []

    for label, data in macro_data.items():
        if label.startswith("_"):
            continue
        if not isinstance(data, dict):
            continue

        weight = weights.get(label, 1.0)
        trend = data.get("trend", "→")

        raw_val = data.get("value")
        if raw_val is None:
            continue
        # V16 Fix P1-3: N/A 값 사전 필터 — float 변환 전 스킵 (warning → info)
        _raw_str = str(raw_val).strip()
        if _raw_str in ("N/A", "n/a", "", "None", "-", "null", "NaN", "nan"):
            _log.info("지표 '%s' 데이터 없음 (%r) — 스킵", label, raw_val)
            continue
        try:
            val = float(_raw_str.replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            # Fix C: warn-once per label — 동일 지표 반복 경고 억제
            if label not in _na_warned_labels:
                _na_warned_labels.add(label)
                _log.warning("지표 '%s' 값 파싱 실패 (이후 동일 지표 경고 생략): %r", label, raw_val)
            continue

        # 변화 속도(delta) 기반 추가 점수
        try:
            prev_val = float(str(data.get("prev_value", str(val))).replace(",", "").replace("+", ""))
            if prev_val != 0:
                delta_pct = abs((val - prev_val) / prev_val * 100)
            else:
                delta_pct = 0.0
            # 변화율에 따른 가중치: 3% 이상이면 1.5배, 1~3%면 1.2배, 미만이면 1.0배
            if delta_pct >= 3.0:
                change_boost = 1.5
            elif delta_pct >= 1.0:
                change_boost = 1.2
            else:
                change_boost = 1.0
        except (ValueError, TypeError):
            change_boost = 1.0

        change_score = 2.0 if trend in ("▲", "▼") else 0.5
        status = _get_status(label, val)
        threshold_score = _STATUS_SCORE.get(status, 0)

        # 곱셈형 스코어: 변화 방향 × 산업 가중치 × 임계값 점수 × 변화속도 부스트
        # V8: 상한 10.0 적용 — 단일 지표가 과도하게 1위 고정되는 것 방지
        total = min(10.0, change_score * weight * max(threshold_score, 0.5) * change_boost)

        # V9: 산업별 핵심 지표(primary_indicators) 우선 부스트
        # 해당 산업의 핵심 지표가 변동 중(▲/▼)이면 1.6배, 안정(→)이면 1.3배 부스트
        if primary_indicators and label in primary_indicators:
            primary_boost = 1.6 if trend in ("▲", "▼") else 1.3
            total = min(10.0, total * primary_boost)

        scored.append((total, label, data))

    # Company Profile 기반 지표 가중치 보정
    if company_profile:
        export_currency = company_profile.get("export_currency", [])
        export_ratio = company_profile.get("export_ratio", "")
        boosted = []
        for score, label, data in scored:
            boost = 1.0
            if "USD" in export_currency and "환율(원/$)" in label:
                boost *= 1.3
            if "JPY" in export_currency and "100엔" in label:
                boost *= 1.3
            if export_ratio == "70% 이상" and ("수출증가율" in label or "수출물가" in label):
                boost *= 1.2
            boosted.append((score * boost, label, data))
        scored = boosted

        # main_market 기반 가중치 추가
        main_market = company_profile.get("main_market", [])
        if "미국" in main_market:
            # 미국 수출 비중 높으면 환율(원/$) 추가 가중
            for i, (score, label, data) in enumerate(scored):
                if "환율(원/$)" in label:
                    scored[i] = (score * 1.2, label, data)
        if "중국" in main_market:
            # 중국 수출 비중 높으면 수출증가율 추가 가중
            for i, (score, label, data) in enumerate(scored):
                if "수출증가율" in label:
                    scored[i] = (score * 1.15, label, data)

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

    # V9: 산업별 핵심 지표 우선 선택 (two-tier selection)
    # 핵심 지표 중 '유의미한 신호'(trending 또는 비정상 status)가 있으면 그 지표를 선택
    # 단, 비핵심 지표가 danger이면서 핵심 지표가 전혀 변동이 없으면 비핵심 허용
    if primary_indicators:
        primary_scored = [
            (s, lbl, d) for s, lbl, d in scored if lbl in primary_indicators
        ]
        # 핵심 지표 중 유의미한 신호가 있는지 확인
        primary_with_signal = [
            (s, lbl, d) for s, lbl, d in primary_scored
            if d.get("trend", "→") in ("▲", "▼")
            or _get_status(lbl, float(str(d.get("value", "0")).replace(",", "").replace("+", ""))) != "normal"
        ]
        if primary_with_signal:
            # 핵심 지표 중 최고 점수 vs 전체 최고 점수 비교
            # 전체 1위가 danger이면서 핵심 1위 점수의 2.5배 이상일 때만 전체 1위 채택
            top_overall = scored[0][0]
            top_primary = primary_with_signal[0]  # 이미 정렬됨
            if top_overall > top_primary[0] * 2.5:
                # 비핵심 지표가 압도적으로 높을 때는 비핵심 채택 (실질적 위기 상황)
                _, best_label, best_data = scored[0]
            else:
                # 핵심 지표 우선 채택
                _, best_label, best_data = top_primary
        else:
            # 핵심 지표에 유의미한 신호가 없으면 전체 기준
            _, best_label, best_data = scored[0]
    else:
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

    # 지표별 맥락에 맞는 high_or_low 결정
    if "수출" in best_label and "물가" not in best_label:
        # 수출증가율: 양수 = 호조(high), 음수 = 부진(low)
        high_or_low = "high" if val_f > 0 else "low"
    elif "금리" in best_label:
        # 금리: 3% 이상 = 고금리 부담(high), 미만 = 저금리(low)
        high_or_low = "high" if val_f >= 3.0 else "low"
    elif "환율" in best_label and "100엔" not in best_label:
        # 환율: 1380 이상 = 고환율(수출 유리, high), 미만 = 저환율(수입 유리, low)
        high_or_low = "high" if val_f >= 1380 else "low"
    elif "물가" in best_label or "CPI" in best_label:
        # 물가: caution/warning/danger = 고물가 부담(high), normal = 안정(low)
        high_or_low = "high" if status in ("warning", "danger", "caution") else "low"
    elif "수입물가" in best_label:
        # 수입물가: 상승 = 원가 부담(high)
        high_or_low = "high" if trend in ("▲",) or status in ("warning", "danger") else "low"
    else:
        # 기타: 추세와 임계값 조합으로 판단
        high_or_low = "high" if status in ("warning", "danger", "caution") else "low"

    impact = _get_impact(category, high_or_low, industry_key)
    impact_detail = _get_impact_detail(category, high_or_low, industry_key)

    # indicator_type 분류
    indicator_type = _classify_indicator_type(best_label, val_f)

    # checklist
    cl_map = _CHECKLIST_MAP.get(category, _CHECKLIST_MAP["환율"])
    checklist = cl_map.get(industry_key, cl_map.get("일반", []))

    # 변화율 계산 (UI 표시용)
    try:
        _prev = float(str(best_data.get("prev_value", val_str)).replace(",", "").replace("+", ""))
        # prev_value 유효성 검증: 50% 이상 차이나면 비정상 데이터
        if _prev == 0 or abs((val_f - _prev) / _prev) > 0.5:
            _delta = 0.0
            _delta_pct = 0.0
        else:
            _delta = val_f - _prev
            _delta_pct = round(abs(_delta / _prev * 100), 1)
            # delta_pct 상한선 30%
            _delta_pct = min(_delta_pct, 30.0)
    except (ValueError, TypeError):
        _delta = 0.0
        _delta_pct = 0.0

    # 급변 감지
    rapid_change = _delta_pct >= 3.0

    if _delta_pct >= 5.0 and trend == "▲":
        change_alert = f"⚡ +{_delta_pct}% 급등 (고위험)"
    elif _delta_pct >= 5.0 and trend == "▼":
        change_alert = f"⚡ -{_delta_pct}% 급락 (고위험)"
    elif _delta_pct >= 3.0 and trend == "▲":
        change_alert = f"🔺 +{_delta_pct}% 급변 주의"
    elif _delta_pct >= 3.0 and trend == "▼":
        change_alert = f"🔻 -{_delta_pct}% 급변 주의"
    elif _delta_pct >= 1.0:
        change_alert = f"📊 {_delta_pct}% 변동 중"
    else:
        change_alert = ""

    # 복합 신호 감지
    composite_signals = _detect_composite_signals(macro_data)

    # 신뢰도 산출
    confidence = _calculate_confidence(best_data, macro_data)

    return {
        "label": best_label,
        "value": val_str,
        "trend": trend,
        "impact": impact,
        "impact_detail": impact_detail,
        "indicator_type": indicator_type,
        "checklist": checklist,
        "delta": round(_delta, 2),
        "delta_pct": _delta_pct,
        "rapid_change": rapid_change,
        "change_alert": change_alert,
        "composite_signals": composite_signals,
        "confidence": confidence,
    }
