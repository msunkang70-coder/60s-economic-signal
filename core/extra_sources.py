"""
core/extra_sources.py
KOTRA 해외시장뉴스 RSS 수집.

fetcher.py의 fetch_list()와 동일한 dict 형식으로 반환:
  {"doc_id", "title", "url", "issue_yyyymm", "category"}
"""

import hashlib
import re
from datetime import datetime

try:
    import feedparser
except ImportError:
    feedparser = None  # type: ignore[assignment]

# KOTRA 해외시장뉴스 RSS
_KOTRA_RSS_URL = "https://dream.kotra.or.kr/kotranews/cms/news/rss/actionRss.do"


def _make_doc_id(url: str) -> str:
    """URL 기반 고유 ID 생성."""
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"kotra_{h}"


def _parse_date(entry) -> str:
    """RSS entry에서 YYYYMM 형식 추출."""
    published = entry.get("published", "") or entry.get("updated", "")
    if not published:
        return datetime.now().strftime("%Y%m")
    # feedparser의 published_parsed 우선
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return f"{parsed.tm_year:04d}{parsed.tm_mon:02d}"
        except (AttributeError, TypeError):
            pass
    # 문자열 파싱 시도
    m = re.search(r"(\d{4})-?(\d{2})", published)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return datetime.now().strftime("%Y%m")


def fetch_kotra_news(max_items: int = 20) -> list[dict]:
    """KOTRA 해외시장뉴스 RSS를 수집하여 fetcher.py 호환 dict 리스트로 반환.

    feedparser 미설치 시 빈 리스트 반환.

    Returns:
        [{"doc_id", "title", "url", "issue_yyyymm", "category"}, ...]
    """
    if feedparser is None:
        print("[extra_sources] feedparser 미설치 — pip install feedparser")
        return []

    try:
        feed = feedparser.parse(_KOTRA_RSS_URL)
    except Exception as e:
        print(f"[extra_sources] KOTRA RSS 수집 오류: {e}")
        return []

    results = []
    for entry in feed.entries[:max_items]:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue

        results.append({
            "doc_id": _make_doc_id(link),
            "title": title,
            "url": link,
            "issue_yyyymm": _parse_date(entry),
            "category": "KOTRA",
        })

    print(f"[extra_sources] KOTRA RSS {len(results)}건 수집 완료")
    return results
