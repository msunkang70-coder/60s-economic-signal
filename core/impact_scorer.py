"""
core/impact_scorer.py
기사별 산업 가중 임팩트 스코어 (1~5).

산출 구조 (총 100점 정규화):
  A. 키워드 매칭 (0~30): keywords × 3 + critical_variables × 5
  B. 거시지표 연동 (0~30): 임계값 초과 지표 수 × macro_weights
  C. 정책 유형 (0~20): 위기대응=20, 규제=15, 구조개편=12, 지원=8, 미분류=5
  D. 시급성 (0~20): 키워드당 +4 (상한 20)
  E. 산업연관 부스트 (0~20): _ind_score × 4 (V7 추가)
  합산(최대 120) → 100점 스케일 정규화 → 1~5 매핑
"""

import copy
import logging

from core.industry_config import get_profile

_log = logging.getLogger(__name__)

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
    """A. 산업 keywords × 3 + extended × 1.5 + critical_variables × 5 (상한 30)."""
    profile = get_profile(industry_key)
    keywords = profile.get("keywords", [])
    crit_vars = profile.get("critical_variables", [])

    # 확장 키워드 (간접 관련 경제 용어)
    try:
        from ui.article_cards import _INDUSTRY_EXTENDED_KW
        ext_kws = _INDUSTRY_EXTENDED_KW.get(industry_key, [])
    except ImportError:
        ext_kws = []

    score = 0.0
    for kw in keywords:
        if kw in text:
            score += 3.0
    for kw in ext_kws:
        if kw in text:
            score += 1.5
    for cv in crit_vars:
        if cv in text:
            score += 5.0
    return min(30.0, score)


def _macro_score(text: str, macro_data: dict | None, industry_key: str) -> float:
    """B. 기사 내 거시지표 키워드가 언급되고 + 해당 지표가 임계값 초과일 때만 가산.

    Score scales proportionally to how far value exceeds threshold:
    - At threshold boundary: base_score (3.0 * weight)
    - Far beyond threshold: max_score (5.0 * weight)
    - Linear interpolation between, capped at 1.0
    """
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
            _log.debug("Failed to parse macro value for indicator '%s': %r", indicator, data.get("value"))
            continue
        lo, hi = _MACRO_THRESHOLDS.get(indicator, (None, None))
        if lo is not None and hi is not None:
            if val < lo or val > hi:
                # Calculate excess ratio based on which threshold exceeded
                if val < lo:
                    excess_ratio = (lo - val) / lo
                else:  # val > hi
                    excess_ratio = (val - hi) / hi

                # Cap excess_ratio at 1.0 for max score
                excess_ratio = min(1.0, excess_ratio)

                # Linear interpolation: base 3.0 to max 5.0
                indicator_score = (3.0 + 2.0 * excess_ratio) * weight
                score += indicator_score
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

    # V7: filter_relevant_docs의 _ind_score 부스트 반영 (산업 연관 기사 우선)
    # V8: 전체 합산을 100점 만점으로 정규화 — ind_boost가 높아도 5점 편향 방지
    ind_boost = min(20.0, article.get("_ind_score", 0) * 4.0)
    raw = a + b + c + d + ind_boost       # 최대 120
    normalized = min(100.0, raw * (100.0 / 120.0))  # 100점 만점 스케일

    # V9: Google News no_fetch 기사 감점 — 본문 추출 불가 기사가 제목만으로 상위 배치되는 문제 방지
    if article.get("no_fetch") or article.get("_google_news"):
        normalized = min(normalized, 25.0)  # 최대 2점으로 제한

    return _score_to_stars(normalized)


def _ind_tier(ind_score: float) -> int:
    """_ind_score를 3단계 연관도 그룹으로 매핑.

    Returns:
        0 = 직접 키워드 매칭 (ind_score >= 2, 직접키워드 ×2 이므로 최소 2)
        1 = 확장 키워드만 매칭 (0 < ind_score < 2)
        2 = 매칭 없음 (ind_score == 0)
    """
    if ind_score >= 2:
        return 0
    if ind_score > 0:
        return 1
    return 2


def score_articles(
    articles: list[dict],
    industry_key: str,
    macro_data: dict | None = None,
) -> list[dict]:
    """기사 리스트에 'impact_score' 키를 추가하여 반환 (원본 수정 X, 복사).

    V8: 3단계 그룹 정렬 — ① 산업 연관도 그룹(직접>확장>무관련) ② 그룹 내 impact_score.
    산업 직접 키워드 매칭 기사가 항상 확장/무관련 기사보다 위에 배치됩니다.
    """
    scored = []
    for art in articles:
        art_copy = copy.copy(art)
        art_copy["impact_score"] = score_article(art, industry_key, macro_data)
        scored.append(art_copy)
    # V8: 그룹 우선 → 그룹 내 impact 정렬
    scored.sort(key=lambda x: (
        _ind_tier(x.get("_ind_score", 0)),   # 0(직접) < 1(확장) < 2(무관련)
        -x["impact_score"],                   # 그룹 내 impact 높은 순
        -x.get("_ind_score", 0),              # 동점 시 연관도 높은 순
    ))
    return scored


# ── 하위 호환 ──────────────────────────────────────────────
def calculate_impact_score(
    article: dict,
    macro_data: dict,
    industry_key: str = "일반",
) -> int:
    """기존 호출 호환용 래퍼."""
    return score_article(article, industry_key, macro_data)


# ════════════════════════════════════════════════════════════════════════════
# 거시경제 임팩트 스코어 (Macro Trend Impact Score)
# ── 기사 채점과 완전히 분리된 독립 모듈 ──────────────────────────────────────
# 설계 원칙:
#   - raw value가 아닌 trend(▲/▼/→)로 정규화 (단위 불일치 문제 해소)
#   - 각 지표의 수출기업 방향성(긍정/부정)을 DIRECTION dict로 명시
#   - 기존 macro_weights 재사용 (새 설정 불필요)
#   - 결과 범위: -3.0 ~ +3.0 (0=중립, +3=매우 우호적, -3=매우 불리)
# ════════════════════════════════════════════════════════════════════════════

import json
import os
from datetime import datetime
from typing import Any

# ── 추이 → 수치 변환 ─────────────────────────────────────────────────────────
_TREND_TO_SCORE: dict[str, float] = {
    "▲": +1.0,
    "▼": -1.0,
    "→": 0.0,
}

# ── 지표별 방향성 ─────────────────────────────────────────────────────────────
# +1 = 해당 지표가 상승(▲)할 때 수출기업에 긍정적
# -1 = 해당 지표가 상승(▲)할 때 수출기업에 부정적
_MACRO_DIRECTION: dict[str, int] = {
    "환율(원/$)":         +1,   # 원화 약세 → 수출 단가 경쟁력 상승
    "수출증가율":          +1,   # 수출 자체 증가 → 직접 긍정
    "기준금리":           -1,   # 금리 상승 → 자금조달 비용 증가
    "소비자물가(CPI)":    -1,   # 물가 상승 → 내수 구매력·원가 부담
    "수출물가지수":        +1,   # 수출 단가 상승 → 수익성 개선
    "수입물가지수":        -1,   # 수입 원자재 비용 상승 → 마진 압박
    "원/100엔 환율":      +1,   # 엔 강세 → 일본 경쟁사 대비 유리
}


def _macro_score_label(score: float) -> str:
    if score >= 2.0:   return "매우 우호적 🟢🟢"
    if score >= 0.8:   return "우호적 🟢"
    if score >= -0.8:  return "중립 🟡"
    if score >= -2.0:  return "비우호적 🔴"
    return "매우 불리 🔴🔴"


def calculate_macro_impact_score(
    macro_data: dict[str, Any],
    industry_key: str,
) -> dict[str, Any]:
    """
    산업별 거시경제 임팩트 스코어를 trend 기반으로 계산한다.

    Args:
        macro_data: data/macro.json 로드 결과 (각 항목에 trend, value, note 포함)
        industry_key: industry_config.py의 산업 키 (예: '반도체', '자동차')

    Returns:
        {
          "total": float,          # -3.0 ~ +3.0
          "label": str,            # "우호적 🟢" 등
          "breakdown": dict,       # 지표별 기여도 {"환율(원/$)": +0.75, ...}
          "top_positive": str,     # 가장 긍정적인 요인명
          "top_negative": str,     # 가장 부정적인 요인명
          "industry": str,
          "computed_at": str,
        }
    """
    profile = get_profile(industry_key)
    weights = profile.get("macro_weights", {})

    breakdown: dict[str, float] = {}
    total = 0.0
    total_weight = 0.0

    for label, item in macro_data.items():
        if not isinstance(item, dict):
            continue
        if label not in weights:
            continue

        trend = item.get("trend", "→")
        direction = _MACRO_DIRECTION.get(label, +1)
        trend_score = _TREND_TO_SCORE.get(trend, 0.0)
        weight = weights[label]

        contribution = trend_score * direction * weight
        breakdown[label] = round(contribution, 3)
        total += contribution
        total_weight += weight

    # -3 ~ +3 범위로 정규화
    normalized = (total / total_weight * 3.0) if total_weight > 0 else 0.0
    normalized = max(-3.0, min(3.0, normalized))

    sorted_bd = sorted(breakdown.items(), key=lambda x: x[1], reverse=True)
    top_positive = sorted_bd[0][0] if sorted_bd and sorted_bd[0][1] > 0 else "—"
    top_negative = sorted_bd[-1][0] if sorted_bd and sorted_bd[-1][1] < 0 else "—"

    return {
        "total": round(normalized, 1),
        "label": _macro_score_label(normalized),
        "breakdown": breakdown,
        "top_positive": top_positive,
        "top_negative": top_negative,
        "industry": industry_key,
        "computed_at": datetime.now().isoformat(),
    }


# ── 직전 데이터 대비 스코어 비교 ──────────────────────────────────────────────
#
# macro.json에 이미 있는 prev_value를 활용해 day 1부터 delta 표시 가능.
#
# 방식: 지표별 "절대 수준 스코어"를 현재값/이전값 각각 계산 후 비교.
#   - 절대 수준 스코어 = sign(value - midpoint) × direction × weight
#   - midpoint: 경제적 중립 기준값 (환율 1320원, CPI 2.5% 등)
#   - 해석: 현재 경기 여건이 이전 데이터 기간보다 개선/악화됐는지 즉시 확인
#
# vs 세션 히스토리 방식:
#   - 세션 방식: "내가 마지막으로 앱을 열었을 때 대비" → 누적 필요
#   - prev_value 방식: "직전 경제 데이터 기간 대비" → 즉시 사용 가능
# ─────────────────────────────────────────────────────────────────────────────

# 지표별 경제적 중립 기준값 (level-based 스코어링 기준)
_LEVEL_MIDPOINTS: dict[str, float] = {
    "환율(원/$)":       1320.0,   # 원/달러: 2024년 평균 기준
    "수출증가율":           0.0,   # 수출 증가율 0% = 성장 없음
    "기준금리":            3.0,   # 3% = 중립 금리 기준
    "소비자물가(CPI)":     2.5,   # 한은 목표 물가 2%~2.5%
    "수출물가지수":          0.0,   # 전년 대비 0% = 변화 없음
    "수입물가지수":          0.0,   # 전년 대비 0% = 변화 없음
    "원/100엔 환율":      900.0,   # 엔/원 900원 = 기준선
}


def _level_score_single(value: float, label: str) -> float:
    """단일 값의 절대 수준 스코어 (+1.0 or -1.0)."""
    mid = _LEVEL_MIDPOINTS.get(label, 0.0)
    return +1.0 if value >= mid else -1.0


def calculate_prev_period_delta(
    macro_data: dict[str, Any],
    industry_key: str,
) -> dict[str, Any]:
    """
    macro.json의 현재값(value)과 직전값(prev_value)을 비교해
    "직전 데이터 기간 대비 경기 여건 변화"를 즉시 산출한다.

    세션 히스토리 없이 day 1부터 의미 있는 delta 제공.

    방식:
      level_score(value)     = sign(value - midpoint) × direction × weight
      level_score(prev_value) = sign(prev_value - midpoint) × direction × weight
      delta = 현재 레벨스코어 - 이전 레벨스코어

    Returns:
        {
          "curr_level_score": float,   # 현재값 기준 레벨스코어
          "prev_level_score": float,   # 직전값 기준 레벨스코어
          "delta": float,              # 양수=개선, 음수=악화
          "delta_label": str,          # "↑ +0.4 직전 대비 개선" 등
          "changed_indicators": list,  # 방향이 바뀐 지표명 리스트
        }
    """
    profile = get_profile(industry_key)
    weights = profile.get("macro_weights", {})

    curr_total = 0.0
    prev_total = 0.0
    total_weight = 0.0
    changed: list[str] = []

    for label, item in macro_data.items():
        if not isinstance(item, dict) or label not in weights:
            continue
        try:
            curr_val = float(str(item.get("value", "0")).replace(",", "").replace("+", ""))
            prev_raw = item.get("prev_value")
            if prev_raw is None:
                continue
            prev_val = float(str(prev_raw).replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            _log.debug("Failed to parse prev_period_delta values for label '%s': curr=%r, prev=%r", label, item.get("value"), prev_raw)
            continue

        direction = _MACRO_DIRECTION.get(label, +1)
        weight    = weights[label]

        c_score = _level_score_single(curr_val, label) * direction * weight
        p_score = _level_score_single(prev_val, label) * direction * weight

        curr_total   += c_score
        prev_total   += p_score
        total_weight += weight

        # 방향이 바뀐 지표 감지 (레벨 변화)
        if (c_score > 0) != (p_score > 0):
            changed.append(label)

    if total_weight == 0:
        return {"curr_level_score": 0.0, "prev_level_score": 0.0,
                "delta": 0.0, "delta_label": "—", "changed_indicators": []}

    curr_norm = round(max(-3.0, min(3.0, curr_total / total_weight * 3.0)), 1)
    prev_norm = round(max(-3.0, min(3.0, prev_total / total_weight * 3.0)), 1)
    delta     = round(curr_norm - prev_norm, 1)

    if delta > 0:
        delta_label = f"↑ +{delta} 직전 대비 개선"
    elif delta < 0:
        delta_label = f"↓ {delta} 직전 대비 악화"
    else:
        delta_label = "→ ±0.0 직전과 동일"

    return {
        "curr_level_score":  curr_norm,
        "prev_level_score":  prev_norm,
        "delta":             delta,
        "delta_label":       delta_label,
        "changed_indicators": changed,
        "has_prev_data":     True,
    }


# ── Score Delta & 히스토리 (세션 기반) ───────────────────────────────────────
#
# score_history.json 구조:
# {
#   "반도체": [
#     {"ts": "2026-03-09T12:27:00", "score": 2.6},
#     {"ts": "2026-03-02T10:10:00", "score": 2.1},
#     ...  (최대 MAX_HISTORY 개, 최신순)
#   ]
# }
#
# 세션 기반: "업데이트" 클릭 or 앱 새로고침 시마다 저장.
# 동일 날짜(YYYY-MM-DD) 내 중복 저장은 덮어쓰기(스코어 갱신).
# ─────────────────────────────────────────────────────────────────────────────

_SCORE_HISTORY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "score_history.json"
)
_MAX_HISTORY = 8   # 스파크라인용 최대 보관 개수


def _load_score_history() -> dict[str, list]:
    try:
        with open(_SCORE_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 구형 format(dict of dict) → 신형 format(dict of list) 자동 마이그레이션
        migrated: dict[str, list] = {}
        for ind, val in data.items():
            if isinstance(val, list):
                migrated[ind] = val
            elif isinstance(val, dict):
                # 구형: {"2026-03": 2.6, ...}  → 날짜 정렬 후 list 변환
                entries = sorted(val.items(), reverse=True)
                migrated[ind] = [
                    {"ts": f"{k}-01T00:00:00", "score": v}
                    for k, v in entries
                ]
        return migrated
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_score_history(history: dict[str, list]) -> None:
    os.makedirs(os.path.dirname(_SCORE_HISTORY_PATH), exist_ok=True)
    with open(_SCORE_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def update_and_get_score_delta(
    industry_key: str,
    current_score: float,
) -> dict[str, Any]:
    """
    현재 스코어를 score_history.json에 세션 기록으로 저장하고
    직전 세션 대비 delta 및 스파크라인용 히스토리를 반환한다.

    동일 날짜(YYYY-MM-DD) 내에는 가장 최신 값으로 덮어쓴다.

    Returns:
        {
          "delta": float | None,          # 직전 값 대비 변화량
          "delta_label": str,             # "↑ +0.7 (이전 대비 개선)" 등
          "prev_score": float | None,
          "prev_ts": str | None,          # ISO timestamp of previous entry
          "days_ago": int | None,         # 직전 기록이 며칠 전인지
          "history": list[float],         # 스파크라인용 [oldest→newest] 최대 8개
        }
    """
    now = datetime.now()
    now_date = now.strftime("%Y-%m-%d")
    now_ts   = now.isoformat(timespec="seconds")

    history = _load_score_history()
    entries: list[dict] = history.get(industry_key, [])

    # 동일 날짜 기존 항목 덮어쓰기 or 새 항목 추가
    today_idx = next(
        (i for i, e in enumerate(entries) if e["ts"][:10] == now_date), None
    )
    if today_idx is not None:
        entries[today_idx] = {"ts": now_ts, "score": current_score}
    else:
        entries.insert(0, {"ts": now_ts, "score": current_score})

    # 최신순 정렬 후 최대 MAX_HISTORY 유지
    entries.sort(key=lambda e: e["ts"], reverse=True)
    entries = entries[:_MAX_HISTORY]
    history[industry_key] = entries
    _save_score_history(history)

    # 직전 값 (오늘 제외 첫 번째)
    prev_entries = [e for e in entries if e["ts"][:10] != now_date]
    if prev_entries:
        prev = prev_entries[0]
        prev_score = prev["score"]
        prev_ts    = prev["ts"]
        try:
            prev_dt  = datetime.fromisoformat(prev_ts[:19])
            days_ago = (now - prev_dt).days
        except ValueError:
            days_ago = None
        delta = round(current_score - prev_score, 1)
        if delta > 0:
            ago_str = f"{days_ago}일 전" if days_ago and days_ago > 0 else "이전"
            delta_label = f"↑ +{delta}  {ago_str} 대비 개선"
        elif delta < 0:
            ago_str = f"{days_ago}일 전" if days_ago and days_ago > 0 else "이전"
            delta_label = f"↓ {delta}  {ago_str} 대비 악화"
        else:
            delta_label = "→ ±0.0  변동 없음"
    else:
        prev_score = None
        prev_ts    = None
        days_ago   = None
        delta      = None
        delta_label = "첫 번째 기록"

    # 스파크라인용: oldest→newest 순서로
    spark = [e["score"] for e in reversed(entries)]

    return {
        "delta":       delta,
        "delta_label": delta_label,
        "prev_score":  prev_score,
        "prev_ts":     prev_ts,
        "days_ago":    days_ago,
        "history":     spark,
    }


# ════════════════════════════════════════════════════════════════════════════
# Article Intelligence v2 — score_article_v2 / batch_score_and_rank
# ════════════════════════════════════════════════════════════════════════════

def _detect_policy_type(text: str) -> str | None:
    """기사 텍스트에서 정책 유형을 감지. 해당 없으면 None."""
    best_type = None
    best_score = 0.0
    for ptype, (kws, pts) in _POLICY_KW.items():
        if any(kw in text for kw in kws):
            if pts > best_score:
                best_score = pts
                best_type = ptype
    return best_type


def _macro_alignment_check(
    article: dict, macro_data: dict, industry_key: str
) -> str:
    """기사 톤 vs 거시 방향 비교. 'aligned'/'neutral'/'contrary' 반환."""
    from core.macro_utils import _RISK_KW, _OPP_KW

    text = article.get("title", "") + " " + article.get("body", "") + " " + article.get("body_text", "")

    risk_count = sum(1 for kw in _RISK_KW if kw in text)
    opp_count = sum(1 for kw in _OPP_KW if kw in text)

    if risk_count == 0 and opp_count == 0:
        return "neutral"

    article_tone = "negative" if risk_count > opp_count else (
        "positive" if opp_count > risk_count else "neutral"
    )

    if article_tone == "neutral":
        return "neutral"

    # 거시 방향 판단: macro_data의 trend 기반
    profile = get_profile(industry_key)
    weights = profile.get("macro_weights", {})
    positive_trends = 0
    negative_trends = 0
    for label, item in macro_data.items():
        if not isinstance(item, dict) or label not in weights:
            continue
        trend = item.get("trend", "→")
        direction = _MACRO_DIRECTION.get(label, +1)
        if trend == "▲":
            if direction > 0:
                positive_trends += 1
            else:
                negative_trends += 1
        elif trend == "▼":
            if direction < 0:
                positive_trends += 1
            else:
                negative_trends += 1

    if positive_trends == 0 and negative_trends == 0:
        return "neutral"

    macro_tone = "positive" if positive_trends > negative_trends else (
        "negative" if negative_trends > positive_trends else "neutral"
    )

    if macro_tone == "neutral":
        return "neutral"
    if article_tone == macro_tone:
        return "aligned"
    return "contrary"


def score_article_v2(
    article: dict,
    industry_key: str,
    macro_data: dict | None = None,
    signal: dict | None = None,
) -> dict:
    """
    v2 스코어링. 내부적으로 기존 score_article() 호출.
    Returns: {
        "score": 1-5, "raw_score": float,
        "confidence": 0.0-1.0,
        "factors": [{"name": str, "score": float, "max": float}],
        "policy_type": str|None,
        "macro_alignment": "aligned"|"neutral"|"contrary"
    }
    """
    text = article.get("title", "")
    text += " " + article.get("body", "")
    text += " " + article.get("body_text", "")
    text += " " + article.get("summary_3lines", "")

    a = _keyword_score(text, industry_key)
    b = _macro_score(text, macro_data, industry_key)
    c = _policy_score(text)
    d = _urgency_score(text)

    raw = a + b + c + d
    stars = _score_to_stars(raw)

    # confidence 결정
    if macro_data and signal:
        confidence = 0.9
    elif macro_data:
        confidence = 0.8
    else:
        confidence = 0.6

    factors = [
        {"name": "keyword", "score": a, "max": 30.0},
        {"name": "macro", "score": b, "max": 30.0},
        {"name": "policy", "score": c, "max": 20.0},
        {"name": "urgency", "score": d, "max": 20.0},
    ]

    policy_type = _detect_policy_type(text)

    if macro_data:
        alignment = _macro_alignment_check(article, macro_data, industry_key)
    else:
        alignment = "neutral"

    return {
        "score": stars,
        "raw_score": raw,
        "confidence": confidence,
        "factors": factors,
        "policy_type": policy_type,
        "macro_alignment": alignment,
    }


def batch_score_and_rank(
    articles: list[dict],
    industry_key: str,
    macro_data: dict | None = None,
) -> list[dict]:
    """일괄 스코어링 + rank(1부터) + percentile(0-1) 추가. score 내림차순 정렬."""
    if not articles:
        return []

    results = []
    for art in articles:
        scored = score_article_v2(art, industry_key, macro_data)
        entry = copy.copy(art)
        entry.update(scored)
        results.append(entry)

    # score 내림차순 정렬
    results.sort(key=lambda x: (-x["score"], -x["raw_score"]))

    n = len(results)
    for i, item in enumerate(results):
        item["rank"] = i + 1
        item["percentile"] = round(1.0 - (i / n), 4) if n > 1 else 1.0

    return results
