"""
core/extra_sources.py
멀티 소스 기사 수집 — 연합뉴스·매일경제·한국경제 RSS 통합

KDI 나라경제 기사 + 외부 뉴스 RSS를 함께 수집하고
중복을 제거하여 통합 기사 목록을 반환한다.

★ v2 — 데이터 소스 안정화:
  - KOTRA RSS(2024년 이후 중단) 제거, 안정적 RSS 소스 3단계로 재정의
  - fetch_kotra_rss → fetch_news_rss 함수명 변경 (하위 호환 alias 유지)
  - 소스별 timeout=5, 실패 시 다음 소스 자동 전환
  - industry_key 기반 키워드 필터 + difflib 제목 유사도 중복 제거
  - fetch_all_sources()에 source_stats 반환 추가

fetcher.py의 fetch_list()와 동일한 dict 형식으로 반환:
  {"doc_id", "title", "url", "issue_yyyymm", "category"}
"""

import difflib
import hashlib
import re
from datetime import datetime

try:
    import feedparser
except ImportError:
    feedparser = None  # type: ignore[assignment]

# ── RSS 소스 목록 (우선순위 순) ─────────────────────────────
_RSS_SOURCES = [
    {
        "name": "연합뉴스경제",
        "url": "https://www.yonhapnewstv.co.kr/category/news/economy/feed/",
        "category": "연합뉴스경제",
        "priority": "PRIMARY",
    },
    {
        "name": "매일경제",
        "url": "https://www.mk.co.kr/rss/40300001/",
        "category": "매일경제",
        "priority": "SECONDARY",
    },
    {
        "name": "한국경제",
        "url": "https://feeds.hankyung.com/article/economy.xml",
        "category": "한국경제",
        "priority": "TERTIARY",
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
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return f"{parsed.tm_year:04d}{parsed.tm_mon:02d}"
        except (AttributeError, TypeError):
            pass
    m = re.search(r"(\d{4})-?(\d{2})", published)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return datetime.now().strftime("%Y%m")


def _parse_sort_key(entry) -> str:
    """RSS entry에서 정렬용 ISO 날짜 문자열 추출 (최신순 정렬용)."""
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


def _fetch_rss(rss_url: str, source_name: str, category: str,
               max_items: int = 10) -> list[dict]:
    """
    단일 RSS 피드를 파싱하여 기사 목록을 반환한다.
    timeout=5초, 파싱 실패 시 빈 리스트를 반환한다.
    """
    if feedparser is None:
        print("[extra_sources] feedparser 미설치 — pip install feedparser")
        return []

    try:
        import urllib.request
        req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        feed = feedparser.parse(raw)
    except Exception as e:
        print(f"[extra_sources] {source_name} RSS 수집 실패 (timeout=5s): {e}")
        return []

    if not feed.entries:
        print(f"[extra_sources] {source_name} RSS 항목 없음 (피드 비활성 가능)")
        return []

    # 최신 기사 우선 정렬
    sorted_entries = sorted(
        feed.entries, key=lambda e: _parse_sort_key(e), reverse=True,
    )

    articles = []
    for entry in sorted_entries[:max_items]:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue

        published = entry.get("published", "") or entry.get("updated", "")
        summary = entry.get("summary", "").strip()
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


def _is_duplicate_title(title: str, existing_titles: list[str], threshold: float = 0.7) -> bool:
    """difflib 기반 제목 유사도 비교. threshold 이상이면 중복으로 판정."""
    for existing in existing_titles:
        ratio = difflib.SequenceMatcher(None, title, existing).ratio()
        if ratio >= threshold:
            return True
    return False


def _filter_by_industry(articles: list[dict], industry_key: str = "") -> list[dict]:
    """industry_key 기반 키워드 필터링. 키워드가 없으면 전체 반환."""
    if not industry_key or industry_key == "일반":
        return articles

    try:
        from core.industry_config import get_profile
        profile = get_profile(industry_key)
        keywords = profile.get("keywords", [])
    except (ImportError, Exception):
        return articles

    if not keywords:
        return articles

    filtered = []
    for art in articles:
        text = art.get("title", "") + " " + art.get("summary", "")
        if any(kw in text for kw in keywords):
            filtered.append(art)

    # 필터 결과가 너무 적으면 전체 반환 (최소 2건 보장)
    return filtered if len(filtered) >= 2 else articles


def _deduplicate_articles(articles: list[dict]) -> list[dict]:
    """difflib 기반 제목 유사도 > 0.7 중복 제거."""
    result = []
    seen_titles: list[str] = []
    for art in articles:
        title = art.get("title", "")
        if not _is_duplicate_title(title, seen_titles):
            result.append(art)
            seen_titles.append(title)
    return result


def fetch_news_rss(max_items: int = 10, industry_key: str = "") -> list[dict]:
    """
    뉴스 RSS를 우선순위 순으로 수집한다.

    우선순위:
      PRIMARY:   연합뉴스 경제 RSS
      SECONDARY: 매일경제 RSS
      TERTIARY:  한국경제 RSS
      FALLBACK:  모두 실패 시 빈 리스트

    각 소스 timeout=5초, 실패 시 다음 소스로 자동 전환.
    industry_key가 주어지면 키워드 필터 적용.
    중복 제거: difflib 제목 유사도 > 0.7
    """
    sources_used: list[str] = []

    for src in _RSS_SOURCES:
        articles = _fetch_rss(
            rss_url=src["url"],
            source_name=src["name"],
            category=src["category"],
            max_items=max_items,
        )
        if articles:
            sources_used.append(src["name"])
            # 산업 키워드 필터
            articles = _filter_by_industry(articles, industry_key)
            # 중복 제거
            articles = _deduplicate_articles(articles)
            return articles

    print("[extra_sources] 모든 외부 RSS 소스 수집 실패")
    return []


# ── 하위 호환 alias ──────────────────────────────────────────
fetch_kotra_rss = fetch_news_rss
fetch_kotra_news = fetch_news_rss


def _title_key(title: str) -> str:
    """제목 앞 20자에서 공백·특수문자 제거한 비교 키를 반환."""
    cleaned = re.sub(r"[^\w가-힣a-zA-Z0-9]", "", title[:20])
    return cleaned.lower()


def merge_articles(
    kdi_articles: list[dict],
    extra_articles: list[dict],
) -> list[dict]:
    """
    KDI 기사와 추가 소스 기사를 합치고 중복을 제거한다.

    중복 판정: difflib 제목 유사도 > 0.7
    KDI 기사가 우선 — 중복 시 KDI 기사를 유지한다.
    """
    merged: list = []
    seen_titles: list[str] = []

    # KDI 기사 우선 추가
    for art in kdi_articles:
        title = art.get("title", "")
        if title and not _is_duplicate_title(title, seen_titles):
            seen_titles.append(title)
            art.setdefault("source", "KDI")
            merged.append(art)

    # 추가 소스 기사 (중복 제거)
    for art in extra_articles:
        title = art.get("title", "")
        if title and not _is_duplicate_title(title, seen_titles):
            seen_titles.append(title)
            merged.append(art)

    return merged


def fetch_all_sources(
    kdi_articles: list[dict],
    kotra_max: int = 10,
    industry_key: str = "",
) -> tuple[list[dict], dict]:
    """
    모든 소스에서 기사를 수집하고 통합하여 반환한다.

    소스 통합 순서: KDI → 뉴스 RSS → 산업부 보도자료
    각 소스는 독립적으로 실패해도 파이프라인 중단 없음.

    Args:
        kdi_articles: 기존 KDI 기사 목록
        kotra_max: 외부 소스 기사 최대 수집 건수
        industry_key: 산업 키워드 필터용 키

    Returns:
        (통합 기사 목록, source_stats)
        source_stats: {"total": int, "kdi": int, "rss": int, "motie": int, "sources_used": list[str]}
    """
    sources_used: list[str] = []
    all_rss: list[dict] = []
    motie_count = 0

    # ── 뉴스 RSS 수집 ────────────────────────────────────────
    for src in _RSS_SOURCES:
        articles = _fetch_rss(
            rss_url=src["url"],
            source_name=src["name"],
            category=src["category"],
            max_items=kotra_max,
        )
        if articles:
            sources_used.append(src["name"])
            all_rss.extend(articles)
            break  # 첫 번째 성공 소스만 사용

    # 산업 키워드 필터
    if all_rss:
        all_rss = _filter_by_industry(all_rss, industry_key)

    # ── 산업부 보도자료 수집 ──────────────────────────────────
    motie_articles: list[dict] = []
    try:
        from core.motie_source import fetch_motie_news
        motie_articles = fetch_motie_news(industry_key=industry_key, max_items=5)
        if motie_articles:
            sources_used.append("산업부")
            motie_count = len(motie_articles)
            all_rss.extend(motie_articles)
    except Exception as e:
        print(f"[extra_sources] 산업부 보도자료 수집 실패: {e}")

    # 중복 제거
    all_rss = _deduplicate_articles(all_rss)

    src_names = ", ".join(sources_used) if sources_used else "없음"
    print(f"[extra_sources] KDI {len(kdi_articles)}건 + 외부 {len(all_rss)}건 ({src_names})")

    merged = merge_articles(kdi_articles, all_rss)
    print(f"[extra_sources] 중복 제거 후 총 {len(merged)}건")

    source_stats = {
        "total": len(merged),
        "kdi": len(kdi_articles),
        "rss": len(all_rss) - motie_count,
        "motie": motie_count,
        "sources_used": sources_used,
    }

    return merged, source_stats
