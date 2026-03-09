"""
core/extra_sources.py
멀티 소스 기사 수집 — KOTRA 해외시장뉴스 + 연합뉴스 경제 RSS 통합

KDI 나라경제 기사 + 외부 소스(KOTRA, 연합뉴스 경제)를 함께 수집하고
중복을 제거하여 통합 기사 목록을 반환한다.

fetcher.py의 fetch_list()와 동일한 dict 형식으로 반환:
  {"doc_id", "title", "url", "issue_yyyymm", "category"}
"""

import hashlib
import re
from datetime import datetime
from typing import Optional

try:
    import feedparser
except ImportError:
    feedparser = None  # type: ignore[assignment]

# ── RSS 소스 목록 (우선순위 순) ─────────────────────────────
# KOTRA RSS가 2024년 이후 서비스 중단됨 → fallback 소스 추가
_RSS_SOURCES = [
    {
        "name": "KOTRA",
        "url": (
            "https://dream.kotra.or.kr/kotranews/cms/common/popup/"
            "actionKotraRss.do?MENU_ID=410"
        ),
        "category": "KOTRA",
    },
    {
        "name": "연합뉴스경제",
        "url": "https://www.yna.co.kr/rss/economy.xml",
        "category": "연합뉴스경제",
    },
    {
        "name": "한국경제",
        "url": "https://www.hankyung.com/feed/economy",
        "category": "한국경제",
    },
]


def _make_doc_id(source: str, url: str) -> str:
    """소스명 + URL 기반 고유 ID 생성."""
    prefix = re.sub(r"[^a-z]", "", source.lower())[:6] or "ext"
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"{prefix}_{h}"


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


def _fetch_rss(rss_url: str, source_name: str, category: str,
               max_items: int = 10) -> list[dict]:
    """
    단일 RSS 피드를 파싱하여 기사 목록을 반환한다.
    파싱 실패 시 빈 리스트를 반환한다 (파이프라인 중단 없음).

    Returns:
        [{"doc_id", "title", "url", "issue_yyyymm", "category",
          "date", "summary", "source"}, ...]
    """
    if feedparser is None:
        print("[extra_sources] feedparser 미설치 — pip install feedparser")
        return []

    try:
        feed = feedparser.parse(rss_url)
    except Exception as e:
        print(f"[extra_sources] {source_name} RSS 파싱 실패: {e}")
        return []

    if not feed.entries:
        print(f"[extra_sources] {source_name} RSS 항목 없음 (피드 비활성 가능)")
        return []

    articles = []
    for entry in feed.entries[:max_items]:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue

        # date parsing
        published = entry.get("published", "") or entry.get("updated", "")
        summary = entry.get("summary", "").strip()
        # Clean HTML from summary
        summary = re.sub(r"<[^>]+>", "", summary).strip()

        articles.append({
            "doc_id": _make_doc_id(source_name, link),
            "title": title,
            "url": link,
            "issue_yyyymm": _parse_date(entry),
            "category": category,
            "date": published,
            "summary": summary[:300] if summary else "",
            "source": source_name,
        })

    print(f"[extra_sources] {source_name} RSS {len(articles)}건 수집 완료")
    return articles


def fetch_kotra_rss(max_items: int = 10) -> list[dict]:
    """
    KOTRA 해외시장뉴스 RSS를 파싱하여 기사 목록을 반환한다.
    KOTRA RSS가 비활성인 경우 연합뉴스 경제 RSS로 fallback한다.

    Returns:
        [{"doc_id", "title", "url", "issue_yyyymm", "category",
          "date", "summary", "source"}, ...]
    """
    # RSS 소스를 순서대로 시도 — 첫 번째 성공 소스 사용
    for src in _RSS_SOURCES:
        articles = _fetch_rss(
            rss_url=src["url"],
            source_name=src["name"],
            category=src["category"],
            max_items=max_items,
        )
        if articles:
            return articles

    print("[extra_sources] 모든 외부 RSS 소스 수집 실패")
    return []


# ── 레거시 호환 별칭 ──────────────────────────────────────
fetch_kotra_news = fetch_kotra_rss


def _title_key(title: str) -> str:
    """제목 앞 20자에서 공백·특수문자 제거한 비교 키를 반환."""
    cleaned = re.sub(r"[^\w가-힣a-zA-Z0-9]", "", title[:20])
    return cleaned.lower()


def merge_articles(
    kdi_articles: list[dict],
    extra_articles: list[dict],
) -> list[dict]:
    """
    KDI 기사와 추가 소스(KOTRA 등) 기사를 합치고 중복을 제거한다.

    중복 판정: 제목 앞 20자 정규화 비교 (공백·특수문자 제거)
    KDI 기사가 우선 — 중복 시 KDI 기사를 유지한다.

    Args:
        kdi_articles: [{"title", "url", ...}, ...]
        extra_articles: [{"title", "url", "source", ...}, ...]

    Returns:
        통합된 기사 목록 (KDI 먼저, 이후 추가 소스 순)
    """
    seen_keys: set = set()
    merged: list = []

    # KDI 기사 우선 추가
    for art in kdi_articles:
        key = _title_key(art.get("title", ""))
        if key and key not in seen_keys:
            seen_keys.add(key)
            art.setdefault("source", "KDI")
            merged.append(art)

    # 추가 소스 기사 (중복 제거)
    for art in extra_articles:
        key = _title_key(art.get("title", ""))
        if key and key not in seen_keys:
            seen_keys.add(key)
            merged.append(art)

    return merged


def fetch_all_sources(kdi_articles: list[dict], kotra_max: int = 10) -> list[dict]:
    """
    모든 소스에서 기사를 수집하고 통합하여 반환한다.

    Args:
        kdi_articles: 기존 KDI 기사 목록
        kotra_max: 외부 소스 기사 최대 수집 건수

    Returns:
        통합 기사 목록
    """
    extra = fetch_kotra_rss(max_items=kotra_max)
    src_name = extra[0]["source"] if extra else "외부"
    print(f"[extra_sources] KDI {len(kdi_articles)}건 + {src_name} {len(extra)}건 수집")

    merged = merge_articles(kdi_articles, extra)
    print(f"[extra_sources] 중복 제거 후 총 {len(merged)}건")

    return merged
