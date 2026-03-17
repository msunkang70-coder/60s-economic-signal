"""
core/kita_source.py
KITA(한국무역협회) 수출입 통계 데이터 연동

★ 산업별 수출 동향 데이터를 KITA RSS/HTML에서 수집하거나,
  data/kita_fallback.json 캐시에서 로드한다.

주요 함수:
  - get_industry_hs_code(industry_key) → HS 코드 반환
  - fetch_kita_export_trend(industry_key) → 수출 동향 dict 반환
  - fetch_kita_news(industry_key, max_items) → 뉴스 기사 리스트 (V16.1 신설)

V16.1 변경사항:
  - fetch_kita_news(): KITA 뉴스 RSS → KOTRA RSS → korea.kr 순서 fallback 체인
  - _KITA_NEWS_RSS_URLS: 복수 후보 URL 배열
  - _KOTRA_NEWS_RSS_URLS: KOTRA 해외시장뉴스 RSS 후보

V16.3 변경사항:
  - _fetch_kita_html(): KITA 유효 기사 링크 패턴 필터 추가 (tradeNewsDetail/boardDetail 포함),
    nav/junk 링크 스킵, 복수 fallback HTML URL 시도, 최소 제목 길이 15자
  - _fetch_kotra_html(): bbsNttSn= 또는 actionKotraBoardDetail 포함 링크만 허용 (본문 URL 품질 보증),
    KOTRA 목록 URL 정확화 (actionKotraBoardList.do)
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
    "반도체": ["반도체", "집적회로", "메모리", "칩", "디스플레이", "OLED", "HBM", "AI반도체", "파운드리"],
    "자동차": ["자동차", "승용차", "차량", "자동차부품", "전기차", "EV", "완성차", "하이브리드"],
    "배터리": ["배터리", "2차전지", "축전지", "리튬", "양극재", "전기차", "ESS", "NCM", "LFP"],
    "조선": ["선박", "조선", "해양", "LNG선", "컨테이너선", "수주", "해운"],
    "철강": ["철강", "열연", "냉연", "철", "철광석", "강판", "스테인리스", "포스코"],
    "화학": ["화학", "석유화학", "유기화합물", "나프타", "에틸렌", "합성수지", "정밀화학"],
    "소비재": ["화장품", "식품", "소비재", "K-뷰티", "K-푸드", "음료", "생활용품", "패션"],
    "일반": ["수출", "무역", "통상", "관세", "FTA", "수출입", "무역수지", "환율", "물류", "공급망", "통상정책"],
}

# ── KITA RSS 소스 ─────────────────────────────────────────────
_KITA_RSS_URL = "https://www.kita.net/cmmrcInfo/tradeStatistics/rss.do"

# ── V16.2: KITA 뉴스 HTML 목록 페이지 (RSS 포기 → HTML 파서로 전환) ─
# RSS 후보들(kita.net/cmmrcInfo/tradeNews/rss.do 등) 모두 403/빈항목 확인 (2026-03-14)
# [WARN] KITA 403 현황 (2026-03-14 확인):
#   - kita.net은 서버 측 스크래핑 차단(403 Forbidden) 정책 적용
#   - RSS URL, HTML 목록 URL 모두 403 반환 (User-Agent 무관)
#   - 근본 해결책: KITA 공식 API(현재 비공개) 또는 KITA 뉴스레터 구독 필요
#   - 현재 전략: KITA HTML 3개 URL 시도 → 모두 403이면 KOTRA 파이프라인으로 전환
#
# TODO-4: KITA source 전략 결론 (2026-03-16)
# ─────────────────────────────────────────────────────────────────────
# 현황 진단:
#   A. kita.net 기사 RSS/HTML: 전 URL 403 Forbidden (스크래핑 차단)
#      → fetch_kita_news()는 매 실행마다 3회 HTTP 요청 + 실패 → 시간 낭비
#   B. fetch_kita_export_trend(): 수출 통계 RSS - 별도 URL, 현재 정상 작동 유지
#
# 전략 선택: [B] 뉴스 circuit breaker 적용 + 통계 source 유지
#   - _KITA_NEWS_CIRCUIT_OPEN = True 시 fetch_kita_news()를 즉시 [] 반환 (HTTP 시도 없음)
#   - fetch_kita_export_trend()는 영향 없음 (별도 함수)
#   - KITA 403이 해소되거나 공식 API 발급 시 False로 전환
#
# 대안 검토:
#   [A] 기사 source 완전 제거: 코드 삭제 → 복구 어려움, 미채택
#   [C] fallback priority 하향: 이미 최하위 → 추가 효과 없음, 미채택
# ─────────────────────────────────────────────────────────────────────

# TODO-4: True = KITA 뉴스 403 회피 (HTTP 시도 없음), False = 정상 시도 재활성화
_KITA_NEWS_CIRCUIT_OPEN: bool = True
_KITA_NEWS_RSS_URLS: list[str] = [
    "https://www.kita.net/board/totalBoard/boardDetail.do?rss=Y&bbs_type=1",
    "https://www.kita.net/cmmrcInfo/tradeNews/rss.do",
    "https://www.kita.net/cmmrcInfo/tradeStatistics/rss.do",
]
# HTML 파서 대상 URL (RSS 실패 시 fallback) - V16.3: 복수 후보 배열
_KITA_NEWS_HTML_URL = "https://www.kita.net/cmmrcInfo/tradeNews/tradeNewsMain.do"
_KITA_NEWS_HTML_URLS: list[str] = [
    "https://www.kita.net/cmmrcInfo/tradeNews/tradeNewsMain.do",       # 1순위: 무역뉴스 메인
    "https://www.kita.net/board/totalBoard/boardList.do?bbs_type=1",   # 2순위: 게시판 목록
    "https://www.kita.net/cmmrcInfo/tradeStatistics/tradeStatMain.do", # 3순위: 무역통계 메인
]
# V16.3: KITA 유효 기사 링크 패턴 (이 문자열 중 하나라도 포함돼야 기사 URL로 인정)
_KITA_VALID_LINK_PATTERNS: list[str] = [
    "tradeNewsDetail",
    "boardDetail",
    "bbs_no=",
    "nttSn=",
    "seq=",
    "articleSn=",
]
# V16.3: KITA 스킵할 네비게이션/유틸리티 링크 패턴
_KITA_SKIP_PATTERNS: list[str] = [
    "javascript:", "#", "/login", "/member", "/sitemap",
    "/about", "/intro", "/main", "tradeNewsMain.do", "rss.do",
]

# ── V16.2: KOTRA 뉴스 HTML 목록 페이지 (RSS 포기 → HTML 파서로 전환) ─
# RSS 후보들(dream.kotra.or.kr/kotranews/cms/com/atl/BMTNEWS_RSS.xml 등) 모두 404/빈항목 확인
_KOTRA_NEWS_RSS_URLS: list[str] = [
    "https://dream.kotra.or.kr/kotranews/cms/com/atl/BMTNEWS_RSS.xml",
    "https://www.kotra.or.kr/rss/news.rss",
]

# ── V17.1: KOTRA Google News RSS - HTML SPA 전환 대응 guaranteed fallback ─
# KOTRA dream.kotra.or.kr HTML 파서가 onclick=0건으로 실패 시 (2026-03-15 확인)
# 원인: Type A = KOTRA HTML 구조 변경 (SPA 전환 또는 onclick 함수명 변경)
# 대응: Google News site:dream.kotra.or.kr RSS → KOTRA 기사 URL + 요약 수집
_KOTRA_GNEWS_QUERIES: dict[str, str] = {
    "반도체": "site:dream.kotra.or.kr 반도체 수출",
    "자동차": "site:dream.kotra.or.kr 자동차 수출",
    "배터리": "site:dream.kotra.or.kr 배터리 2차전지 수출",
    "화학":   "site:dream.kotra.or.kr 화학 석유화학 수출",
    "소비재": "site:dream.kotra.or.kr K뷰티 OR 화장품 OR K푸드 OR 식품수출",
    "조선":   "site:dream.kotra.or.kr 조선 수주 LNG선",
    "철강":   "site:dream.kotra.or.kr 철강 수출",
    "일반":   "site:dream.kotra.or.kr 해외시장뉴스 수출",
}
# HTML 파서 대상 URL - V16.3: 정확한 목록 URL로 교체
_KOTRA_NEWS_HTML_URL = "https://dream.kotra.or.kr/kotranews/cms/news/actionKotraBoardDetail.do?SITE_NO=3&MENU_ID=180&CONTENTS_NO=1&bbsGbn=322&bbsSn=322"
_KOTRA_NEWS_LIST_URL = "https://dream.kotra.or.kr/kotranews/cms/com/actionBbsNNewsView.do?SITE_NO=3&MENU_ID=180&CONTENTS_NO=1&bbsGbn=322&bbsSn=322&pageIndex=1"
# V16.3: KOTRA 유효 기사 링크 패턴 (본문 상세 URL 판별)
_KOTRA_VALID_LINK_PATTERNS: list[str] = [
    "bbsNttSn=",            # 기사 고유번호 파라미터 → 상세 본문 URL
    "actionKotraBoardDetail",   # KOTRA 게시판 상세 액션
]

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


# ── V16.2: 뉴스 기사 수집용 공통 헬퍼 ───────────────────────────

import hashlib as _hashlib


def _make_doc_id(url: str) -> str:
    """URL 기반 고유 ID 생성 (kita_source 내부용)."""
    h = _hashlib.md5(url.encode()).hexdigest()[:8]
    return f"kita_{h}"


def _parse_date(entry) -> str:
    """RSS entry 또는 dict에서 YYYYMM 형식 추출."""
    if isinstance(entry, dict):
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed:
            try:
                return f"{parsed.tm_year:04d}{parsed.tm_mon:02d}"
            except (AttributeError, TypeError):
                pass
        published = entry.get("published", "") or entry.get("updated", "")
    else:
        published = str(entry)
    m = re.search(r"(\d{4})-?(\d{2})", published)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return datetime.now().strftime("%Y%m")


def _parse_sort_key(entry) -> str:
    """RSS entry에서 정렬용 날짜 문자열 추출."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return f"{parsed.tm_year:04d}-{parsed.tm_mon:02d}-{parsed.tm_mday:02d}"
        except (AttributeError, TypeError):
            pass
    published = entry.get("published", "") or entry.get("updated", "")
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", published)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return "0000-00-00"


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


# ── V16.1: KITA/KOTRA 뉴스 기사 수집 ─────────────────────────────


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def _get_requests():
    """requests 모듈 로드 헬퍼. ImportError 시 None 반환."""
    try:
        import requests as _r
        import urllib3 as _u3
        _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
        return _r
    except ImportError:
        return None


def _fetch_news_from_rss_list(
    rss_urls: list[str],
    source_label: str,
    keywords: list[str],
    max_items: int = 5,
) -> list[dict]:
    """
    V16.2: requests 기반 RSS 수집 (urllib → requests로 전환, WinError 10054 해결).
    주어진 RSS URL 목록을 순서대로 시도하여 뉴스 기사 수집.
    성공 시(기사 1건 이상) 즉시 반환. 모두 실패 시 빈 목록.
    """
    if feedparser is None:
        return []

    import re as _re
    _req = _get_requests()
    _RSS_HEADERS = {
        **_BROWSER_HEADERS,
        "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    }

    for url in rss_urls:
        try:
            if _req:
                resp = _req.get(url, headers=_RSS_HEADERS, timeout=8, verify=False)
                resp.raise_for_status()
                raw = resp.content
            else:
                import urllib.request as _ureq
                req = _ureq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with _ureq.urlopen(req, timeout=8) as r:
                    raw = r.read()
            feed = feedparser.parse(raw)
            if not feed.entries:
                print(f"[kita_source] {source_label} RSS 항목 없음: {url}")
                continue
            print(f"[kita_source] {source_label} RSS 수집 성공: {url} ({len(feed.entries)}건)")
        except Exception as e:
            print(f"[kita_source] {source_label} RSS 실패: {url} - {type(e).__name__}: {e}")
            continue

        try:
            sorted_entries = sorted(
                feed.entries, key=lambda e: _parse_sort_key(e), reverse=True,
            )
        except Exception:
            sorted_entries = feed.entries

        articles: list[dict] = []
        for entry in sorted_entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue

            if keywords:
                text = title + " " + entry.get("summary", "")
                if not any(kw in text for kw in keywords):
                    continue

            summary = entry.get("summary", "").strip()
            summary = _re.sub(r"<[^>]+>", "", summary).strip()
            published = entry.get("published", "") or entry.get("updated", "")

            articles.append({
                "doc_id": _make_doc_id(link),
                "title": title,
                "summary": summary[:300] if summary else "",
                "url": link,
                "link": link,
                "published": published,
                "issue_yyyymm": _parse_date(entry),
                "category": source_label,
                "date": published,
                "source": source_label,
            })

            if len(articles) >= max_items:
                break

        if articles:
            return articles

        print(f"[kita_source] {source_label} RSS 키워드 매칭 없음: {url}")

    return []


def _fetch_kita_html(keywords: list[str], max_items: int = 5) -> list[dict]:
    """
    V16.3: KITA 무역뉴스 목록 HTML 파서 (개선).
    복수 fallback URL 순차 시도 + 유효 기사 링크 패턴 필터 적용.

    V16.3 개선:
      - _KITA_NEWS_HTML_URLS: 3개 후보 URL 순차 시도
      - _KITA_VALID_LINK_PATTERNS: tradeNewsDetail/boardDetail 등 기사 URL 패턴 필터
      - _KITA_SKIP_PATTERNS: nav/junk 링크 완전 배제
      - 최소 제목 길이 15자 (nav 텍스트 필터)
    """
    _req = _get_requests()
    if not _req:
        return []

    import re as _re
    from datetime import datetime as _dt
    _now_ym = _dt.now().strftime("%Y%m")

    try:
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        print("[kita_source] BeautifulSoup 미설치 - pip install beautifulsoup4")
        return []

    def _is_valid_kita_link(href: str) -> bool:
        """V16.3: KITA 유효 기사 URL 판별 - _KITA_VALID_LINK_PATTERNS 중 하나 포함"""
        return any(pat in href for pat in _KITA_VALID_LINK_PATTERNS)

    def _is_skip_kita_link(href: str) -> bool:
        """V16.3: nav/유틸리티 링크 스킵 판별"""
        return any(pat in href for pat in _KITA_SKIP_PATTERNS)

    # V16.3: 복수 후보 URL 순차 시도
    for _html_url in _KITA_NEWS_HTML_URLS:
        try:
            resp = _req.get(
                _html_url,
                headers={**_BROWSER_HEADERS, "Referer": "https://www.kita.net/"},
                timeout=10,
                verify=False,
            )
            resp.raise_for_status()
            raw = resp.text
            if len(raw) < 500:
                print(f"[kita_source] KITA HTML 응답 너무 짧음: {len(raw)}자 ({_html_url[:60]})")
                continue
            print(f"[kita_source] KITA HTML 수신: {len(raw)}자 ({_html_url[:60]})")
        except Exception as e:
            print(f"[kita_source] KITA HTML 수집 실패: {type(e).__name__}: {e} ({_html_url[:60]})")
            continue

        try:
            soup = _BS(raw, "html.parser")
            articles: list[dict] = []

            # KITA 뉴스 목록 구조 탐지
            candidates: list = []
            for sel in [
                "ul.board_list li", "ul.news_list li", ".board_list li",
                ".news-list li", "table.board_list tr", "tbody tr",
                ".article-list li", "li.item",
            ]:
                found = soup.select(sel)
                if found:
                    candidates = found
                    print(f"[kita_source] KITA HTML 셀렉터 '{sel}' → {len(found)}건")
                    break

            if not candidates:
                # 범용 fallback: 기사 링크 패턴 포함 a 태그만
                all_links = soup.find_all("a", href=True)
                candidates = [a for a in all_links if _is_valid_kita_link(a.get("href", ""))]
                print(f"[kita_source] KITA HTML 유효링크 패턴 fallback → {len(candidates)}개")

            for item in candidates:
                a_tag = item.find("a", href=True) if item.name != "a" else item
                if not a_tag:
                    continue
                title = a_tag.get_text(strip=True)
                # V16.3: 최소 제목 길이 15자 (nav 텍스트 필터)
                if not title or len(title) < 15:
                    continue
                href = a_tag.get("href", "")
                if not href:
                    continue
                # V16.3: nav/junk 스킵
                if _is_skip_kita_link(href):
                    continue
                if not href.startswith("http"):
                    href = "https://www.kita.net" + href if href.startswith("/") else _html_url

                # V16.3: 유효 기사 URL 필터 (기사 상세 패턴 포함 여부)
                # 단, 범용 fallback에서 이미 필터됐으므로 candidates가 list<li>인 경우만 추가 확인
                if candidates and candidates[0].name != "a":
                    if not _is_valid_kita_link(href):
                        continue

                # 키워드 필터
                if keywords and not any(kw in title for kw in keywords):
                    continue

                # 날짜 추출 시도
                date_str = ""
                date_tag = item.find(class_=_re.compile(r"date|time|regdate", _re.I)) if item.name != "a" else None
                if date_tag:
                    date_str = date_tag.get_text(strip=True)
                m = _re.search(r"(\d{4})[.\-/](\d{2})", date_str or "")
                issue_ym = f"{m.group(1)}{m.group(2)}" if m else _now_ym

                articles.append({
                    "doc_id": _make_doc_id(href),
                    "title": title,
                    "summary": title[:200],
                    "url": href,
                    "link": href,
                    "published": date_str or _dt.now().strftime("%Y-%m-%d"),
                    "issue_yyyymm": issue_ym,
                    "category": "KITA",
                    "date": date_str or _dt.now().strftime("%Y-%m-%d"),
                    "source": "KITA",
                })

                if len(articles) >= max_items:
                    break

            if articles:
                print(f"[kita_source] KITA HTML 파서: {len(articles)}건 수집 ({_html_url[:60]})")
                return articles

            print(f"[kita_source] KITA HTML 파서: 키워드/링크 매칭 없음 ({_html_url[:60]})")
        except Exception as e:
            print(f"[kita_source] KITA HTML 파싱 실패: {type(e).__name__}: {e}")

    print("[kita_source] KITA HTML: 모든 후보 URL 실패")
    return []


def _build_kotra_detail_url(bbs_ntt_sn: str) -> str:
    """KOTRA 기사 상세 URL 구성 (bbsNttSn으로 직접 접근)."""
    return (
        "https://dream.kotra.or.kr/kotranews/cms/news/actionKotraBoardDetail.do"
        f"?SITE_NO=3&MENU_ID=180&CONTENTS_NO=1&bbsGbn=322&bbsSn=322&bbsNttSn={bbs_ntt_sn}"
    )


def _extract_kotra_bbs_ntt_sn(el_or_text: object) -> str:
    """
    V16.3: KOTRA 기사 목록 요소에서 bbsNttSn 추출.

    KOTRA dream.kotra.or.kr는 기사 링크를 href가 아닌 onclick에 담음:
      onclick="fn_detail(this, '322', '123456')"  → bbsNttSn=123456
      onclick="fn_detail(322, 123456)"            → bbsNttSn=123456
      onclick="goView('123456')"                  → bbsNttSn=123456
      onclick="javascript:goDetail(123456)"       → bbsNttSn=123456
    data-seq, data-ntt-sn, data-id 속성도 탐색.
    """
    import re as _re

    def _find_ids(text: str) -> list[str]:
        """문자열에서 4자리 이상 숫자 ID 추출."""
        return _re.findall(r"\b(\d{4,})\b", text)

    # BeautifulSoup Tag인 경우
    if hasattr(el_or_text, "get"):
        tag = el_or_text
        # 1순위: onclick 속성
        oc = tag.get("onclick", "") or ""
        ids = _find_ids(oc)
        if ids:
            return ids[-1]  # 마지막 숫자 = bbsNttSn (fn_detail의 두 번째 인자)
        # 2순위: data-* 속성
        for attr in ["data-ntt-sn", "data-sn", "data-seq", "data-id", "data-no", "data-idx"]:
            val = tag.get(attr, "")
            if val and _re.match(r"^\d{4,}$", str(val)):
                return str(val)
    # 문자열인 경우
    elif isinstance(el_or_text, str):
        ids = _find_ids(el_or_text)
        if ids:
            return ids[-1]

    return ""


def _trace(msg: str) -> None:
    """V17.4 검증 전용 파일 트레이스 로거 - data/debug_trace.log 에 기록."""
    import os as _os2, datetime as _dt2
    try:
        _log_path = _os2.path.join(
            _os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__))),
            "data", "debug_trace.log"
        )
        ts = _dt2.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with open(_log_path, "a", encoding="utf-8") as _lf:
            _lf.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _try_resolve_gnews_url(gnews_url: str, timeout: int = 5) -> str | None:
    """
    V17.4: Google News 래퍼 URL → 실제 원문 URL 해소 시도.

    Google News RSS <link>는 보통 JavaScript 기반 리다이렉트지만,
    일부 경우 HTTP 리다이렉트 또는 HTML 내 URL 추출이 가능하다.

    해소 우선순위:
      1) HTTP 리다이렉트 후 resp.url 이 news.google.com 이 아닌 경우 → 즉시 반환
      2) HTML 내 window.location / window.location.href JS 리다이렉트 URL 추출
      3) <a href> 내 KOTRA URL (dream.kotra.or.kr 포함) 추출
      4) <meta http-equiv="refresh"> content 내 URL 추출

    반환:
      str  - 해소된 실제 URL (news.google.com 이 아닌 URL)
      None - 해소 실패 (gnews_url 유지 권고)
    """
    _req = _get_requests()
    if not _req:
        return None
    try:
        import re as _re2
        resp = _req.get(
            gnews_url,
            headers={**_BROWSER_HEADERS, "Accept": "text/html,*/*;q=0.8"},
            timeout=timeout,
            allow_redirects=True,
            verify=False,
        )
        # 1) HTTP 리다이렉트 성공 (resp.url 이 원문 URL로 변경된 경우)
        if "news.google.com" not in resp.url:
            return resp.url

        # 2) HTML 내 JavaScript window.location 파싱
        html = resp.text
        m_js = _re2.search(
            r'window\.location(?:\.href)?\s*=\s*["\']([^"\']{20,})["\']',
            html,
        )
        if m_js and "news.google.com" not in m_js.group(1):
            return m_js.group(1)

        # 3) KOTRA URL 직접 탐색 (<a href="...">)
        m_kotra = _re2.search(
            r'href=["\']([^"\']*kotra\.or\.kr[^"\']{10,})["\']',
            html,
        )
        if m_kotra:
            return m_kotra.group(1)

        # 4) <meta http-equiv="refresh"> URL
        m_meta = _re2.search(
            r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^\s;>"\']{20,})',
            html,
            _re2.IGNORECASE,
        )
        if m_meta and "news.google.com" not in m_meta.group(1):
            return m_meta.group(1).strip("\"'")

    except Exception as _e:
        print(f"[kita_source] gnews URL 해소 오류: {type(_e).__name__}: {_e}")

    return None  # 해소 실패


# ── P5: KOTRA URL 복원 성공률 집계 ─────────────────────────────────────────
_kotra_restore_stats: dict = {
    "total": 0,       # Google News KOTRA 기사 총 처리 수
    "success": 0,     # 실제 KOTRA URL 복원 성공 (V17.4 + V17.5/V17.6 캐시)
    "cache_hit": 0,   # kotra_pnttsn_cache.json HIT
    "gnews_ok": 0,    # _try_resolve_gnews_url() 성공
    "title_ok": 0,    # _try_find_kotra_url_by_title() 성공
    "failed": 0,      # 전체 실패 → 스니펫 모드
}


def get_kotra_restore_stats() -> dict:
    """P5: KOTRA URL 복원 통계 반환 (현재 세션 누적)."""
    s = _kotra_restore_stats
    rate = (s["success"] / s["total"] * 100) if s["total"] else 0.0
    return {**s, "restore_rate_pct": round(rate, 1)}


# ── V17.6 KOTRA 목록 탐색 대상 URL ────────────────────────────────────────
# V17.6 분석 결과:
#   - SPA 페이지(actionBbsNNewsView.do, actionKotraBoardList.do)는 기사 목록 없음 (JS 렌더링)
#   - actionKotraMainSearch.do → 404 오류 페이지 (잘못된 URL)
# V17.6 전략: RSS/sitemap 후보 탐색으로 전환
# 우선순위 1: kotra_pnttsn_cache.json 직접 조회 (함수 내부에서 처리)
# 우선순위 2: KOTRA RSS/sitemap URL (아래 목록)
_KOTRA_LIST_SEARCH_URLS: list[dict] = [
    # KOTRA 해외경제정보드림 hotclip RSS (추정 URL - BMTNEWS RSS 패턴 기반)
    {
        "url": "https://dream.kotra.or.kr/kotranews/cms/com/atl/HOTCLIP_RSS.xml",
        "type": "rss_hotclip",
    },
    # KOTRA 사이트맵 (기사 URL 목록 포함 가능)
    {
        "url": "https://dream.kotra.or.kr/sitemap.xml",
        "type": "sitemap",
    },
    # KOTRA 해외시장뉴스 RSS (기존 - 404지만 일단 유지)
    {
        "url": "https://dream.kotra.or.kr/kotranews/cms/com/atl/BMTNEWS_RSS.xml",
        "type": "rss_bmtnews",
    },
    # KOTRA 전체 검색 (수정된 URL 패턴 - sSearchVal 파라미터)
    {
        "url": "https://dream.kotra.or.kr/kotranews/cms/com/actionKotraSearch.do"
               "?sSearchVal={keyword}&pageIndex=1&pagePerCnt=10",
        "type": "search",
    },
]


def _try_find_kotra_url_by_title(title: str, timeout: int = 10) -> str | None:
    """
    V17.5: KOTRA 목록 페이지에서 title 매칭으로 원문 detail URL 복원.

    _try_resolve_gnews_url() 실패 후 fallback.
    여러 KOTRA 엔드포인트(해외경제정보드림/해외시장뉴스/전체검색)를 순차 탐색.

    지원 URL 패턴:
      pNttSn=XXXXXX  → 해외경제정보드림 detail URL
      bbsNttSn=XXXX  → 해외시장뉴스 detail URL
      onclick 내 ID  → fn_detail() / goView() 등

    반환:
      성공: dream.kotra.or.kr/... detail URL 문자열
      실패: None
    """
    _req = _get_requests()
    if not _req:
        return None

    import re as _re3
    from difflib import SequenceMatcher as _SM

    # P5: 제목 정규화 강화
    # 1) 출처 접미사 제거: "- 해외경제정보드림", "- KOTRA 해외시장뉴스" 등
    _title_no_src = _re3.sub(
        r'\s*[-–|]\s*(해외경제정보드림|해외시장뉴스|KOTRA\s*해외시장뉴스|코트라|KOTRA)\s*$',
        '', title
    ).strip()
    # 2) 꺾쇠/대괄호 태그 제거: "[미국 경제통상리포트]" → ""
    _title_no_tag = _re3.sub(r'\[[^\]]{1,30}\]\s*', '', _title_no_src).strip()
    # 3) 특수문자 → 공백, 연속공백 축약
    _title_norm = _re3.sub(r"\s+", " ", _re3.sub(r"[^\w\s]", " ", _title_no_tag)).strip()
    _title_short = _title_no_src[:60]   # 비교용 단축 제목 (접미사만 제거)

    # ── 제목 유사도 계산 헬퍼 ──────────────────────────────────────────
    # TODO-1: SequenceMatcher + Token Jaccard 혼합 (Korean-friendly)
    _KO_STOPWORDS = frozenset({
        "의", "에", "는", "이", "가", "을", "를", "과", "와", "에서", "으로", "로",
        "한", "및", "등", "대한", "통한", "위한", "관련", "대해", "대해서",
        "하는", "하여", "하고", "위해", "통해", "따른", "따라", "대한",
    })

    def _sim(a: str, b: str) -> float:
        _a = _re3.sub(r"[^\w\s]", " ", a).strip()
        _b = _re3.sub(r"[^\w\s]", " ", b).strip()
        # SequenceMatcher (character n-gram level)
        _seq = _SM(None, _a[:80], _b[:80]).ratio()
        # Token Jaccard overlap (의미 단어 중심)
        _ta = set(t for t in _a.split() if len(t) >= 2 and t not in _KO_STOPWORDS)
        _tb = set(t for t in _b.split() if len(t) >= 2 and t not in _KO_STOPWORDS)
        if _ta and _tb:
            _inter = len(_ta & _tb)
            _union = len(_ta | _tb)
            _jaccard = _inter / _union if _union > 0 else 0.0
            # 가중 평균: seq 40% + jaccard 60% (한국어 단어 단위 일치가 더 신뢰성 높음)
            return 0.4 * _seq + 0.6 * _jaccard
        return _seq

    def _token_overlap(a: str, b: str) -> float:
        """쿼리 제목의 핵심 토큰이 후보에 몇 % 포함되는지 (recall 기준)"""
        _a = _re3.sub(r"[^\w\s]", " ", a)
        _b = _re3.sub(r"[^\w\s]", " ", b)
        _ta = set(t for t in _a.split() if len(t) >= 2 and t not in _KO_STOPWORDS)
        _tb = set(t for t in _b.split() if len(t) >= 2 and t not in _KO_STOPWORDS)
        if not _ta:
            return 0.0
        return len(_ta & _tb) / len(_ta)

    # ── KOTRA detail URL 구성 헬퍼 ────────────────────────────────────
    def _build_hotclip_url(pntt_sn: str) -> str:
        return (
            "https://dream.kotra.or.kr/kotranews/cms/news/actionKotraBoardDetail.do"
            f"?SITE_NO=3&MENU_ID=1460&CONTENTS_NO=1&hotClipGbn=9&pNttSn={pntt_sn}"
        )

    def _build_bmtnews_url(bbs_ntt_sn: str) -> str:
        return (
            "https://dream.kotra.or.kr/kotranews/cms/news/actionKotraBoardDetail.do"
            f"?SITE_NO=3&MENU_ID=180&CONTENTS_NO=1&bbsGbn=322&bbsSn=322&bbsNttSn={bbs_ntt_sn}"
        )

    # ── 검색 키워드 생성 - 한국어 불용어 제거 후 핵심 4단어 ──────────
    # TODO-1: 3단어 → 4단어, 불용어 필터 적용
    _kw_all = [w for w in _title_norm.split() if len(w) >= 2 and w not in _KO_STOPWORDS]
    _kw_parts = _kw_all[:4]  # 최대 4단어
    _keyword = "+".join(_kw_parts)

    candidates: list[tuple[float, str, str]] = []  # (score, url, matched_text)

    # ── V17.6: kotra_pnttsn_cache.json 직접 조회 (SPA 스크래핑 전 최우선) ──
    # KOTRA SPA 페이지는 JS 렌더링으로 기사 목록 없음 → 수동 캐시를 먼저 확인
    import os as _os_kc
    _kc_path = _os_kc.path.join(
        _os_kc.path.dirname(_os_kc.path.dirname(_os_kc.path.abspath(__file__))),
        "data", "kotra_pnttsn_cache.json"
    )
    # 소스명 접미사 제거: "이탈리아 스킨케어 ... - 해외경제정보드림" → "이탈리아 스킨케어 ..."
    _title_clean = _re3.sub(r'\s*-\s*(해외경제정보드림|해외시장뉴스|KOTRA 해외시장뉴스)\s*$', '', title).strip()
    if _os_kc.path.exists(_kc_path):
        try:
            import json as _json_kc
            with open(_kc_path, "r", encoding="utf-8") as _cf:
                _kc = _json_kc.load(_cf)
            for _cached_title, _cached_info in _kc.items():
                if _cached_title.startswith("_"):
                    continue  # _comment 등 메타 키 스킵
                if not isinstance(_cached_info, dict):
                    continue
                _csim = _sim(_title_clean, _cached_title)
                if _csim >= 0.65:  # TODO-1: 0.75→0.65 (Jaccard 혼합으로 신뢰성 유지하며 완화)
                    _pntt = str(_cached_info.get("pNttSn", "")).strip()
                    _bbs  = str(_cached_info.get("bbsNttSn", "")).strip()
                    if _pntt and _pntt.isdigit():
                        _cache_url = _build_hotclip_url(_pntt)
                        _trace(f"[KOTRA_CACHE_HIT] [OK] title='{_title_clean[:45]}' → pNttSn={_pntt} score={_csim:.3f}")
                        # P5: cache_hit 카운트
                        _kotra_restore_stats["cache_hit"] += 1
                        print(f"[kita_source] P5 cache_hit: pNttSn={_pntt} score={_csim:.3f} '{_title_clean[:40]}'")
                        return _cache_url
                    elif _bbs and _bbs.isdigit():
                        _cache_url = _build_bmtnews_url(_bbs)
                        _trace(f"[KOTRA_CACHE_HIT] [OK] title='{_title_clean[:45]}' → bbsNttSn={_bbs} score={_csim:.3f}")
                        # P5: cache_hit 카운트
                        _kotra_restore_stats["cache_hit"] += 1
                        print(f"[kita_source] P5 cache_hit: bbsNttSn={_bbs} score={_csim:.3f} '{_title_clean[:40]}'")
                        return _cache_url
            _trace(f"[KOTRA_CACHE_MISS] title='{_title_clean[:45]}' cache={len([k for k in _kc if not k.startswith('_')])}건")
        except Exception as _ce:
            _trace(f"[KOTRA_CACHE] 캐시 로드 오류: {_ce}")

    import urllib.request as _urlreq_sys
    _sys_proxies = _urlreq_sys.getproxies()

    # TODO-1: search 타입 엔트리를 키워드 길이별로 복제 (4단어 → 3단어 → 2단어 순차)
    _search_entries_expanded: list[dict] = []
    for _entry in _KOTRA_LIST_SEARCH_URLS:
        if _entry["type"] == "search" and len(_kw_all) > 2:
            # 4단어, 3단어, 2단어 쿼리를 각각 추가
            for _n in sorted({min(4, len(_kw_all)), 3, 2}, reverse=True):
                if _n <= len(_kw_all):
                    _kw_n = "+".join(_kw_all[:_n])
                    _search_entries_expanded.append({
                        "url": _entry["url"].replace("{keyword}", _kw_n),
                        "type": "search",
                    })
        else:
            _search_entries_expanded.append(_entry)

    for _entry in _search_entries_expanded:
        _url = _entry["url"]
        _etype = _entry["type"]

        # search 타입: 이미 키워드 치환 완료 (expanded 시)
        if _etype == "search" and "{keyword}" in _url:
            if not _keyword:
                continue
            _url = _url.replace("{keyword}", _re3.sub(r"\s+", "+", _keyword))

        try:
            _headers = {
                **_BROWSER_HEADERS,
                "Referer": "https://dream.kotra.or.kr/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "application/json,*/*;q=0.8",
            }
            resp = _req.get(_url, headers=_headers, timeout=timeout, verify=False,
                            proxies=_sys_proxies if _sys_proxies else None)
            if resp.status_code != 200:
                _trace(f"[KOTRA_TITLE_MATCH] {_url[:70]} → HTTP {resp.status_code}")
                continue
            raw = resp.text
            if len(raw) < 200:
                _trace(f"[KOTRA_TITLE_MATCH] {_url[:70]} → 응답 너무 짧음({len(raw)}자)")
                continue
            # ── JSON 응답 처리 ──────────────────────────────────────
            if raw.lstrip().startswith("{") or raw.lstrip().startswith("["):
                import json as _js
                try:
                    data = _js.loads(raw)
                    # KOTRA JSON 구조: items / list / data 키 탐색
                    items_list = None
                    for _k in ("items", "list", "data", "result", "rows"):
                        if isinstance(data, dict) and _k in data:
                            items_list = data[_k]
                            break
                    if isinstance(data, list):
                        items_list = data

                    if items_list:
                        for _item in items_list[:30]:
                            if not isinstance(_item, dict):
                                continue
                            # 제목 필드 후보
                            for _tf in ("nttSj", "title", "bbsNttSj", "subject"):
                                _item_title = str(_item.get(_tf, ""))
                                if not _item_title:
                                    continue
                                _score = _sim(_title_short, _item_title)
                                if _score >= 0.45:  # TODO-1: 0.55→0.45 (Jaccard 혼합 후 최종 선택에서 필터)
                                    # ID 필드 후보
                                    _pntt = str(_item.get("pNttSn", _item.get("nttSn", "")))
                                    _bbs  = str(_item.get("bbsNttSn", _item.get("bbsSn", "")))
                                    if _pntt and _pntt.isdigit():
                                        candidates.append((_score, _build_hotclip_url(_pntt), _item_title))
                                    elif _bbs and _bbs.isdigit():
                                        candidates.append((_score, _build_bmtnews_url(_bbs), _item_title))
                except Exception as _je:
                    _trace(f"[KOTRA_TITLE_MATCH] JSON 파싱 실패: {_je}")

            # ── HTML 파싱 ───────────────────────────────────────────
            # 방법 A: BeautifulSoup
            try:
                from bs4 import BeautifulSoup as _BS4
                soup = _BS4(raw, "html.parser")

                for _tag in soup.find_all(["a", "li", "td", "dt", "span", "p"]):
                    _tag_text = _tag.get_text(separator=" ", strip=True)
                    if not _tag_text or len(_tag_text) < 5:
                        continue
                    _score = _sim(_title_short, _tag_text)
                    if _score < 0.45:  # TODO-1: 0.55→0.45 (최종 dynamic threshold에서 필터)
                        continue

                    # href에 pNttSn / bbsNttSn
                    _href = _tag.get("href", "") or ""
                    _m_pntt = _re3.search(r"pNttSn=(\d+)", _href)
                    _m_bbs  = _re3.search(r"bbsNttSn=(\d+)", _href)
                    if _m_pntt:
                        candidates.append((_score, _build_hotclip_url(_m_pntt.group(1)), _tag_text[:60]))
                        continue
                    if _m_bbs:
                        candidates.append((_score, _build_bmtnews_url(_m_bbs.group(1)), _tag_text[:60]))
                        continue

                    # onclick에서 ID 추출 (fn_detail / goView / goDetail)
                    _onclick = _tag.get("onclick", "") or ""
                    for _anc in [_tag] + list(_tag.parents)[:4]:
                        if not hasattr(_anc, "get"):
                            continue
                        _oc = _anc.get("onclick", "") or ""
                        _m_id = _re3.search(
                            r"(?:fn_detail|goView|goDetail)[^)]*?['\"]?(\d{5,7})['\"]?",
                            _oc,
                        )
                        if _m_id:
                            _id_val = _m_id.group(1)
                            if _etype == "hotclip":
                                candidates.append((_score, _build_hotclip_url(_id_val), _tag_text[:60]))
                            else:
                                candidates.append((_score, _build_bmtnews_url(_id_val), _tag_text[:60]))
                            break

            except ImportError:
                # BeautifulSoup 없을 경우 regex 파싱
                # pNttSn=XXXXXX 패턴 주변 텍스트로 매칭
                for _m in _re3.finditer(r"pNttSn=(\d{5,7})", raw):
                    _pntt_sn = _m.group(1)
                    # 주변 200자에서 제목 추출 시도
                    _ctx_start = max(0, _m.start() - 200)
                    _ctx = _re3.sub(r"<[^>]+>", " ", raw[_ctx_start:_m.start() + 100])
                    _ctx = _re3.sub(r"\s+", " ", _ctx).strip()
                    _score = _sim(_title_short, _ctx[:80])
                    if _score >= 0.45:
                        candidates.append((_score, _build_hotclip_url(_pntt_sn), _ctx[:60]))

                for _m in _re3.finditer(r"bbsNttSn=(\d{5,7})", raw):
                    _bbs_sn = _m.group(1)
                    _ctx_start = max(0, _m.start() - 200)
                    _ctx = _re3.sub(r"<[^>]+>", " ", raw[_ctx_start:_m.start() + 100])
                    _ctx = _re3.sub(r"\s+", " ", _ctx).strip()
                    _score = _sim(_title_short, _ctx[:80])
                    if _score >= 0.45:
                        candidates.append((_score, _build_bmtnews_url(_bbs_sn), _ctx[:60]))

        except Exception as _ex:
            _trace(f"[KOTRA_TITLE_MATCH] {_url[:60]} 요청 오류: {type(_ex).__name__}: {_ex}")
            continue

    # ── 최적 후보 선택 ──────────────────────────────────────────────
    _trace(f"[KOTRA_TITLE_MATCH]")
    _trace(f"title={_title_short}")
    _trace(f"candidate_count={len(candidates)}")

    if not candidates:
        _trace("resolved_detail_url=실패(후보 없음)")
        print(f"[kita_source] P5 title_match: 후보 0건 (query='{_title_norm[:40]}')")
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_url, best_text = candidates[0]

    # P5: 상위 3개 후보 점수 로그
    _top3 = candidates[:3]
    _cand_log = " | ".join(f"{s:.2f}:{t[:25]}" for s, u, t in _top3)
    print(f"[kita_source] P5 title_match candidates: [{_cand_log}]")

    _trace(f"matched_title={best_text}")
    _trace(f"matched_score={best_score:.3f}")
    _trace(f"pNttSn={_re3.search(r'pNttSn=(\d+)', best_url).group(1) if 'pNttSn=' in best_url else 'N/A'}")
    _trace(f"resolved_detail_url={best_url}")

    # TODO-1: 동적 임계값 - 토큰 오버랩이 높으면 0.50으로 완화
    # 핵심 토큰의 60% 이상이 후보에 포함되면 신뢰도 충분
    _best_toverlap = _token_overlap(_title_norm, best_text)
    _threshold = 0.50 if _best_toverlap >= 0.60 else 0.60
    if best_score < _threshold:
        _trace(f"resolved_detail_url=기각(score={best_score:.3f} < {_threshold:.2f} 임계값, token_overlap={_best_toverlap:.2f})")
        print(f"[kita_source] P5 title_match: 최고점 {best_score:.3f} < {_threshold:.2f} (token_overlap={_best_toverlap:.2f}) → 기각")
        return None

    return best_url


def _fetch_kotra_gnews(
    industry_key: str,
    keywords: list[str],
    max_items: int = 5,
) -> list[dict]:
    """
    V17.1: Google News RSS 경유 KOTRA 해외시장뉴스 수집.

    KOTRA HTML 파서 실패(Type A: HTML 구조 변경) 대응.
    Google News site:dream.kotra.or.kr RSS → KOTRA 기사 URL + 요약.

    의존성: feedparser 불필요 - xml.etree.ElementTree (stdlib) 직접 파싱.
    feedparser 설치 여부와 무관하게 동작.

    반환 기사 플래그:
      - <source url>이 dream.kotra.or.kr 인 경우: no_fetch=False → tier0 가능
      - Google News URL만 있는 경우:              no_fetch=True  → tier1
    """
    _req = _get_requests()
    if not _req:
        return []

    import re as _re
    import xml.etree.ElementTree as _ET
    import urllib.parse as _urlp
    from datetime import datetime as _dt
    _now_ym = _dt.now().strftime("%Y%m")

    query = _KOTRA_GNEWS_QUERIES.get(industry_key, _KOTRA_GNEWS_QUERIES["일반"])
    rss_url = (
        f"https://news.google.com/rss/search"
        f"?q={_urlp.quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    )

    try:
        _RSS_HEADERS = {
            **_BROWSER_HEADERS,
            "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
        }
        resp = _req.get(rss_url, headers=_RSS_HEADERS, timeout=10, verify=False)
        resp.raise_for_status()
        raw_xml = resp.content
    except Exception as e:
        print(f"[kita_source] KOTRA Google News RSS 실패: {type(e).__name__}: {e}")
        return []

    # ── stdlib XML 파싱 ────────────────────────────────────────────
    try:
        root = _ET.fromstring(raw_xml)
    except _ET.ParseError as e:
        print(f"[kita_source] KOTRA Google News XML 파싱 실패: {e}")
        return []

    # RSS 2.0: <rss><channel><item>...</item></channel></rss>
    channel = root.find("channel")
    if channel is None:
        print("[kita_source] KOTRA Google News: channel 태그 없음")
        return []

    items = channel.findall("item")
    print(f"[kita_source] KOTRA Google News RSS: {len(items)}건 수신 ({industry_key})")
    if not items:
        return []

    def _text(el, tag: str) -> str:
        node = el.find(tag)
        return (node.text or "").strip() if node is not None else ""

    articles: list[dict] = []
    for item in items:
        title = _text(item, "title")
        if not title or len(title) < 8:
            continue

        # Google News RSS <link> 태그: 일반적으로 Google News 래퍼 URL
        gnews_link = _text(item, "link")

        # <description>에서 원문 URL 추출 시도
        # Google News RSS는 <description>에 <a href="실제URL">을 포함할 때가 있음
        desc_raw = _text(item, "description")
        desc_clean = _re.sub(r"<[^>]+>", "", desc_raw).strip()

        # <source url="..."> 속성에서 실제 도메인 확인
        src_el = item.find("source")
        src_url = (src_el.get("url", "") if src_el is not None else "")

        # <link> 이후 텍스트에 실제 URL이 있을 경우 추출 (일부 RSS 포맷)
        # 실제 KOTRA URL 판별: dream.kotra.or.kr 포함 여부
        real_url = gnews_link
        # P2: V17.5 - description HTML decode 후 KOTRA URL 추출 (html.unescape 적용)
        # Google News description이 HTML 인코딩된 경우(&amp;, &quot; 등) URL이 가려짐
        import html as _html_mod
        desc_decoded = _html_mod.unescape(desc_raw)  # &amp; → &, &quot; → " 등 변환
        _kotra_url_pattern = r'https?://[^\s"\'<>]*kotra\.or\.kr[^\s"\'<>]{10,}'
        kotra_urls_in_desc = _re.findall(_kotra_url_pattern, desc_raw)
        if not kotra_urls_in_desc:
            # decode 후 재탐색
            kotra_urls_in_desc = _re.findall(_kotra_url_pattern, desc_decoded)
        if kotra_urls_in_desc:
            real_url = kotra_urls_in_desc[0]

        # V17.2: Google News RSS link에서 base64 디코딩으로 KOTRA 원문 URL 추출
        # Google News RSS <link>는 /articles/CBMi{base64}... 형태로 원문 URL이 인코딩됨
        # description에서 KOTRA URL 미발견 시 → base64 디코딩 시도
        if "news.google.com" in real_url and "kotra" not in real_url:
            try:
                import base64 as _b64
                _m_enc = _re.search(r"/articles/([A-Za-z0-9_=-]+)", real_url)
                if _m_enc:
                    _encoded = _m_enc.group(1)
                    _padded = _encoded + "=" * (4 - len(_encoded) % 4)
                    _decoded = _b64.urlsafe_b64decode(_padded)
                    _url_match = _re.search(
                        rb'https?://(?:dream\.)?kotra\.or\.kr[^\x00-\x1f\s]{10,}',
                        _decoded,
                    )
                    if _url_match:
                        _candidate = _url_match.group(0).decode("utf-8", errors="ignore")
                        real_url = _candidate
            except Exception:
                pass  # 디코딩 실패 시 gnews_link 유지

        is_gnews_url = "news.google.com" in real_url

        # V17.4: <source url> 속성이 kotra.or.kr 도메인이면 KOTRA 기사로 확정 후
        # 실제 KOTRA URL 해소를 시도한다.
        #
        # ■ 버그 수정 (V17.3 → V17.4):
        #   V17.3은 is_gnews_url=False로 설정하여 no_fetch=False를 만들었으나,
        #   real_url 자체는 여전히 Google News URL(gnews_link)이었음.
        #   이로 인해 fetch_detail()이 Google News URL로 호출 →
        #   fetcher.py fast-fail → "Google News URL (JS redirect, 본문 추출 불가)" 표시.
        #
        # ■ 수정 내용:
        #   _try_resolve_gnews_url()로 실제 KOTRA URL 해소 시도.
        #   성공 시: real_url = 해소된 KOTRA URL, is_gnews_url=False → tier0 (정상 fetch)
        #   실패 시: is_gnews_url=True 유지 → no_fetch=True → RSS 스니펫 사용 (안전 fallback)
        _is_kotra_src = "kotra.or.kr" in src_url
        if is_gnews_url and _is_kotra_src:
            _kotra_restore_stats["total"] += 1
            # ── V17.4: Google News redirect 해소 시도 ────────────────────
            _resolved = _try_resolve_gnews_url(gnews_link)
            if _resolved and "kotra.or.kr" in _resolved:
                real_url = _resolved
                is_gnews_url = False
                _kotra_restore_stats["success"] += 1
                _kotra_restore_stats["gnews_ok"] += 1
                print(
                    f"[kita_source] V17.4 [OK] KOTRA URL 해소 성공: {real_url[:60]} | {title[:35]}"
                )
            else:
                # ── V17.5/V17.6: title 기반 KOTRA 목록 탐색 fallback ─────
                print(
                    f"[kita_source] V17.4 resolve 실패 → V17.5/V17.6 title 탐색 fallback: {title[:40]}"
                )
                _resolved_by_title = _try_find_kotra_url_by_title(title)
                if _resolved_by_title and "kotra.or.kr" in _resolved_by_title:
                    real_url = _resolved_by_title
                    is_gnews_url = False
                    _kotra_restore_stats["success"] += 1
                    _kotra_restore_stats["title_ok"] += 1
                    print(
                        f"[kita_source] V17.5 [OK] title 매칭으로 KOTRA URL 복원: "
                        f"{real_url[:70]} | {title[:35]}"
                    )
                else:
                    # 최종 실패 → no_fetch=True (안전 fallback)
                    _kotra_restore_stats["failed"] += 1
                    print(
                        f"[kita_source] V17.5 KOTRA URL 복원 전체 실패 → 스니펫 모드: {title[:40]}"
                    )
                    # is_gnews_url 변경 없음 → no_fetch=True

        # 날짜
        pub_raw = _text(item, "pubDate")
        pub_m = _re.search(r"(\d{4})-?(\d{2})", pub_raw)
        issue_ym = f"{pub_m.group(1)}{pub_m.group(2)}" if pub_m else _now_ym

        # 키워드 필터
        text = title + " " + desc_clean
        if keywords and not any(kw in text for kw in keywords):
            continue

        # ── V17.4 TRACE: 이탈리아 기사 한정 상세 로그 ──────────────────
        if "이탈리아" in title and "스킨케어" in title:
            _trace("[KOTRA_TRACE][SOURCE]")
            _trace(f"title={title}")
            _trace(f"src_url={src_url}")
            _trace(f"gnews_link={gnews_link[:100]}")
            _trace(f"is_kotra_src={_is_kotra_src}")
            _trace(f"kotra_urls_in_desc={kotra_urls_in_desc[:2] if kotra_urls_in_desc else '[]'}")
            _trace(f"resolved_kotra_url={'성공: '+real_url[:80] if not is_gnews_url else '실패(None)'}")
            _trace(f"article.url={real_url[:100]}")
            _trace(f"article.no_fetch={is_gnews_url}")
            _trace(f"article._google_news={is_gnews_url}")

        articles.append({
            "doc_id": _make_doc_id(real_url),
            "title": title,
            "summary": desc_clean[:300] if desc_clean else title[:200],
            "body": desc_clean if len(desc_clean) >= 50 else "",
            "url": real_url,
            "link": real_url,
            "published": pub_raw,
            "issue_yyyymm": issue_ym,
            "category": "코트라",
            "date": pub_raw,
            "source": "코트라",
            # tier 플래그: KOTRA URL → tier0, Google URL → tier1
            "_google_news": is_gnews_url,
            "no_fetch": is_gnews_url,
        })

        if len(articles) >= max_items:
            break

    if articles:
        tier0_cnt = sum(1 for a in articles if not a.get("_google_news"))
        tier1_cnt = sum(1 for a in articles if a.get("_google_news"))
        print(
            f"[kita_source] [OK] KOTRA Google News {len(articles)}건 수집 "
            f"(kotra_url={tier0_cnt} / gnews_url={tier1_cnt}) [{industry_key}]"
        )
        # P5: 복원 성공률 요약 출력
        _rs = get_kotra_restore_stats()
        if _rs["total"] > 0:
            print(
                f"[kita_source] P5 복원율: {_rs['success']}/{_rs['total']} "
                f"({_rs['restore_rate_pct']}%) "
                f"[cache={_rs['cache_hit']} gnews={_rs['gnews_ok']} title={_rs['title_ok']} fail={_rs['failed']}]"
            )
    else:
        print(f"[kita_source] KOTRA Google News: 키워드 매칭 없음 ({industry_key})")

    return articles


def _fetch_kotra_html(keywords: list[str], max_items: int = 5) -> list[dict]:
    """
    V16.3(수정): KOTRA 해외시장뉴스 목록 HTML 파서 - onclick 기반 URL 구성.

    핵심 수정 사유:
      KOTRA dream.kotra.or.kr 기사 목록의 a 태그는
        href="javascript:void(0)" 또는 href="#" 으로 직접 URL이 없고,
        onclick="fn_detail(this, '322', '123456')" 형태로 기사 ID를 전달함.
      → bbsNttSn을 href에서 탐색하면 항상 0건 (V16.3 기존 버그 원인).
      → onclick 속성에서 숫자 추출 → _build_kotra_detail_url() 로 URL 직접 구성.
    [WARN] V17.1: HTML 파서는 KOTRA SPA 전환 이후 동작 불안정 (onclick 0건).
      _fetch_kotra_gnews()가 먼저 시도됨. 이 함수는 HTML 구조 복구 시를 위해 유지.

    탐색 우선순위:
      1) a 태그 onclick 속성에서 bbsNttSn 추출
      2) li/tr 등 부모 요소의 onclick/data-* 속성에서 추출
      3) href에 bbsNttSn= 포함 (일부 URL 직접 노출 케이스 대비)
      4) href에 actionKotraBoardDetail 포함 (fallback)
    """
    _req = _get_requests()
    if not _req:
        return []

    import re as _re
    from datetime import datetime as _dt
    _now_ym = _dt.now().strftime("%Y%m")

    try:
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        print("[kita_source] BeautifulSoup 미설치")
        return []

    # KOTRA 뉴스 목록 후보 URL들
    _kotra_urls = [
        # 1순위: 해외시장뉴스 목록 (bbsSn=322 - 기사 onclick 포함)
        "https://dream.kotra.or.kr/kotranews/cms/com/actionKotraBoardList.do?SITE_NO=3&MENU_ID=180&CONTENTS_NO=1&bbsGbn=322&bbsSn=322",
        # 2순위: 기존 뷰 URL
        "https://dream.kotra.or.kr/kotranews/cms/com/actionBbsNNewsView.do?SITE_NO=3&MENU_ID=180&CONTENTS_NO=1&bbsGbn=322&bbsSn=322&pageIndex=1",
        # 3순위: 글로벌 비즈니스 리포트
        "https://dream.kotra.or.kr/user/extra/kotranews/globalBbs/List.do?menuIdx=403",
    ]

    for _url in _kotra_urls:
        try:
            resp = _req.get(
                _url,
                headers={**_BROWSER_HEADERS, "Referer": "https://dream.kotra.or.kr/"},
                timeout=12,
                verify=False,
            )
            resp.raise_for_status()
            raw = resp.text
            if len(raw) < 500:
                print(f"[kita_source] KOTRA HTML 응답 너무 짧음: {len(raw)}자 ({_url[:60]})")
                continue
            print(f"[kita_source] KOTRA HTML 수신: {len(raw)}자 ({_url[:60]})")
        except Exception as e:
            print(f"[kita_source] KOTRA HTML 수집 실패: {type(e).__name__}: {e} ({_url[:60]})")
            continue

        try:
            soup = _BS(raw, "html.parser")
            articles: list[dict] = []

            # ── 방법 A: onclick 속성에서 bbsNttSn 추출 (V16.3 핵심 수정) ──
            # KOTRA 기사 목록: <a href="javascript:void(0)" onclick="fn_detail(this,'322','123456')">
            onclick_candidates = []
            for a in soup.find_all("a"):
                oc = a.get("onclick", "") or ""
                href = a.get("href", "") or ""
                # onclick에 숫자 있거나, href에 bbsNttSn 직접 포함
                sn = _extract_kotra_bbs_ntt_sn(a)
                if not sn and "bbsNttSn=" in href:
                    m = _re.search(r"bbsNttSn=(\d+)", href)
                    if m:
                        sn = m.group(1)
                if sn:
                    onclick_candidates.append((a, sn))

            print(f"[kita_source] KOTRA onclick/bbsNttSn 후보: {len(onclick_candidates)}개 ({_url[:60]})")

            if onclick_candidates:
                for a_tag, bbs_ntt_sn in onclick_candidates:
                    title = a_tag.get_text(strip=True)
                    if not title or len(title) < 15:
                        continue
                    detail_url = _build_kotra_detail_url(bbs_ntt_sn)

                    if keywords and not any(kw in title for kw in keywords):
                        continue

                    # 날짜: 부모 요소에서 탐색
                    date_str = ""
                    for _ancestor in [a_tag.parent, a_tag.parent.parent if a_tag.parent else None]:
                        if _ancestor:
                            date_tag = _ancestor.find(class_=_re.compile(r"date|time|regdate", _re.I))
                            if date_tag:
                                date_str = date_tag.get_text(strip=True)
                                break
                    m_d = _re.search(r"(\d{4})[.\-/](\d{2})", date_str or "")
                    issue_ym = f"{m_d.group(1)}{m_d.group(2)}" if m_d else _now_ym

                    articles.append({
                        "doc_id": _make_doc_id(detail_url),
                        "title": title,
                        "summary": title[:200],
                        "url": detail_url,
                        "link": detail_url,
                        "published": date_str or _dt.now().strftime("%Y-%m-%d"),
                        "issue_yyyymm": issue_ym,
                        "category": "코트라",
                        "date": date_str or _dt.now().strftime("%Y-%m-%d"),
                        "source": "코트라",
                    })

                    if len(articles) >= max_items:
                        break

                if articles:
                    print(f"[kita_source] KOTRA HTML 파서(onclick): {len(articles)}건 수집")
                    return articles
                print(f"[kita_source] KOTRA onclick: 키워드 매칭 없음 ({_url[:60]})")
                # 키워드 불일치 시 다음 URL로 넘어가지 말고 일반 키워드로 다시 시도
                if keywords:
                    # 키워드 없이 다시 수집 (키워드 필터 해제 후 최대 max_items개)
                    for a_tag, bbs_ntt_sn in onclick_candidates:
                        title = a_tag.get_text(strip=True)
                        if not title or len(title) < 15:
                            continue
                        detail_url = _build_kotra_detail_url(bbs_ntt_sn)
                        date_str = ""
                        _ancestor = a_tag.parent
                        if _ancestor:
                            date_tag = _ancestor.find(class_=_re.compile(r"date|time|regdate", _re.I))
                            if date_tag:
                                date_str = date_tag.get_text(strip=True)
                        m_d = _re.search(r"(\d{4})[.\-/](\d{2})", date_str or "")
                        issue_ym = f"{m_d.group(1)}{m_d.group(2)}" if m_d else _now_ym
                        articles.append({
                            "doc_id": _make_doc_id(detail_url),
                            "title": title,
                            "summary": title[:200],
                            "url": detail_url,
                            "link": detail_url,
                            "published": date_str or _dt.now().strftime("%Y-%m-%d"),
                            "issue_yyyymm": issue_ym,
                            "category": "코트라",
                            "date": date_str or _dt.now().strftime("%Y-%m-%d"),
                            "source": "코트라",
                        })
                        if len(articles) >= max_items:
                            break
                    if articles:
                        print(f"[kita_source] KOTRA HTML 파서(onclick, 키워드 해제): {len(articles)}건 수집")
                        return articles
                continue

            # ── 방법 B: li/tr 부모 요소 onclick/data-* 탐색 ─────────────
            # onclick이 a 태그가 아닌 li/tr에 있는 경우
            parent_candidates = []
            for sel in ["li", "tr"]:
                for el in soup.find_all(sel):
                    sn = _extract_kotra_bbs_ntt_sn(el)
                    if sn:
                        a_inner = el.find("a")
                        if a_inner:
                            parent_candidates.append((a_inner, sn, el))

            print(f"[kita_source] KOTRA 부모 onclick/data 후보: {len(parent_candidates)}개 ({_url[:60]})")

            if parent_candidates:
                for a_tag, bbs_ntt_sn, container in parent_candidates:
                    title = a_tag.get_text(strip=True)
                    if not title or len(title) < 15:
                        continue
                    detail_url = _build_kotra_detail_url(bbs_ntt_sn)
                    if keywords and not any(kw in title for kw in keywords):
                        continue
                    date_str = ""
                    date_tag = container.find(class_=_re.compile(r"date|time|regdate", _re.I))
                    if date_tag:
                        date_str = date_tag.get_text(strip=True)
                    m_d = _re.search(r"(\d{4})[.\-/](\d{2})", date_str or "")
                    issue_ym = f"{m_d.group(1)}{m_d.group(2)}" if m_d else _now_ym
                    articles.append({
                        "doc_id": _make_doc_id(detail_url),
                        "title": title,
                        "summary": title[:200],
                        "url": detail_url,
                        "link": detail_url,
                        "published": date_str or _dt.now().strftime("%Y-%m-%d"),
                        "issue_yyyymm": issue_ym,
                        "category": "코트라",
                        "date": date_str or _dt.now().strftime("%Y-%m-%d"),
                        "source": "코트라",
                    })
                    if len(articles) >= max_items:
                        break
                if articles:
                    print(f"[kita_source] KOTRA HTML 파서(부모 onclick): {len(articles)}건 수집")
                    return articles

            # ── 방법 C: 원본 HTML 정규식으로 bbsNttSn + 제목 추출 ───────
            # BeautifulSoup 구조 탐지 실패 시 raw HTML 정규식
            # 패턴 1: onclick="fn_detail(this,'322','123456')" ...>제목</a>
            raw_matches = _re.findall(
                r'onclick=["\'][^"\']*?(?:fn_detail|goView|goDetail|detailView)'
                r'[^"\']*?(\d{4,})[^"\']*?["\'][^>]*?>([^<]{10,})<\/a>',
                raw, _re.I
            )
            print(f"[kita_source] KOTRA 정규식 추출: {len(raw_matches)}개 ({_url[:60]})")
            for sn, title_raw in raw_matches[:max_items * 2]:
                title = title_raw.strip()
                if not title or len(title) < 10:
                    continue
                if keywords and not any(kw in title for kw in keywords):
                    continue
                detail_url = _build_kotra_detail_url(sn)
                articles.append({
                    "doc_id": _make_doc_id(detail_url),
                    "title": title,
                    "summary": title[:200],
                    "url": detail_url,
                    "link": detail_url,
                    "published": _dt.now().strftime("%Y-%m-%d"),
                    "issue_yyyymm": _now_ym,
                    "category": "코트라",
                    "date": _dt.now().strftime("%Y-%m-%d"),
                    "source": "코트라",
                })
                if len(articles) >= max_items:
                    break
            if articles:
                print(f"[kita_source] KOTRA HTML 파서(정규식): {len(articles)}건 수집")
                return articles

            print(f"[kita_source] KOTRA HTML: A/B/C 방법 모두 실패 ({_url[:60]})")

        except Exception as e:
            print(f"[kita_source] KOTRA HTML 파싱 실패: {type(e).__name__}: {e}")

    return []


def fetch_kita_news(industry_key: str = "일반", max_items: int = 5) -> list[dict]:
    """
    V17.1: KITA 무역뉴스 / KOTRA 해외시장뉴스를 수집한다.
    RSS → HTML → Google News fallback 순서의 6단계 체인.

    단계:
      1) KITA RSS (_KITA_NEWS_RSS_URLS) - 404/403 정상 fallback
      2) KITA HTML 파서 - 404 정상 fallback
      3) KOTRA RSS (_KOTRA_NEWS_RSS_URLS) - 404 정상 fallback
      4) KOTRA Google News RSS (_fetch_kotra_gnews) ← V17.1 신설
         KOTRA HTML SPA 전환으로 onclick=0건 확정 대응.
         real KOTRA URL 획득 시 tier0, Google URL fallback 시 tier1.
      5) KOTRA HTML 파서 (onclick 기반) - SPA이면 0건 예상, 구조 복구 대비 유지
      6) 일반 키워드로 KOTRA Google News 재시도 (industry_key != "일반")

    Args:
        industry_key: 산업 키 (industry_config.py의 키)
        max_items: 최대 수집 건수

    Returns:
        [{doc_id, title, summary, url, link, published,
          issue_yyyymm, category, date, source}, ...]
    """
    keywords = _KITA_KEYWORDS.get(industry_key, _KITA_KEYWORDS["일반"])

    # ── TODO-4: KITA 뉴스 circuit breaker ─────────────────────────
    # kita.net 403 Forbidden 상시 발생 → 불필요한 HTTP 시도 2회 생략
    # 해소 시: _KITA_NEWS_CIRCUIT_OPEN = False 로 전환
    if _KITA_NEWS_CIRCUIT_OPEN:
        print("[kita_source] KITA 뉴스 circuit breaker OPEN → 1~2단계 스킵, KOTRA로 직행")
    else:
        # ── 1단계: KITA RSS (404/403 정상 fallback) ─────────────────
        articles = _fetch_news_from_rss_list(_KITA_NEWS_RSS_URLS, "KITA", keywords, max_items)
        if articles:
            print(f"[kita_source] [OK] KITA RSS {len(articles)}건 수집 ({industry_key})")
            return articles

        # ── 2단계: KITA HTML 파서 (404 정상 fallback) ───────────────
        print("[kita_source] KITA RSS 전부 실패(정상) → KITA HTML 파서 시도")
        articles = _fetch_kita_html(keywords, max_items)
        if articles:
            print(f"[kita_source] [OK] KITA HTML {len(articles)}건 수집 ({industry_key})")
            return articles
        print("[kita_source] KITA HTML 파서도 실패 → KOTRA 파이프라인으로 전환")

    articles = []  # 이후 3~6단계는 KOTRA 파이프라인

    if False:  # unreachable - circuit breaker OFF 분기 나머지 코드 연결용 placeholder
        articles = _fetch_kita_html(keywords, max_items)
    if articles:
        print(f"[kita_source] [OK] KITA HTML {len(articles)}건 수집 ({industry_key})")
        return articles

    # ── 3단계: KOTRA RSS (기존 RSS, 404 정상 fallback) ───────────
    print("[kita_source] KITA 전부 실패(정상) → KOTRA RSS 시도")
    articles = _fetch_news_from_rss_list(_KOTRA_NEWS_RSS_URLS, "코트라", keywords, max_items)
    if articles:
        print(f"[kita_source] [OK] KOTRA RSS {len(articles)}건 수집 ({industry_key})")
        return articles

    # ── 4단계: KOTRA Google News RSS ─────────────────────────────
    # V17.1: KOTRA HTML SPA 전환(onclick=0건) 확정 → Google News 우선
    print("[kita_source] KOTRA RSS 전부 실패 → KOTRA Google News RSS 시도 (V17.1)")
    articles = _fetch_kotra_gnews(industry_key, keywords, max_items)
    if articles:
        return articles

    # ── 5단계: KOTRA HTML 파서 (구조 복구 대비 유지) ──────────────
    # SPA 상태이면 0건 예상. HTML 구조 복구 시 자동으로 동작함.
    print("[kita_source] KOTRA Google News 키워드 없음 → KOTRA HTML 파서 시도")
    articles = _fetch_kotra_html(keywords, max_items)
    if articles:
        print(f"[kita_source] [OK] KOTRA HTML {len(articles)}건 수집 ({industry_key})")
        return articles

    # ── 6단계: 일반 키워드로 KOTRA Google News 재시도 ─────────────
    if industry_key != "일반":
        print(f"[kita_source] '{industry_key}' 키워드 없음 → 일반 키워드 KOTRA Google News 재시도")
        general_kw = _KITA_KEYWORDS["일반"]
        articles = _fetch_kotra_gnews("일반", general_kw, max_items)
        if articles:
            print(f"[kita_source] [OK] KOTRA Google News 일반 키워드 {len(articles)}건 수집")
            return articles

    print(f"[kita_source] [WARN] KITA/KOTRA 전체 실패 ({industry_key}) - 빈 목록 반환")
    return []
