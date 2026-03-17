"""
daily_live_qa.py -- Daily QA Automation Pipeline
================================================
실행: python daily_live_qa.py
출력: data/daily_qa_report.json

QA 항목:
  1. source_ingestion_count   -- 소스별 수집 건수
  2. junk_filtering_ratio     -- 정크 필터 비율
  3. zero_relevance_ratio     -- relevance_score == 0 비율 (post-filter 기준)
  4. ranking_stability        -- Top 거시지표 신호 안정성 (산업별)
  5. cache_ttl_status         -- summary_cache.json TTL 상태
  6. source_availability      -- RSS 소스 가용성 (HTTP 상태)

Threshold (v2 -- 실측 데이터 기반 튜닝, 2026-03-17):
  WARNING  -- zero_relevance_ratio > 0.75  (실측 baseline ~64-88%, 정상 범위 상정)
  WARNING  -- junk_ratio > 0.15            (실측 baseline ~5%, 3배 초과시 이상)
  CRITICAL -- junk_ratio > 0.25            (실측 5x 초과 = 필터 고장 의심)
  CRITICAL -- PRIMARY/SECONDARY RSS source down
  WARNING  -- cache file age > 6h (TTL 초과)
  WARNING  -- ranking shift >= 3 industries (8개 산업 기준)
  WARNING  -- total ingested articles < 10
  CRITICAL -- total ingested articles < 3

변경 이력:
  v1 (초기): zero_relevance_warn=0.40, junk_warn=0.30, cache_warn=0.50, rank_warn=2
  v2 (2026-03-17): 실측 기반 전면 재조정 -- false positive 제거
    - zero_relevance: 0.40 -> 0.75 (RSS 제목 매칭 특성상 64-88% 정상)
    - junk_warn: 0.30 -> 0.15, junk_critical 신설 0.25
    - cache_expired: 0.50 -> file_age_h 6h 기준으로 방식 변경
    - ranking_shift: 2 -> 3 (macro 갱신시 2개 변동은 정상)
    - source_min_articles_warn: 10 (신규), source_min_articles_critical: 3 (신규)
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from datetime import datetime
from typing import Any

# -- 프로젝트 루트를 sys.path에 추가 --
_ROOT = pathlib.Path(__file__).parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# -- 경로 상수 --
_DATA_DIR = _ROOT / "data"
_SUMMARY_CACHE_PATH = _DATA_DIR / "summary_cache.json"
_QA_REPORT_PATH = _DATA_DIR / "daily_qa_report.json"
_PREV_REPORT_PATH = _DATA_DIR / "daily_qa_report_prev.json"
_MACRO_PATH = _DATA_DIR / "macro.json"

# -- RSS 소스 정의 (extra_sources.py 동기화) --
_RSS_SOURCES = [
    {
        "name": "연합뉴스경제",
        "url": "https://www.yonhapnewstv.co.kr/category/news/economy/feed/",
        "priority": "PRIMARY",
    },
    {
        "name": "매일경제",
        "url": "https://www.mk.co.kr/rss/40300001/",
        "priority": "SECONDARY",
    },
    {
        "name": "한국경제",
        # feeds.hankyung.com 서브도메인 소멸 (2026-03-17 확인)
        # 대체: www.hankyung.com/feed/economy (200 OK, 50건 안정)
        "url": "https://www.hankyung.com/feed/economy",
        "priority": "TERTIARY",
    },
    {
        "name": "KITA RSS",
        "url": "https://www.kita.net/cmmrcInfo/tradeStatistics/rss.do",
        "priority": "SUPPLEMENTARY",
    },
]

# -- 정크 키워드 (app.py / main_content.py 동기화) --
_IRRELEVANT_KW: list[str] = [
    "거버넌스", "투자자 보호", "코스피", "상법", "자사주",
    "지배구조", "주총", "소액주주", "의결권", "코스닥",
    "주가상승", "시가총액", "동네", "로컬", "하이퍼로컬",
    "동네책방", "당근", "카카오", "지역상권", "골목",
    "소상공인 창업", "프랜차이즈", "부동산", "전세대출",
    "서민 주거", "임대차", "청약", "재건축",
    "투자자보호", "주주환원",
]

# -- 경제 관련성 키워드 (macro_utils._ECON_KW 기반 + 산업·에너지 확장) --
# v2: 17개 → 52개 확장. RSS 제목 특성상 산업명/브랜드 키워드 포함 필수.
# 측정 방식: "제목에 1개 이상 포함" 여부 (비율이 아닌 존재 여부 기준).
_ECON_KW: list[str] = [
    # macro_utils 기본 17개
    "성장", "금리", "환율", "수출", "수입", "물가", "소비", "투자",
    "고용", "재정", "경기", "부채", "기업", "가계", "산업", "정책", "무역",
    # 8대 수출 산업
    "반도체", "자동차", "배터리", "조선", "철강", "화학", "소비재", "배터리",
    # 주요 기업/브랜드 (수출 경제 뉴스 핵심)
    "현대", "삼성", "SK", "LG", "포스코", "한화", "두산",
    # 에너지/자원
    "유가", "석유", "원유", "가스", "기름값", "유조선",
    # 통상/무역
    "관세", "FTA", "통상", "수출입", "달러", "원화", "위안",
    # 거시 보완
    "GDP", "CPI", "경상수지", "내수", "국제", "글로벌",
    # 기술/혁신 (수출 관련)
    "AI", "HBM", "엔비디아", "반도체법",
]
# 중복 제거
_ECON_KW = list(dict.fromkeys(_ECON_KW))

# ── Threshold v2 (실측 기반) ──────────────────────────────────────
# 근거 데이터 (2026-03-17 실측, 연합+매일경제 RSS 61건):
#   zero_relevance (확장 KW 기준): 63.9%  → baseline ~65%
#   junk_ratio: 4.9%               → baseline ~5%
#   source availability: 연합 200, 매일 200, 한국경제 0건(RSS 불안정)
_THRESHOLDS = {
    # [1] zero_relevance: RSS 제목 매칭 특성상 60-70%는 정상
    #     75% 초과 = KW 매칭 이상 or 비경제 기사 대거 유입 의심
    "zero_relevance_ratio_warn": 0.75,

    # [2] junk: 실측 baseline ~5%. 15% = 3x 초과 = WARNING
    "junk_ratio_warn": 0.15,
    #     25% = 5x 초과 = 필터 로직 오동작 의심 = CRITICAL
    "junk_ratio_critical": 0.25,

    # [3] cache: TTL은 파일 age로 판단 (6h 초과 = 갱신 미실행)
    "cache_file_age_warn_hours": 6.0,
    #     24h 초과 = pipeline 완전 중단 의심
    "cache_file_age_critical_hours": 24.0,

    # [4] ranking: 8개 산업 중 3개 이상 변동 = WARNING
    #     2개 이하 변동은 macro 갱신 시 정상 범위
    "ranking_shift_warn": 3,

    # [5] source ingestion: 최소 기사 수
    "source_min_articles_warn": 10,       # 10건 미만 = WARNING
    "source_min_articles_critical": 3,    # 3건 미만 = CRITICAL

    # [6] HTTP 타임아웃
    "source_timeout_sec": 10,
}


# ══════════════════════════════════════════════════════════════════
# QA Check 1: Source Ingestion Count
# ══════════════════════════════════════════════════════════════════

def check_source_ingestion() -> dict[str, Any]:
    """RSS 소스별 직접 수집 건수를 측정한다.

    fetch_all_sources는 KDI articles 필요로 standalone 호출 불가.
    QA에서는 feedparser로 RSS를 직접 읽어 측정한다.
    """
    result: dict[str, Any] = {
        "check": "source_ingestion_count",
        "status": "ok",
        "details": {},
        "total": 0,
        "warnings": [],
    }

    try:
        import feedparser
    except ImportError:
        result["status"] = "error"
        result["warnings"].append("feedparser 미설치 -- pip install feedparser")
        return result

    source_counts: dict[str, int] = {}
    all_docs: list[dict] = []
    timeout = _THRESHOLDS["source_timeout_sec"]

    for src in _RSS_SOURCES:
        try:
            feed = feedparser.parse(src["url"])
            count = len(feed.entries)
            source_counts[src["name"]] = count
            for entry in feed.entries:
                all_docs.append({
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "_source": src["name"],
                })
        except Exception as e:
            source_counts[src["name"]] = 0
            result["warnings"].append(f"{src['name']} 수집 실패: {e!r}")

    total = len(all_docs)
    result["total"] = total
    result["details"]["source_counts"] = source_counts
    result["_docs"] = all_docs  # 다른 체크에서 재사용

    warn_min = _THRESHOLDS["source_min_articles_warn"]
    crit_min = _THRESHOLDS["source_min_articles_critical"]

    if total < crit_min:
        result["status"] = "critical"
        result["warnings"].append(
            f"수집 기사 {total}건 < CRITICAL 기준 {crit_min}건 -- 소스 전체 이상"
        )
    elif total < warn_min:
        result["status"] = "warning"
        result["warnings"].append(
            f"수집 기사 {total}건 < WARNING 기준 {warn_min}건 -- 일부 소스 불안정"
        )

    return result


# ══════════════════════════════════════════════════════════════════
# QA Check 2: Junk Filtering Ratio
# ══════════════════════════════════════════════════════════════════

def check_junk_filtering_ratio(docs: list[dict]) -> dict[str, Any]:
    """정크 키워드 매칭 비율을 검사한다.

    Baseline: ~5% (2026-03-17 실측)
    WARNING:  >15% (3x baseline)
    CRITICAL: >25% (5x baseline, 필터 로직 오동작 의심)
    """
    result: dict[str, Any] = {
        "check": "junk_filtering_ratio",
        "status": "ok",
        "total": 0,
        "junk_count": 0,
        "junk_ratio": 0.0,
        "baseline_note": "2026-03-17 실측 baseline ~5%",
        "junk_examples": [],
        "warnings": [],
    }

    total = len(docs)
    result["total"] = total

    if total == 0:
        result["status"] = "warning"
        result["warnings"].append("기사 0건 -- 정크 비율 계산 불가")
        return result

    junk_titles: list[str] = []
    for doc in docs:
        title = doc.get("title", "")
        if any(kw in title for kw in _IRRELEVANT_KW):
            junk_titles.append(title)

    junk_count = len(junk_titles)
    junk_ratio = round(junk_count / total, 3)

    result["junk_count"] = junk_count
    result["junk_ratio"] = junk_ratio
    result["junk_examples"] = junk_titles[:5]

    warn = _THRESHOLDS["junk_ratio_warn"]
    crit = _THRESHOLDS["junk_ratio_critical"]

    if junk_ratio > crit:
        result["status"] = "critical"
        result["warnings"].append(
            f"정크 비율 {junk_ratio:.1%} > CRITICAL {crit:.0%} -- 필터 로직 오동작 의심"
        )
    elif junk_ratio > warn:
        result["status"] = "warning"
        result["warnings"].append(
            f"정크 비율 {junk_ratio:.1%} > WARNING {warn:.0%} (baseline ~5%)"
        )

    return result


# ══════════════════════════════════════════════════════════════════
# QA Check 3: Zero Relevance Ratio
# ══════════════════════════════════════════════════════════════════

def check_zero_relevance_ratio(docs: list[dict]) -> dict[str, Any]:
    """RSS 수집 기사 중 경제 관련성 0인 비율을 검사한다.

    측정 방식: 제목에 _ECON_KW 1개 이상 포함 여부 (비율 기반이 아닌 존재 여부).
    Baseline: 63-88% (2026-03-17 실측, RSS 제목 특성상 정상 범위).
    WARNING:  >75% (비정상적으로 비경제 기사 대거 유입 또는 KW 리스트 손상 의심)

    주의: summary_cache의 relevance_score는 full text 기반이라 다른 지표.
          이 체크는 RSS 유입단 기사 제목 기준 측정.
    """
    result: dict[str, Any] = {
        "check": "zero_relevance_ratio",
        "status": "ok",
        "total": 0,
        "zero_count": 0,
        "zero_ratio": 0.0,
        "baseline_note": "2026-03-17 실측 baseline 64-88% (RSS 제목 매칭 특성)",
        "econ_kw_count": len(_ECON_KW),
        "false_positive_risk": "HIGH if threshold < 0.65",
        "zero_examples": [],
        "warnings": [],
    }

    # 정크 제거 후 측정 (post-filter 기준으로 false positive 감소)
    clean_docs = [d for d in docs if not any(kw in d.get("title", "") for kw in _IRRELEVANT_KW)]
    total = len(clean_docs)
    result["total"] = total
    result["details"] = {"raw_count": len(docs), "post_filter_count": total}

    if total == 0:
        result["status"] = "warning"
        result["warnings"].append("정크 제거 후 기사 0건 -- 측정 불가")
        return result

    zero_titles: list[str] = []
    for doc in clean_docs:
        title = doc.get("title", "")
        # 1개 이상 매칭되면 관련성 있음
        has_econ = any(kw in title for kw in _ECON_KW)
        if not has_econ:
            zero_titles.append(title)

    zero_count = len(zero_titles)
    zero_ratio = round(zero_count / total, 3)

    result["zero_count"] = zero_count
    result["zero_ratio"] = zero_ratio
    result["zero_examples"] = zero_titles[:5]

    warn = _THRESHOLDS["zero_relevance_ratio_warn"]

    if zero_ratio > warn:
        result["status"] = "warning"
        result["warnings"].append(
            f"Zero relevance {zero_ratio:.1%} > WARNING {warn:.0%} "
            f"-- _ECON_KW({len(_ECON_KW)}개) 커버리지 부족 또는 비경제 기사 대거 유입"
        )

    return result


# ══════════════════════════════════════════════════════════════════
# QA Check 4: Ranking Stability
# ══════════════════════════════════════════════════════════════════

def check_ranking_stability() -> dict[str, Any]:
    """macro.json 기반 산업별 Top 거시지표 신호 안정성 검사.

    today_signal 모듈 import 실패시 macro.json 직접 파싱으로 fallback.
    WARNING: 8개 산업 중 3개 이상 Top 신호 변동.
    """
    result: dict[str, Any] = {
        "check": "ranking_stability",
        "status": "ok",
        "industries_checked": 0,
        "unstable_industries": [],
        "current_top1": {},
        "warnings": [],
    }

    if not _MACRO_PATH.exists():
        result["status"] = "warning"
        result["warnings"].append("macro.json 없음 -- ranking 계산 불가")
        return result

    try:
        with open(_MACRO_PATH, "r", encoding="utf-8") as f:
            macro_data = json.load(f)
    except Exception as e:
        result["status"] = "error"
        result["warnings"].append(f"macro.json 로드 실패: {e!r}")
        return result

    # today_signal 시도, 실패시 macro 직접 분석으로 fallback
    current_top1: dict[str, str] = {}

    try:
        from core.today_signal import generate_today_signal
        from core.industry_config import get_industry_list

        industries = [item["key"] for item in get_industry_list()]
        for ind in industries:
            try:
                sig = generate_today_signal(macro_data, ind)
                current_top1[ind] = sig.get("label", "unknown") if sig else "none"
            except Exception:
                current_top1[ind] = "error"

    except ImportError:
        # checklist_rules 등 모듈 누락시 macro 직접 분석 fallback
        _DANGER_SCORE = {"danger": 3, "warning": 2, "caution": 1, "normal": 0}
        _THRESHOLDS_MAP = {
            "환율(원/$)": [(0, 1380, "normal"), (1380, 1450, "caution"),
                          (1450, 1500, "warning"), (1500, 9999, "danger")],
            "수출증가율": [(-9999, -10, "danger"), (-10, 0, "caution"),
                         (0, 15, "normal"), (15, 9999, "caution")],
            "소비자물가(CPI)": [(0, 2.0, "normal"), (2.0, 3.0, "caution"),
                              (3.0, 9999, "danger")],
            "기준금리": [(0, 2.0, "caution"), (2.0, 3.5, "normal"),
                       (3.5, 9999, "warning")],
        }

        def _status(key: str, val: float) -> str:
            for lo, hi, st in _THRESHOLDS_MAP.get(key, []):
                if lo <= val < hi:
                    return st
            return "normal"

        scores: dict[str, float] = {}
        for key, item in macro_data.items():
            if key.startswith("_"):
                continue
            try:
                v = float(str(item.get("value", 0)).replace(",", ""))
                st = _status(key, v)
                scores[key] = _DANGER_SCORE.get(st, 0)
            except (ValueError, TypeError):
                scores[key] = 0

        top_indicator = max(scores, key=lambda k: scores[k]) if scores else "unknown"
        # fallback: 모든 산업에 동일 top 신호 (산업별 구분 불가)
        try:
            from core.industry_config import get_industry_list
            industries = [item["key"] for item in get_industry_list()]
        except Exception:
            industries = ["반도체", "자동차", "배터리", "조선", "철강", "화학", "소비재", "일반"]
        for ind in industries:
            current_top1[ind] = top_indicator

        result["warnings"].append(
            "today_signal 모듈 import 실패 -- macro 직접 분석 fallback 사용"
        )

    result["industries_checked"] = len(current_top1)
    result["current_top1"] = current_top1

    # 이전 리포트와 비교
    if _PREV_REPORT_PATH.exists():
        try:
            with open(_PREV_REPORT_PATH, "r", encoding="utf-8") as f:
                prev = json.load(f)
            prev_top1 = (
                prev.get("checks", {})
                    .get("ranking_stability", {})
                    .get("current_top1", {})
            )
            unstable = [
                {"industry": ind, "prev": prev_top1.get(ind, "?"), "current": curr}
                for ind, curr in current_top1.items()
                if curr != prev_top1.get(ind)
            ]
            result["unstable_industries"] = unstable

            warn_n = _THRESHOLDS["ranking_shift_warn"]
            if len(unstable) >= warn_n:
                result["status"] = "warning"
                result["warnings"].append(
                    f"Top 신호 변동 {len(unstable)}개 산업 >= WARNING {warn_n}개"
                )
        except Exception:
            pass  # 비교 실패는 조용히 무시

    return result


# ══════════════════════════════════════════════════════════════════
# QA Check 5: Cache TTL Status
# ══════════════════════════════════════════════════════════════════

def check_cache_ttl_status() -> dict[str, Any]:
    """summary_cache.json TTL 상태 점검.

    v2: 파일 mtime 기반 age로 판단.
    WARNING:  age > 6h  (갱신 파이프라인 지연 의심)
    CRITICAL: age > 24h (pipeline 완전 중단 의심)

    추가: 구형 str 포맷 캐시 탐지, 4-frame dict 형식 검증.
    """
    result: dict[str, Any] = {
        "check": "cache_ttl_status",
        "status": "ok",
        "total_entries": 0,
        "str_format_count": 0,
        "valid_format_count": 0,
        "cache_age_hours": None,
        "warnings": [],
    }

    if not _SUMMARY_CACHE_PATH.exists():
        result["warnings"].append("summary_cache.json 없음 (첫 실행 전 정상)")
        return result

    age_hours = round((time.time() - _SUMMARY_CACHE_PATH.stat().st_mtime) / 3600, 1)
    result["cache_age_hours"] = age_hours

    warn_h = _THRESHOLDS["cache_file_age_warn_hours"]
    crit_h = _THRESHOLDS["cache_file_age_critical_hours"]

    if age_hours > crit_h:
        result["status"] = "critical"
        result["warnings"].append(
            f"캐시 파일 age {age_hours}h > CRITICAL {crit_h}h -- pipeline 중단 의심"
        )
    elif age_hours > warn_h:
        result["status"] = "warning"
        result["warnings"].append(
            f"캐시 파일 age {age_hours}h > WARNING {warn_h}h -- 갱신 지연"
        )

    try:
        with open(_SUMMARY_CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        result["status"] = "error"
        result["warnings"].append(f"캐시 파싱 실패: {e!r}")
        return result

    if not isinstance(raw, dict):
        result["warnings"].append("캐시 형식 이상 (dict 아님)")
        return result

    total = len(raw)
    result["total_entries"] = total

    str_count = sum(1 for v in raw.values() if isinstance(v, str))
    valid_count = sum(
        1 for v in raw.values()
        if isinstance(v, dict) and isinstance(v.get("summary"), dict)
    )
    result["str_format_count"] = str_count
    result["valid_format_count"] = valid_count

    if str_count > 0 and result["status"] == "ok":
        result["status"] = "warning"
        result["warnings"].append(
            f"구형 str 포맷 캐시 {str_count}건 -- summary_cache.json 재초기화 권장"
        )

    return result


# ══════════════════════════════════════════════════════════════════
# QA Check 6: Source Availability
# ══════════════════════════════════════════════════════════════════

def check_source_availability() -> dict[str, Any]:
    """RSS 소스 가용성을 HTTP GET으로 확인한다.

    CRITICAL: PRIMARY 또는 SECONDARY 소스 응답 실패
    WARNING:  TERTIARY 소스 응답 실패 (연합+매일경제 정상이면 운영 가능)

    실측 (2026-03-17):
      연합뉴스경제: 200 OK, 11건
      매일경제: 200 OK, 50건
      한국경제: feeds.hankyung.com DNS 소멸 -> www.hankyung.com/feed/economy 로 교체 (200 OK, 50건)
    """
    result: dict[str, Any] = {
        "check": "source_availability",
        "status": "ok",
        "sources": [],
        "down_count": 0,
        "warnings": [],
    }

    try:
        import urllib.request
        import urllib.error
    except ImportError:
        result["status"] = "error"
        result["warnings"].append("urllib 사용 불가")
        return result

    timeout = _THRESHOLDS["source_timeout_sec"]
    down_primary = []
    down_tertiary = []

    for src in _RSS_SOURCES:
        sr: dict[str, Any] = {
            "name": src["name"],
            "priority": src["priority"],
            "status_code": None,
            "available": False,
            "response_ms": None,
        }
        t0 = time.time()
        try:
            req = urllib.request.Request(
                src["url"],
                headers={"User-Agent": "Mozilla/5.0 (QABot/2.0)"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                sr["status_code"] = resp.status
                sr["available"] = resp.status == 200
        except urllib.error.HTTPError as e:
            sr["status_code"] = e.code
        except Exception as e:
            sr["status_code"] = 0
            sr["error"] = str(e)[:80]

        sr["response_ms"] = round((time.time() - t0) * 1000)
        result["sources"].append(sr)

        if not sr["available"]:
            if src["priority"] in ("PRIMARY", "SECONDARY"):
                down_primary.append(src["name"])
            else:
                down_tertiary.append(src["name"])

    result["down_count"] = len(down_primary) + len(down_tertiary)

    if down_primary:
        result["status"] = "critical"
        result["warnings"].append(
            f"주요 소스 다운 (PRIMARY/SECONDARY): {', '.join(down_primary)}"
        )
    elif down_tertiary:
        result["status"] = "warning"
        result["warnings"].append(
            f"보조 소스 불안정 (TERTIARY/SUPPLEMENTARY): {', '.join(down_tertiary)}"
        )

    return result


# ══════════════════════════════════════════════════════════════════
# 종합 Health 판정
# ══════════════════════════════════════════════════════════════════

def _compute_overall_health(checks: dict[str, dict]) -> str:
    """critical/error -> Red, warning -> Yellow, else -> Green."""
    statuses = [c.get("status", "ok") for c in checks.values()]
    if "critical" in statuses or "error" in statuses:
        return "Red"
    if "warning" in statuses:
        return "Yellow"
    return "Green"


def _health_emoji(health: str) -> str:
    return {"Green": "🟢", "Yellow": "🟡", "Red": "🔴"}.get(health, "⚪")


# ══════════════════════════════════════════════════════════════════
# 메인 파이프라인
# ══════════════════════════════════════════════════════════════════

def run_daily_qa(verbose: bool = True) -> dict[str, Any]:
    """Daily QA 파이프라인을 실행하고 결과를 반환한다."""
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if verbose:
        print(f"\n{'='*60}")
        print(f"  Daily QA Pipeline [{run_at}]")
        print(f"  Threshold version: v2 (2026-03-17)")
        print(f"{'='*60}")

    # Check 1 먼저 실행해서 docs 공유
    if verbose:
        print("\n[source_ingestion_count] 실행 중...")
    ingestion_result = check_source_ingestion()
    docs: list[dict] = ingestion_result.pop("_docs", [])
    if verbose:
        _print_check_status(ingestion_result)

    checks: dict[str, dict] = {"source_ingestion_count": ingestion_result}

    remaining_steps = [
        ("junk_filtering_ratio",  lambda: check_junk_filtering_ratio(docs)),
        ("zero_relevance_ratio",  lambda: check_zero_relevance_ratio(docs)),
        ("ranking_stability",     lambda: check_ranking_stability()),
        ("cache_ttl_status",      lambda: check_cache_ttl_status()),
        ("source_availability",   lambda: check_source_availability()),
    ]

    for name, fn in remaining_steps:
        if verbose:
            print(f"\n[{name}] 실행 중...")
        try:
            res = fn()
        except Exception as e:
            res = {"check": name, "status": "error", "warnings": [f"예외: {e!r}"]}
        checks[name] = res
        if verbose:
            _print_check_status(res)

    overall_health = _compute_overall_health(checks)
    emoji = _health_emoji(overall_health)

    if verbose:
        print(f"\n{'='*60}")
        print(f"  System Health: [{overall_health}]")
        print(f"{'='*60}\n")

    report: dict[str, Any] = {
        "run_at": run_at,
        "threshold_version": "v2",
        "overall_health": overall_health,
        "health_emoji": emoji,
        "checks": checks,
        "thresholds": _THRESHOLDS,
        "summary": {
            "total_warnings": sum(len(c.get("warnings", [])) for c in checks.values()),
            "critical_count": sum(1 for c in checks.values() if c.get("status") == "critical"),
            "warning_count": sum(1 for c in checks.values() if c.get("status") == "warning"),
        },
    }

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _QA_REPORT_PATH.exists():
        try:
            import shutil
            shutil.copy2(_QA_REPORT_PATH, _PREV_REPORT_PATH)
        except Exception:
            pass

    with open(_QA_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    if verbose:
        print(f"리포트 저장: {_QA_REPORT_PATH}")

    return report


def _print_check_status(result: dict) -> None:
    status = result.get("status", "ok")
    icons = {"ok": "OK", "warning": "WARN", "critical": "CRIT", "error": "ERR"}
    emoji = {"ok": "  [OK]", "warning": "  [!!]", "critical": "  [XX]", "error": "  [??]"}
    print(f"  {emoji.get(status, '  [??]')} status: {icons.get(status, status).upper()}")
    for w in result.get("warnings", []):
        print(f"       {w}")


# ══════════════════════════════════════════════════════════════════
# Streamlit Debug Panel용 공개 API
# ══════════════════════════════════════════════════════════════════

def load_latest_qa_report() -> dict[str, Any] | None:
    """data/daily_qa_report.json을 로드. 없으면 None."""
    if not _QA_REPORT_PATH.exists():
        return None
    try:
        with open(_QA_REPORT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_system_health() -> tuple[str, str]:
    """(health_str, emoji) 반환. 리포트 없으면 ('Unknown', '⚪')."""
    report = load_latest_qa_report()
    if not report:
        return "Unknown", "⚪"
    health = report.get("overall_health", "Unknown")
    return health, report.get("health_emoji", _health_emoji(health))


# ══════════════════════════════════════════════════════════════════
# CLI 진입점
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Daily QA Pipeline v2")
    parser.add_argument("--quiet", action="store_true", help="콘솔 출력 억제")
    args = parser.parse_args()

    report = run_daily_qa(verbose=not args.quiet)
    health = report["overall_health"]
    s = report["summary"]
    print(
        f"\n[결과] {health} | "
        f"CRITICAL {s['critical_count']} | WARNING {s['warning_count']}"
    )
