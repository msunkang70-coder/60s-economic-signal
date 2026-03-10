"""
core/kita_source.py
KITA(한국무역협회) 수출입 통계 데이터 연동

★ 산업별 수출 동향 데이터를 KITA RSS/HTML에서 수집하거나,
  data/kita_fallback.json 캐시에서 로드한다.

주요 함수:
  - get_industry_hs_code(industry_key) → HS 코드 반환
  - fetch_kita_export_trend(industry_key) → 수출 동향 dict 반환
"""

import json
import os
import re
from datetime import datetime, timedelta

try:
    import feedparser
except ImportError:
    feedparser = None  # type: ignore[assignment]

# ── 산업별 HS 코드 매핑 ──────────────────────────────────────
_HS_CODE_MAP = {
    "반도체": "8542",
    "자동차": "8703",
    "배터리": "8507",
    "조선": "8901",
    "철강": "7208",
    "화학": "2901",
    "소비재": "3304",
    "일반": "ALL",
}

# ── 산업별 KITA 검색 키워드 (RSS/HTML 필터용) ─────────────────
_KITA_KEYWORDS = {
    "반도체": ["반도체", "집적회로", "메모리", "칩"],
    "자동차": ["자동차", "승용차", "차량", "자동차부품"],
    "배터리": ["배터리", "2차전지", "축전지", "리튬"],
    "조선": ["선박", "조선", "해양"],
    "철강": ["철강", "열연", "냉연", "철"],
    "화학": ["화학", "석유화학", "유기화합물"],
    "소비재": ["화장품", "식품", "소비재"],
    "일반": ["수출", "무역", "통상"],
}

# ── KITA RSS 소스 ─────────────────────────────────────────────
_KITA_RSS_URL = "https://www.kita.net/cmmrcInfo/tradeStatistics/rss.do"

# ── 캐시 경로 ─────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FALLBACK_PATH = os.path.join(_BASE_DIR, "data", "kita_fallback.json")
_CACHE_EXPIRY_DAYS = 7


def get_industry_hs_code(industry_key: str) -> str:
    """산업별 HS 코드 반환. 미등록 키는 'ALL'."""
    return _HS_CODE_MAP.get(industry_key, "ALL")


def _load_fallback_cache() -> dict:
    """data/kita_fallback.json에서 캐시된 데이터 로드."""
    if not os.path.exists(_FALLBACK_PATH):
        return {}
    try:
        with open(_FALLBACK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[kita_source] 캐시 로드 실패: {e}")
        return {}


def _save_fallback_cache(data: dict) -> None:
    """data/kita_fallback.json에 캐시 저장."""
    os.makedirs(os.path.dirname(_FALLBACK_PATH), exist_ok=True)
    try:
        # 기존 캐시 로드 후 병합
        existing = _load_fallback_cache()
        existing.update(data)
        with open(_FALLBACK_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[kita_source] 캐시 저장 실패: {e}")


def _is_cache_valid(cache_entry: dict) -> bool:
    """캐시 유효기간(7일) 확인."""
    cached_at = cache_entry.get("cached_at", "")
    if not cached_at:
        return False
    try:
        cached_dt = datetime.strptime(cached_at, "%Y-%m-%d")
        return (datetime.now() - cached_dt).days < _CACHE_EXPIRY_DAYS
    except ValueError:
        return False


def _fetch_from_rss(industry_key: str) -> dict | None:
    """KITA RSS에서 산업 관련 수출 통계 정보 추출 시도."""
    if feedparser is None:
        return None

    keywords = _KITA_KEYWORDS.get(industry_key, _KITA_KEYWORDS["일반"])

    try:
        import urllib.request
        req = urllib.request.Request(
            _KITA_RSS_URL,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        feed = feedparser.parse(raw)
    except Exception as e:
        print(f"[kita_source] KITA RSS 수집 실패: {e}")
        return None

    if not feed.entries:
        print("[kita_source] KITA RSS 항목 없음")
        return None

    # 키워드 매칭되는 기사 탐색
    for entry in feed.entries:
        title = entry.get("title", "")
        summary = entry.get("summary", "")
        text = title + " " + summary

        if not any(kw in text for kw in keywords):
            continue

        # 수출 금액 추출 시도
        amount_match = re.search(r"(\d+[\.,]?\d*)\s*(억\s*달러|만\s*달러|달러)", text)
        yoy_match = re.search(r"([+-]?\d+[\.,]?\d*)\s*%", text)
        period_match = re.search(r"(\d{4})년\s*(\d{1,2})월", text)

        industry_label = _get_industry_label(industry_key)

        result = {
            "industry": industry_label,
            "export_amount": amount_match.group(0).strip() if amount_match else "",
            "yoy_change": f"{yoy_match.group(1)}%" if yoy_match else "",
            "period": f"{period_match.group(1)}년 {period_match.group(2)}월" if period_match else "",
            "top_markets": _extract_markets(text),
            "source": "KITA",
            "title": title,
            "cached_at": datetime.now().strftime("%Y-%m-%d"),
        }

        # 유효한 데이터가 하나라도 있으면 반환
        if result["export_amount"] or result["yoy_change"]:
            return result

    print(f"[kita_source] KITA RSS에서 '{industry_key}' 관련 통계 없음")
    return None


def _extract_markets(text: str) -> list[str]:
    """텍스트에서 주요 수출 시장(국가명) 추출."""
    markets = []
    country_keywords = [
        "미국", "중국", "일본", "베트남", "인도", "EU", "유럽",
        "독일", "영국", "호주", "대만", "홍콩", "싱가포르",
        "인도네시아", "태국", "멕시코", "캐나다", "브라질",
    ]
    for country in country_keywords:
        if country in text and country not in markets:
            markets.append(country)
        if len(markets) >= 3:
            break
    return markets if markets else ["미국", "중국", "베트남"]


def _get_industry_label(industry_key: str) -> str:
    """산업 키에서 레이블 반환."""
    try:
        from core.industry_config import get_profile
        return get_profile(industry_key).get("label", industry_key)
    except ImportError:
        return industry_key


def fetch_kita_export_trend(industry_key: str) -> dict:
    """
    KITA 수출 동향 데이터를 반환한다.

    우선순위:
      1) KITA RSS에서 실시간 수집
      2) 실패 시 data/kita_fallback.json 캐시 (7일 유효)
      3) 캐시도 없으면 빈 데이터 반환

    Returns:
        {
          "industry": "반도체·디스플레이",
          "export_amount": "145억 달러",
          "yoy_change": "+12.3%",
          "period": "2026년 1월",
          "top_markets": ["미국", "중국", "베트남"],
          "source": "KITA"
        }
    """
    # 1) RSS에서 실시간 수집
    result = _fetch_from_rss(industry_key)
    if result:
        print(f"[kita_source] RSS 수집 성공: {industry_key}")
        # 캐시 업데이트
        _save_fallback_cache({industry_key: result})
        return result

    # 2) 캐시 폴백
    cache = _load_fallback_cache()
    cached_entry = cache.get(industry_key)
    if cached_entry and _is_cache_valid(cached_entry):
        print(f"[kita_source] 캐시 사용: {industry_key} (캐시일: {cached_entry.get('cached_at', '?')})")
        return cached_entry

    # 3) 빈 데이터 반환
    print(f"[kita_source] 데이터 없음: {industry_key}")
    return {
        "industry": _get_industry_label(industry_key),
        "export_amount": "",
        "yoy_change": "",
        "period": "",
        "top_markets": [],
        "source": "KITA",
    }
