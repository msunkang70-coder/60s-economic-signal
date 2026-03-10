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
from datetime import datetime

try:
    import feedparser
except ImportError:
    feedparser = None  # type: ignore[assignment]

# ── 산업부 RSS URL ────────────────────────────────────────────
_MOTIE_RSS_URL = "https://www.motie.go.kr/rss/pressRelease.do"

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
    if feedparser is None:
        print("[motie_source] feedparser 미설치 — pip install feedparser")
        return []

    # RSS 수집
    try:
        import urllib.request
        req = urllib.request.Request(
            _MOTIE_RSS_URL,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        feed = feedparser.parse(raw)
    except Exception as e:
        print(f"[motie_source] 산업부 RSS 수집 실패: {e}")
        return []

    if not feed.entries:
        print("[motie_source] 산업부 RSS 항목 없음")
        return []

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
