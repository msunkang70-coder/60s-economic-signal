"""
core/motie_source.py
산업통상자원부 보도자료 RSS 연동

산업부 보도자료 RSS를 파싱하여 산업별 관련 기사를 수집한다.
extra_sources.py의 fetch_all_sources()에 통합되어
source="산업부" 태그로 기사 목록에 병합된다.

주요 함수:
  - fetch_motie_news(industry_key, max_items) → 산업부 기사 리스트
"""

import hashlib
import re
import time as _time
from datetime import datetime

try:
    import feedparser
except ImportError:
    feedparser = None  # type: ignore[assignment]

# ── 산업부 RSS URL ────────────────────────────────────────────
# V16.1 Fix: motie.go.kr RSS 전체 404 → korea.kr 정책브리핑 RSS로 교체
# 검증: motie.go.kr/rss/*.do 모두 HTTP 404 확인 (2026-03-14)
# korea.kr에서 산업부 단독 RSS 공식 제공 확인
_MOTIE_RSS_URLS: list[str] = [
    "https://www.korea.kr/rss/dept_motie.xml",   # ★ 1순위: 정책브리핑 산업부 전용 RSS (검증완료)
    "https://www.korea.kr/rss/pressrelease.xml", # 2순위: 정책브리핑 전 부처 통합 보도자료 RSS
]
_MOTIE_RSS_URL = _MOTIE_RSS_URLS[0]  # 하위 호환 유지

# HTML 파서 fallback: motie.go.kr 신규 보도자료 목록 URL (RSS 전부 실패 시)
_MOTIE_HTML_URL = "https://motie.go.kr/kor/article/ATCL3f49a5a8c"

# Fix F: 실패 결과 인메모리 쿨다운 - 연속 실패 시 15분간 재시도 억제
_motie_fail_until: float = 0.0   # 이 시각 이후에만 재시도 허용
_MOTIE_FAIL_COOLDOWN = 900       # 15분 (초)

# ── 산업별 필터 키워드 ────────────────────────────────────────
_MOTIE_FILTER_KEYWORDS = {
    "반도체": ["반도체", "CHIPS", "수출통제", "첨단산업", "디스플레이"],
    "자동차": ["자동차", "관세", "전기차", "IRA", "자동차부품", "완성차"],
    "배터리": ["배터리", "2차전지", "IRA", "광물", "리튬", "핵심광물"],
    "화학": ["화학", "CBAM", "탄소", "석유화학", "탄소중립"],
    "소비재": ["소비재", "식품", "화장품", "유통", "수출", "K-뷰티"],
    "조선": ["조선", "선박", "해양", "LNG", "해운"],
    "철강": ["철강", "금속", "CBAM", "탄소", "철광석"],
    "일반": ["수출", "무역", "통상", "산업", "경제", "투자", "규제"],
}


def _make_doc_id(url: str) -> str:
    """URL 기반 고유 ID 생성."""
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"motie_{h}"


def _parse_date(entry) -> str:
    """RSS entry에서 YYYYMM 형식 추출."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return f"{parsed.tm_year:04d}{parsed.tm_mon:02d}"
        except (AttributeError, TypeError):
            pass
    published = entry.get("published", "") or entry.get("updated", "")
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


def fetch_motie_news(industry_key: str = "일반", max_items: int = 5) -> list[dict]:
    """
    산업통상자원부 보도자료 RSS에서 산업 관련 기사를 수집한다.

    Args:
        industry_key: 산업 키 (industry_config.py의 키)
        max_items: 최대 수집 건수

    Returns:
        [{"doc_id", "title", "summary", "url", "issue_yyyymm",
          "category", "date", "source": "산업부", "link"}, ...]
    """
    global _motie_fail_until

    if feedparser is None:
        print("[motie_source] feedparser 미설치 - pip install feedparser")
        return []

    # Fix F: 쿨다운 중이면 즉시 빈 목록 반환 (재시도 억제)
    _now = _time.time()
    if _now < _motie_fail_until:
        _remaining = int(_motie_fail_until - _now)
        print(f"[motie_source] [PAUSE] 쿨다운 중 (잔여 {_remaining}초) - MOTIE RSS 재시도 생략")
        return []

    # V16.2 Fix P1-1: urllib → requests 전환 (WinError 10054 해결)
    # requests는 Connection 헤더·Keep-Alive 등 브라우저 수준 핸들링 → 서버 차단 우회
    try:
        import requests as _requests
        import urllib3 as _urllib3
        _urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)
        _HAS_REQUESTS = True
    except ImportError:
        _HAS_REQUESTS = False

    _BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    feed = None
    _last_err = None
    for _rss_url in _MOTIE_RSS_URLS:
        try:
            if _HAS_REQUESTS:
                # requests 기반 - WinError 10054 / SSL mismatch 모두 처리
                _resp = _requests.get(
                    _rss_url,
                    headers=_BROWSER_HEADERS,
                    timeout=8,
                    verify=False,  # SSL hostname mismatch 허용
                )
                _resp.raise_for_status()
                raw = _resp.content
            else:
                import urllib.request
                req = urllib.request.Request(_rss_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw = resp.read()
            _parsed = feedparser.parse(raw)
            if _parsed.entries:
                feed = _parsed
                print(f"[motie_source] [OK] RSS 수집 성공: {_rss_url} ({len(_parsed.entries)}건)")
                break
            else:
                print(f"[motie_source] RSS 항목 없음 ({_rss_url}) - 다음 URL 시도")
        except Exception as e:
            _last_err = e
            print(f"[motie_source] RSS 실패 ({_rss_url}): {type(e).__name__}: {e} - 다음 URL 시도")

    if feed is None:
        print(f"[motie_source] 모든 RSS URL 실패: {_last_err}")
        # V16.1 Fix: RSS 전부 실패 시 HTML 파서로 최종 fallback 시도
        print(f"[motie_source] HTML 파서 fallback 시도: {_MOTIE_HTML_URL}")
        _html_articles = _fetch_motie_html(industry_key, max_items)
        if _html_articles:
            _motie_fail_until = 0.0
            print(f"[motie_source] [OK] HTML 파서 성공: {len(_html_articles)}건")
            return _html_articles
        _motie_fail_until = _time.time() + _MOTIE_FAIL_COOLDOWN
        print(f"[motie_source] [WARN] 쿨다운 설정: {_MOTIE_FAIL_COOLDOWN}초 동안 재시도 억제")
        return []
    _motie_fail_until = 0.0  # 성공 시 쿨다운 해제

    if not feed.entries:
        print("[motie_source] 산업부 RSS 항목 없음")
        # korea.kr RSS에 항목이 없어도 HTML fallback 시도
        return _fetch_motie_html(industry_key, max_items)

    # 최신순 정렬
    sorted_entries = sorted(
        feed.entries, key=lambda e: _parse_sort_key(e), reverse=True,
    )

    # 산업 키워드 필터
    keywords = _MOTIE_FILTER_KEYWORDS.get(industry_key, _MOTIE_FILTER_KEYWORDS["일반"])

    articles = []
    for entry in sorted_entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue

        summary = entry.get("summary", "").strip()
        summary = re.sub(r"<[^>]+>", "", summary).strip()
        text = title + " " + summary

        # 키워드 필터링
        if not any(kw in text for kw in keywords):
            continue

        published = entry.get("published", "") or entry.get("updated", "")

        articles.append({
            "doc_id": _make_doc_id(link),
            "title": title,
            "summary": summary[:300] if summary else "",
            "url": link,
            "link": link,
            "published": published,
            "issue_yyyymm": _parse_date(entry),
            "category": "산업부",
            "date": published,
            "source": "산업부",
        })

        if len(articles) >= max_items:
            break

    # 키워드 매칭 결과가 없으면 일반 키워드로 재시도
    if not articles and industry_key != "일반":
        print(f"[motie_source] '{industry_key}' 키워드 매칭 없음 → 일반 키워드 재시도")
        general_kw = _MOTIE_FILTER_KEYWORDS["일반"]
        for entry in sorted_entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue

            summary = entry.get("summary", "").strip()
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            text = title + " " + summary

            if not any(kw in text for kw in general_kw):
                continue

            published = entry.get("published", "") or entry.get("updated", "")
            articles.append({
                "doc_id": _make_doc_id(link),
                "title": title,
                "summary": summary[:300] if summary else "",
                "url": link,
                "link": link,
                "published": published,
                "issue_yyyymm": _parse_date(entry),
                "category": "산업부",
                "date": published,
                "source": "산업부",
            })

            if len(articles) >= max_items:
                break

    print(f"[motie_source] 산업부 보도자료 {len(articles)}건 수집 (산업: {industry_key})")
    return articles


def _fetch_motie_html(industry_key: str = "일반", max_items: int = 5) -> list[dict]:
    """V16.2: motie.go.kr 보도자료 목록 HTML 파서 fallback (requests 기반).

    RSS 전체 실패 시 HTML 목록 페이지를 직접 파싱하여 기사 수집.
    대상: https://motie.go.kr/kor/article/ATCL3f49a5a8c
    """
    _BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://motie.go.kr/",
    }
    try:
        try:
            import requests as _requests
            import urllib3 as _urllib3
            _urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)
            _resp = _requests.get(
                _MOTIE_HTML_URL,
                headers=_BROWSER_HEADERS,
                timeout=10,
                verify=False,  # SSL hostname mismatch 허용 (motie.go.kr 인증서 이슈)
            )
            _resp.raise_for_status()
            raw = _resp.text
        except ImportError:
            import urllib.request
            req = urllib.request.Request(
                _MOTIE_HTML_URL,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
        print(f"[motie_source] HTML 페이지 수신: {len(raw)}자")
    except Exception as e:
        print(f"[motie_source] HTML fallback 수집 실패: {type(e).__name__}: {e}")
        return []

    try:
        try:
            from bs4 import BeautifulSoup as _BS
        except ImportError:
            print("[motie_source] BeautifulSoup 미설치 - pip install beautifulsoup4")
            return []
        soup = _BS(raw, "html.parser")
        keywords = _MOTIE_FILTER_KEYWORDS.get(industry_key, _MOTIE_FILTER_KEYWORDS["일반"])
        articles = []
        _now_ym = datetime.now().strftime("%Y%m")

        # motie.go.kr 신규 구조: li.item 또는 a 태그에서 제목+링크 추출
        for item in soup.select("li, tr.board_list, .board_list li, article li"):
            a_tag = item.find("a", href=True)
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            if not title or len(title) < 5:
                continue
            href = a_tag["href"]
            if not href.startswith("http"):
                href = "https://motie.go.kr" + href if href.startswith("/") else _MOTIE_HTML_URL

            # 키워드 필터
            if not any(kw in title for kw in keywords):
                continue

            articles.append({
                "doc_id": _make_doc_id(href),
                "title": title,
                "summary": title[:200],
                "url": href,
                "link": href,
                "published": datetime.now().strftime("%Y-%m-%d"),
                "issue_yyyymm": _now_ym,
                "category": "산업부",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "source": "산업부",
            })
            if len(articles) >= max_items:
                break

        if not articles:
            print("[motie_source] HTML 파서: 키워드 매칭 없음")
        else:
            print(f"[motie_source] HTML 파서 수집: {len(articles)}건")
        return articles
    except Exception as e:
        print(f"[motie_source] HTML 파서 처리 실패: {e}")
        return []
