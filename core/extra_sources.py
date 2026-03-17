"""
core/extra_sources.py
멀티 소스 기사 수집 — 연합뉴스·매일경제·한국경제 RSS + 산업별 전문 RSS 통합

KDI 나라경제 기사 + 외부 뉴스 RSS + 산업별 전문 미디어를 함께 수집하고
중복을 제거하여 통합 기사 목록을 반환한다.

★ v3 — 멀티소스 기사 수집 개선:
  - 제목 중복 임계값 0.7 → 0.85 (과도한 필터링 방지)
  - 동일 소스 내에서만 중복 판정 (교차 소스 기사 유지)
  - 본문 첫 2문장 유사도 비교 추가 (threshold 0.7)
  - _SOURCE_PRIORITY 상수 + source_priority 필드 추가
  - 산업별 소스 라우팅 (반도체→KITA, 화학→MOTIE 등)

★ v4 — 산업별 전문 데이터 소스 확보 (Phase 7):
  - _INDUSTRY_RSS_SOURCES: 8개 산업별 전문 미디어 RSS + Google News 산업 검색
  - fetch_industry_rss(): 산업 전용 RSS 수집 함수
  - fetch_all_sources()에 산업별 전문 소스 통합
  - 산업별 소스 라우팅에 "industry_rss" 추가

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

# ── 소스 우선순위 상수 (높을수록 신뢰도 높음) ─────────────
_SOURCE_PRIORITY: dict[str, int] = {
    "kdi": 100,
    "산업부": 90,
    "kita": 85,
    "코트라": 83,                  # V16.1: KOTRA 해외시장뉴스 (KITA RSS 불안정 대체)
    "industry_rss": 80,           # V4: 산업별 전문 미디어
    "KITA": 83,                   # V16.1: fetch_kita_news() 반환 source 이름과 일치
    "연합뉴스경제": 70,
    "google_news_industry": 55,   # Fix F: Google News 우선순위 하향 (JS 리디렉션, 본문 추출 불가)
    "mk": 50,
    "hankyung": 40,
}

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
        # feeds.hankyung.com 서브도메인 소멸 (2026-03-17 확인, DNS getaddrinfo failed)
        # 대체: www.hankyung.com/feed/economy (200 OK, 50건, 안정)
        "url": "https://www.hankyung.com/feed/economy",
        "category": "한국경제",
        "priority": "TERTIARY",
    },
]

# ── 산업별 전문 RSS 소스 (V4: Phase 7) ──────────────────
# 각 산업에 대해:
#   1) 전문 미디어 RSS (직접 피드)
#   2) Google News 산업 검색 RSS (항상 동작하는 백업)
# Google News RSS: https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko

_INDUSTRY_RSS_SOURCES: dict[str, list[dict]] = {
    "반도체": [
        {
            "name": "전자신문_반도체",
            "url": "https://rss.etnews.com/Section902.xml",
            "category": "전자신문",
            "source_type": "industry_rss",
        },
        {
            "name": "Google_반도체",
            "url": "https://news.google.com/rss/search?q=반도체+수출+OR+파운드리+OR+HBM+OR+AI반도체&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
    ],
    "자동차": [
        {
            "name": "자동차수출",
            "url": "https://news.google.com/rss/search?q=자동차+수출+관세+OR+완성차+수출+OR+현대차+수출&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
        {
            "name": "자동차부품공급망",
            "url": "https://news.google.com/rss/search?q=자동차+부품+공급망+OR+자동차+관세+영향+OR+완성차+부품+수출&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
        {
            "name": "전기차수출",
            "url": "https://news.google.com/rss/search?q=전기차+수출+OR+EV+수출+OR+전기차+관세+OR+미국+전기차&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
        {
            "name": "자동차산업동향",
            "url": "https://news.google.com/rss/search?q=현대차+OR+기아차+OR+GM한국+수출+OR+자동차+무역수지&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
    ],
    "화학": [
        {
            "name": "화학저널",
            "url": "https://news.google.com/rss/search?q=석유화학+OR+나프타+OR+에틸렌+OR+정밀화학+수출&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
        {
            "name": "케미컬뉴스",
            "url": "https://news.google.com/rss/search?q=LG화학+OR+롯데케미칼+OR+CBAM+OR+탄소중립+화학&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
    ],
    "소비재": [
        {
            # V17: 식품음료신문 RSS (직접 피드 — 본문 추출 가능, tier0)
            "name": "식품음료신문",
            "url": "https://www.thinkfood.co.kr/rss/allArticle.xml",
            "category": "식품음료신문",
            "source_type": "industry_rss",
        },
        {
            # V17: 코스인(Cosìn) K-뷰티 전문지 RSS
            "name": "코스인",
            "url": "https://www.cosinkorea.com/rss/allArticle.xml",
            "category": "코스인",
            "source_type": "industry_rss",
        },
        {
            # V17: 연합뉴스 소비재/식품/화장품 직접 검색 (본문 추출 가능)
            "name": "연합_소비재",
            "url": "https://news.google.com/rss/search?q=site:yna.co.kr+(화장품수출+OR+K뷰티+OR+식품수출+OR+K푸드)&hl=ko&gl=KR&ceid=KR:ko",
            "category": "연합뉴스",
            "source_type": "industry_rss",  # yna.co.kr → 본문 추출 가능 → industry_rss 처리
        },
        {
            "name": "뷰티경제",
            "url": "https://news.google.com/rss/search?q=K뷰티+OR+K푸드+OR+화장품수출+OR+식품수출&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
        {
            "name": "소비재산업",
            "url": "https://news.google.com/rss/search?q=소비재+수출+OR+아모레퍼시픽+OR+CJ+OR+농심+해외&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
    ],
    "배터리": [
        {
            "name": "전자신문_배터리",
            "url": "https://rss.etnews.com/Section902.xml",
            "category": "전자신문",
            "source_type": "industry_rss",
        },
        {
            "name": "Google_배터리",
            "url": "https://news.google.com/rss/search?q=2차전지+OR+배터리+수출+OR+리튬+OR+양극재+OR+LFP&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
    ],
    "조선": [
        {
            "name": "Google_조선해양",
            "url": "https://news.google.com/rss/search?q=조선+수주+OR+LNG선+OR+컨테이너선+OR+HD한국조선&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
        {
            "name": "해양한국",
            "url": "https://news.google.com/rss/search?q=조선해양+OR+해양플랜트+OR+친환경선박+OR+수주잔량&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
    ],
    "철강": [
        {
            "name": "Google_철강",
            "url": "https://news.google.com/rss/search?q=철강+수출+OR+포스코+OR+현대제철+OR+철강가격&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
        {
            "name": "스틸데일리",
            "url": "https://news.google.com/rss/search?q=철강금속+OR+CBAM+철강+OR+스테인리스+OR+열연코일&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
    ],
    "일반": [
        {
            "name": "Google_수출",
            "url": "https://news.google.com/rss/search?q=한국+수출+증가+OR+무역수지+OR+수출기업+전략&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
        {
            # V16.1: KOTRA 해외시장뉴스 Google News 보조 RSS — 일반수출 탭 보강
            "name": "KOTRA_해외시장",
            "url": "https://news.google.com/rss/search?q=KOTRA+해외시장+OR+수출상담+OR+FTA+수출+OR+통상규제+대응&hl=ko&gl=KR&ceid=KR:ko",
            "category": "Google뉴스",
            "source_type": "google_news_industry",
        },
    ],
    # V16.1: 소비재 탭 KOTRA 소스 추가 (기존 소비재는 아래에 유지)
}

# ── 산업별 소스 라우팅 (우선 수집 소스) ────────────────────
# V16.1: "소비재"·"일반" 탭에 "코트라" 추가 — KITA RSS 불안정 대체 소스
# V16.3: "배터리" 탭에 "코트라" 추가 — Google_배터리 snippet 대체 (KOTRA 풀바디 기사 유입)
_INDUSTRY_SOURCE_ROUTING: dict[str, list[str]] = {
    "반도체": ["industry_rss", "kita", "연합뉴스경제", "산업부"],
    "자동차": ["industry_rss", "산업부", "연합뉴스경제", "kita"],
    "배터리": ["industry_rss", "산업부", "kita", "연합뉴스경제", "코트라"],  # V16.3: 코트라 추가
    "화학": ["industry_rss", "산업부", "연합뉴스경제"],
    "소비재": ["industry_rss", "산업부", "kdi", "연합뉴스경제", "코트라"],  # V17: 산업부 추가 (소비재 수출 통상 보도자료 활용)
    "조선": ["industry_rss", "kita", "산업부", "연합뉴스경제"],
    "철강": ["industry_rss", "kita", "산업부", "연합뉴스경제"],
    "일반": ["industry_rss", "연합뉴스경제", "산업부", "코트라"],  # V16.1: 코트라 추가
}


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


# ── T-NEW-01: RSS 전문 수집 헬퍼 ──────────────────────────────────────
def _fetch_full_text_safe(url: str, max_chars: int = 2000) -> str:
    """기사 전문 수집 (실패 시 빈 문자열 반환, extra_sources 전용)."""
    try:
        from core.fetcher import fetch_article_text
        text = fetch_article_text(url, max_chars=max_chars)
        return text if isinstance(text, str) else ""
    except Exception:
        return ""


def _enrich_with_full_text(articles: list[dict], max_workers: int = 3) -> list[dict]:
    """
    T-NEW-01: RSS 기사 목록의 'body' 필드를 전문 수집으로 보강.

    - ThreadPoolExecutor (max_workers=3) 병렬 수집
    - 전체 타임아웃 15초, 개별 기사 응답 후 결과 수집
    - 100자 이상 수집 성공 시에만 body 필드 추가
    - 실패/타임아웃 시 기존 summary 필드(RSS 스니펫) 유지
    """
    if not articles:
        return articles

    from concurrent.futures import ThreadPoolExecutor, wait as _cf_wait, ALL_COMPLETED

    # http(s) URL이 있는 기사만 대상
    targets = [(art["doc_id"], art["url"]) for art in articles
               if art.get("url", "").startswith("http")]
    if not targets:
        return articles

    full_texts: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_full_text_safe, url): doc_id for doc_id, url in targets}
        done, _ = _cf_wait(futures, timeout=15, return_when=ALL_COMPLETED)
        for fut in done:
            doc_id = futures[fut]
            try:
                text = fut.result(timeout=1)
                if text:
                    full_texts[doc_id] = text
            except Exception:
                pass

    enriched = 0
    for art in articles:
        text = full_texts.get(art["doc_id"], "")
        if len(text) >= 100:   # LLM 최소 임계값 이상만 저장
            art["body"] = text
            enriched += 1

    print(f"[extra_sources] T-NEW-01 전문 보강: {enriched}/{len(articles)}건")
    return articles


def _is_google_news_url(url: str) -> bool:
    """Google News 중간 리디렉션 URL 여부 판정.

    Google News RSS 링크는 news.google.com 도메인의 리디렉션 URL로,
    JavaScript 기반 리디렉션을 사용하므로 서버 사이드 fetch로 원문 추출 불가.
    이런 URL은 fetch 시도 자체를 건너뛰고 RSS 스니펫을 본문으로 활용한다.

    V12-perf: Google News 기사 본문 추출 fail-fast 핵심 조건
    """
    return bool(url and "news.google.com" in url)


def _fetch_rss(rss_url: str, source_name: str, category: str,
               max_items: int = 10) -> list[dict]:
    """
    단일 RSS 피드를 파싱하여 기사 목록을 반환한다.
    timeout=5초, 파싱 실패 시 빈 리스트를 반환한다.
    T-NEW-01: 전문 보강(_enrich_with_full_text) — Google News URL 제외.
    V12-perf: Google News URL은 no_fetch=True로 즉시 마킹, enrich 대상에서 제외.
    """
    if feedparser is None:
        print("[extra_sources] feedparser 미설치 - pip install feedparser")
        return []

    try:
        import urllib.request
        import urllib.parse
        # V11: 한국어 URL 인코딩 처리 (Google News 등)
        _encoded_url = urllib.parse.quote(rss_url, safe=':/?&=+#')
        req = urllib.request.Request(_encoded_url, headers={"User-Agent": "Mozilla/5.0"})
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

        # V12-perf: Google News URL은 JavaScript 리디렉션으로 서버 사이드 fetch 불가
        # no_fetch=True + body=RSS스니펫 으로 즉시 처리, 불필요한 HTTP 시도 완전 차단
        _is_google = _is_google_news_url(link)
        _art: dict = {
            "doc_id": _make_doc_id(source_name, link),
            "title": title,
            "url": link,
            "issue_yyyymm": _parse_date(entry),
            "category": category,
            "date": published,
            "summary": summary[:300] if summary else "",
            "source": source_name,
            "source_priority": _SOURCE_PRIORITY.get(source_name, 30),
        }
        if _is_google:
            # RSS 스니펫을 body로 활용 — fetch 파이프라인 전체 생략
            _art["no_fetch"] = True
            _art["body"] = summary[:500] if summary else title
            _art["_google_news"] = True  # 진단용 플래그

        articles.append(_art)

    _google_cnt = sum(1 for a in articles if a.get("_google_news"))
    _normal_cnt = len(articles) - _google_cnt
    print(
        f"[extra_sources] {source_name} RSS {len(articles)}건 수집 완료 "
        f"(Google News={_google_cnt}건 no_fetch, 일반={_normal_cnt}건)"
    )

    # T-NEW-01: 전문 보강 — Google News URL 제외 (V12-perf)
    # Google News는 no_fetch=True이므로 enrich 대상에서 제외
    normal_articles = [a for a in articles if not a.get("no_fetch")]
    if normal_articles:
        enriched = _enrich_with_full_text(normal_articles, max_workers=3)
        # enriched 결과를 원래 articles에 병합 (no_fetch 기사는 그대로 유지)
        enriched_by_id = {a["doc_id"]: a for a in enriched}
        articles = [enriched_by_id.get(a["doc_id"], a) for a in articles]

    return articles


def _normalize_title(title: str) -> str:
    """출처 태그·특수문자 접미사를 제거하여 정규화."""
    # 대시/구분자 이후 출처 접미사 제거 (예: "— KDI 원문", "| 최신판")
    title = re.sub(r"\s*[—\-–|·]\s*.{1,15}$", "", title)
    # 말미 출처·버전 키워드 제거
    title = re.sub(r"\s+(KDI|원문|최신판|업데이트|속보|전문)$", "", title)
    return title.strip()


def _is_duplicate_title(title: str, existing_titles: list[str], threshold: float = 0.85) -> bool:
    """정규화 후 difflib 유사도 비교. threshold 이상이면 중복으로 판정."""
    norm = _normalize_title(title)
    for existing in existing_titles:
        norm_ex = _normalize_title(existing)
        ratio = difflib.SequenceMatcher(None, norm, norm_ex).ratio()
        if ratio >= threshold:
            return True
    return False


def _extract_first_sentences(text: str, n: int = 2) -> str:
    """본문에서 첫 n문장 추출 (중복 비교용)."""
    if not text:
        return ""
    sentences = re.split(r"[.!?。]\s+", text.strip())
    return " ".join(sentences[:n]).strip()


def _is_duplicate_body(body: str, existing_bodies: list[str], threshold: float = 0.7) -> bool:
    """본문 첫 2문장 유사도 비교. threshold 이상이면 중복으로 판정."""
    snippet = _extract_first_sentences(body)
    if not snippet or len(snippet) < 20:
        return False  # 본문이 너무 짧으면 비교 불가
    for existing in existing_bodies:
        if not existing or len(existing) < 20:
            continue
        ratio = difflib.SequenceMatcher(None, snippet, existing).ratio()
        if ratio >= threshold:
            return True
    return False


# ── T-NEW-02: 수출 관련성 스코어링 ────────────────────────────────────
# blacklist 오탐(false positive) 복구 + blacklist 미탐(false negative) 보완
# 운영 방식:
#   - score <= _RELEVANCE_NEG_THRESHOLD  : 즉시 필터 (blacklist 무관)
#   - score >= _RELEVANCE_RESCUE_THRESHOLD AND blacklist 히트: 복구(rescue)
_EXPORT_SCORE_MAP: dict[str, int] = {
    # 수출·무역·국제경제 (+3)
    '수출': +3, '관세': +3, '무역': +3, '수입': +3,
    '통상': +3, '제재': +3, 'FTA': +3,
    # 글로벌·외환 (+2)
    '글로벌': +2, '해외': +2, '환율': +2, '외환': +2, '달러': +2,
    # 주요 수출 산업 (+2)
    '반도체': +2, '자동차': +2, '화학': +2, '배터리': +2,
    '조선': +2, '철강': +2, '원자재': +2, '공급망': +2,
    '석유화학': +2, '정밀화학': +2, '의약품': +2, '바이오': +2,
    '소비재': +2, '식품': +2,
    # 에너지·원자재 (+2)
    '원유': +2, '에너지': +2, '가스': +2, '비축': +2, 'LNG': +2,
    # 거시경제 (+2)
    'GDP': +2, '물가': +2, '기준금리': +2, '경상수지': +2, '무역수지': +2,
    # 기업·산업 일반 (+1)
    '기업': +1, '산업': +1, '생산': +1, '투자': +1,
    # ── 비관련 신호 (-)
    '팝업': -3, '데뷔': -4, '패션쇼': -3, '론칭': -2,
    '아이돌': -4, '연예': -3, '콘서트': -3, '공연': -2,
    '신메뉴': -3, '맛집': -3, '레시피': -3,
    # T-08: 행사·이벤트 개최 신호 (-1) — 단독으론 약한 감점, 구제 임계값과 연동
    '개최': -1,
}
_RELEVANCE_NEG_THRESHOLD = -2    # 이하 → 즉시 필터
_RELEVANCE_RESCUE_THRESHOLD = 2  # 이상 → blacklist 오탐 복구


def _score_relevance(title: str) -> int:
    """제목 관련성 점수 산출 (수출/무역 관련성 측정)."""
    score = 0
    for kw, pts in _EXPORT_SCORE_MAP.items():
        if kw in title:
            score += pts
    return score


# V11: 잡기사/광고 필터링 패턴 (전자신문 등 RSS에서 자주 혼입되는 비경제 콘텐츠)
# V12: 행사/증시/지역 행사 패턴 추가 (QA T-03)
# P3: 기업 PR/캠페인/내수이벤트/주가 기사 강화 필터 추가
_JUNK_TITLE_PATTERNS = re.compile(
    r'(?:'
    r'포토|갤러리|사진|영상|동영상|라이브|'
    r'\[AD\]|\[광고\]|\[후원\]|스폰서|SPONSORED|'
    r'인사|부고|부음|결혼|'
    r'날씨|운세|로또|복권|'
    r'채용|구인|입사|'
    r'\[기자수첩\]|\[칼럼\]|\[사설\]|\[기고\]|'
    r'이벤트|경품|할인|쿠폰|프로모션|'
    r'TV|드라마|예능|연예|아이돌|방송|'
    # V12: 행사/세미나/지역 이벤트
    r'\[행사\]|페스티벌|박람회|세미나|엑스포|전시회|컨퍼런스|포럼\s*개최|개최|'
    r'지역\s*행사|대전[·\s]*충청|부산\s*행사|광주\s*행사|대구\s*행사|'
    r'인천\s*행사|울산\s*행사|전남\s*행사|경북\s*행사|강원\s*행사|'
    # V12: 주식/증시 기사
    r'핫종목|주가\s*강세|주가\s*상승률|코스피\s*종목|코스닥\s*종목|'
    r'증시\s*마감|장중\s*흐름|오늘의\s*주식|상한가|하한가|급등주|'
    r'주식\s*추천|종목\s*추천|투자\s*종목|'
    # P3: 기업 PR / 캠페인 / 내수 이벤트
    r'출시\s*기념|론칭\s*행사|신제품\s*출시|매장\s*오픈|지점\s*오픈|'
    r'팝업\s*스토어|팝업스토어|한정\s*판매|한정판\s*출시|'
    r'캠페인\s*시작|사회공헌|CSR\s*활동|봉사활동|기부\s*행사|'
    r'내수\s*판촉|국내\s*행사|국내\s*이벤트|국내\s*출시|'
    # P3: 기업 인사발령 / 경영진 PR
    r'대표이사\s*취임|CEO\s*선임|신임\s*대표|대표\s*교체|임원\s*인사|'
    r'정기\s*주총|주주총회|배당금\s*결정|자사주\s*매입|유상증자|'
    # P3: 증시/주가 단독 기사 (수출 맥락 없는 순수 주가 기사)
    r'주가\s*급등|주가\s*급락|주가\s*폭등|주가\s*폭락|주가\s*전망|'
    r'목표주가|증권사\s*리포트|애널리스트\s*추천|'
    # TODO-2: 편의점·특화매장·국내 개점 (수출/해외 rescue 메커니즘으로 오탐 방지)
    r'편의점\s*(?:오픈|신규|개점)|편의점\s*매장\s*(?:오픈|개점)|'
    r'신규\s*개점|정식\s*개점|그랜드\s*오픈\s*(?:행사|기념)?|오픈\s*기념\s*(?:행사|이벤트|할인)|'
    r'특화\s*(?:점|매장|스토어)|플래그십\s*스토어|'
    r'명동(?:점|매장|스토어|지점)|홍대(?:점|매장)|신촌(?:점|매장)|강남(?:점|매장)|'
    r'이마트\d*\s*(?:오픈|개점)|세븐일레븐\s*(?:오픈|개점)|GS25\s*(?:오픈|개점)|CU\s*(?:오픈|개점)'
    r')',
    re.IGNORECASE
)

# V12: 주식·증시 관련 키워드 — 제목에 포함 시 제거 (T-03)
_STOCK_MARKET_KEYWORDS = frozenset({
    "주가", "증시", "코스피", "코스닥", "핫종목", "증권사",
    "주주", "주식", "시가총액", "상장", "배당", "공매도",
    "주가지수", "ETF", "펀드", "종목", "장세",
})


def _filter_junk_articles(articles: list[dict]) -> list[dict]:
    """
    V11/V12: 잡기사/광고/비경제/증시 콘텐츠 필터링.
    T-NEW-02: 관련성 스코어링 통합.
      - score <= NEG_THRESHOLD → 즉시 필터 (blacklist 무관)
      - score >= RESCUE_THRESHOLD AND blacklist 히트 → 복구 (false positive 방지)
    """
    result = []
    for art in articles:
        title = art.get("title", "")

        # T-NEW-02 Step 1: 관련성 점수 산출
        _rel_score = _score_relevance(title)

        # T-NEW-02 Step 2: 강한 부정 신호 → 즉시 필터 (blacklist 무관)
        if _rel_score <= _RELEVANCE_NEG_THRESHOLD:
            print(f"[extra_sources] [DROP] 부정점수 필터(score={_rel_score}): '{title[:40]}'")
            continue

        # T-NEW-02 Step 3: 패턴 기반 필터 (blacklist)
        _pattern_hit = bool(_JUNK_TITLE_PATTERNS.search(title))
        if _pattern_hit:
            if _rel_score >= _RELEVANCE_RESCUE_THRESHOLD:
                # 수출/무역 키워드 있음 → blacklist 오탐 복구
                print(f"[extra_sources] [RESCUE] 오탐 복구(score={_rel_score}): '{title[:40]}'")
            else:
                print(f"[extra_sources] [DROP] 잡기사 필터(패턴,score={_rel_score}): '{title[:40]}'")
                continue

        # V12: 주식·증시 키워드 필터 — 단독 종목 분석 기사 제거
        title_words = set(re.findall(r'[가-힣A-Z]{2,}', title))
        if title_words & _STOCK_MARKET_KEYWORDS:
            _export_kw = {"수출", "산업", "생산", "무역", "원자재", "공급망", "수주"}
            if not (title_words & _export_kw):
                print(f"[extra_sources] [DROP] 증시기사 필터링: '{title[:40]}'")
                continue

        # 최소 제목 길이
        if len(title.strip()) < 5:
            continue

        result.append(art)
    return result


def _filter_by_industry(articles: list[dict], industry_key: str = "") -> list[dict]:
    """industry_key 기반 키워드 필터링. V11: 잡기사 필터 선적용. 키워드가 없으면 전체 반환."""
    # V11: 잡기사 필터 먼저 적용
    articles = _filter_junk_articles(articles)

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
    """
    V11: 동일 소스 내 제목 유사도 > 0.85 또는 본문 유사도 > 0.7 중복 제거.
    V12 (T-04): 교차 소스 간 제목 유사도 > 0.65 중복 제거 추가.
              같은 날짜 + 같은 산업 맥락에서 중복 기사 제거.
              중복 시 source_priority가 낮은(덜 신뢰도) 기사를 제거.
    """
    result: list[dict] = []
    # 소스별로 seen 목록을 분리하여 같은 소스끼리만 비교
    seen_titles_by_source: dict[str, list[str]] = {}
    seen_bodies_by_source: dict[str, list[str]] = {}

    for art in articles:
        title = art.get("title", "")
        source = art.get("source", "unknown")
        body = art.get("summary", "")

        source_titles = seen_titles_by_source.setdefault(source, [])
        source_bodies = seen_bodies_by_source.setdefault(source, [])

        # 제목 중복 검사 (같은 소스 내에서만, threshold 0.85)
        if _is_duplicate_title(title, source_titles, threshold=0.85):
            print(f"[extra_sources] [DUP] 동일소스 중복 제거: '{title[:40]}'")
            continue
        # 본문 중복 검사 (같은 소스 내에서만)
        body_snippet = _extract_first_sentences(body)
        if _is_duplicate_body(body, source_bodies, threshold=0.7):
            print(f"[extra_sources] [DUP] 동일소스 본문 중복 제거: '{title[:40]}'")
            continue

        result.append(art)
        source_titles.append(title)
        if body_snippet:
            source_bodies.append(body_snippet)

    # V12 (T-04): 교차 소스 중복 제거 (threshold 0.65)
    # 이미 result에 들어온 기사들끼리 교차 비교 후 낮은 priority 제거
    _cross_deduped: list[dict] = []
    _seen_cross_titles: list[tuple[str, int]] = []  # (title, source_priority)

    # source_priority 높은 순 정렬 → 높은 우선순위 기사를 먼저 보존
    result_sorted = sorted(result, key=lambda x: -x.get("source_priority", 30))

    for art in result_sorted:
        title = art.get("title", "")
        s_priority = art.get("source_priority", 30)
        norm_title = _normalize_title(title)

        is_cross_dup = False
        for seen_title, seen_priority in _seen_cross_titles:
            ratio = difflib.SequenceMatcher(None, norm_title, seen_title).ratio()
            if ratio >= 0.65:
                # 이미 더 높은 priority 기사가 있으므로 현 기사를 제거
                print(f"[extra_sources] [DUP] 교차소스 중복 제거(ratio={ratio:.2f}): '{title[:40]}'")
                is_cross_dup = True
                break
        if not is_cross_dup:
            _cross_deduped.append(art)
            _seen_cross_titles.append((_normalize_title(title), s_priority))

    # 원래 순서 복원 (source_priority 기준 정렬 해제)
    _cross_deduped_set = {id(a) for a in _cross_deduped}
    return [a for a in result if id(a) in _cross_deduped_set]


def _clean_google_news_title(title: str) -> str:
    """Google News 제목에서 출처 접미사를 제거한다. (예: ' - 연합뉴스' 제거)"""
    # " - 출처명" 패턴 제거 (Google News RSS 특유)
    title = re.sub(r"\s*-\s*[가-힣A-Za-z0-9\s]{2,20}$", "", title)
    return title.strip()


def fetch_industry_rss(industry_key: str, max_items: int = 8) -> list[dict]:
    """
    산업별 전문 RSS를 수집한다. (V4: Phase 7)

    각 산업에 대해 _INDUSTRY_RSS_SOURCES에 정의된 전문 미디어 RSS와
    Google News 산업별 검색 RSS를 순차적으로 시도한다.
    첫 번째 성공 소스의 결과를 반환하되, Google News는 항상 추가 수집한다.

    Args:
        industry_key: 산업 키 (반도체, 자동차, 화학, 소비재, 배터리, 조선, 철강, 일반)
        max_items: 소스당 최대 수집 건수

    Returns:
        산업 전문 기사 목록 (최대 max_items * 2건)
    """
    sources = _INDUSTRY_RSS_SOURCES.get(industry_key, [])
    if not sources:
        return []

    all_articles: list[dict] = []
    industry_media_found = False

    for src in sources:
        source_type = src.get("source_type", "industry_rss")
        articles = _fetch_rss(
            rss_url=src["url"],
            source_name=src["name"],
            category=src["category"],
            max_items=max_items,
        )

        if not articles:
            continue

        # Google News 제목 정리
        if source_type == "google_news_industry":
            for art in articles:
                art["title"] = _clean_google_news_title(art.get("title", ""))
                art["source"] = src["name"]
                art["source_priority"] = _SOURCE_PRIORITY.get("google_news_industry", 75)

        # 산업 전문 미디어인 경우
        if source_type == "industry_rss":
            for art in articles:
                art["source"] = src["name"]
                art["source_priority"] = _SOURCE_PRIORITY.get("industry_rss", 80)
            industry_media_found = True

        all_articles.extend(articles)

        # 전문 미디어 1개 + Google News 1개면 충분
        if industry_media_found and source_type == "google_news_industry":
            break
        # 전문 미디어 없이 Google News만 있으면 첫 번째 것만 사용
        if not industry_media_found and source_type == "google_news_industry":
            break

    # 산업 키워드 필터 (범용 기사 제거)
    if all_articles:
        all_articles = _filter_by_industry(all_articles, industry_key)

    # 중복 제거
    all_articles = _deduplicate_articles(all_articles)

    print(f"[extra_sources] 산업RSS({industry_key}) {len(all_articles)}건 수집")
    return all_articles


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
    중복 제거: difflib 제목 유사도 > 0.85 (동일 소스 내)
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

    중복 판정: difflib 제목 유사도 > 0.85 (교차 소스 비교)
    KDI 기사가 우선 — 중복 시 KDI 기사를 유지한다.
    최종 정렬: source_priority 내림차순.
    """
    merged: list = []
    seen_titles: list[str] = []

    # KDI 기사 우선 추가
    for art in kdi_articles:
        title = art.get("title", "")
        if title and not _is_duplicate_title(title, seen_titles, threshold=0.85):
            seen_titles.append(title)
            art.setdefault("source", "KDI")
            art.setdefault("source_priority", _SOURCE_PRIORITY.get("kdi", 100))
            merged.append(art)

    # 추가 소스 기사 (중복 제거)
    for art in extra_articles:
        title = art.get("title", "")
        if title and not _is_duplicate_title(title, seen_titles, threshold=0.85):
            seen_titles.append(title)
            merged.append(art)

    # source_priority 내림차순 정렬
    merged.sort(key=lambda a: a.get("source_priority", 0), reverse=True)

    return merged


def _get_routed_sources(industry_key: str) -> list[str]:
    """산업별 우선 수집 소스 목록 반환."""
    return _INDUSTRY_SOURCE_ROUTING.get(industry_key, _INDUSTRY_SOURCE_ROUTING["일반"])


def fetch_all_sources(
    kdi_articles: list[dict],
    kotra_max: int = 10,
    industry_key: str = "",
) -> tuple[list[dict], dict]:
    """
    모든 소스에서 기사를 수집하고 통합하여 반환한다.

    산업별 소스 라우팅에 따라 우선 소스를 선택하고,
    소스 통합 순서: KDI → 뉴스 RSS → 산업부 → KITA
    각 소스는 독립적으로 실패해도 파이프라인 중단 없음.

    Args:
        kdi_articles: 기존 KDI 기사 목록
        kotra_max: 외부 소스 기사 최대 수집 건수
        industry_key: 산업 키워드 필터용 키

    Returns:
        (통합 기사 목록, source_stats)
        source_stats: {"total", "kdi", "rss", "motie", "kita", "sources_used"}
    """
    sources_used: list[str] = []
    all_extra: list[dict] = []
    motie_count = 0
    kita_count = 0
    kotra_count = 0  # V16.1
    industry_rss_count = 0

    # 산업별 라우팅 소스 확인
    routed = _get_routed_sources(industry_key)

    # ── 산업별 전문 RSS 수집 (V4: 최우선 수집) ────────────────
    if "industry_rss" in routed and industry_key:
        try:
            ind_articles = fetch_industry_rss(industry_key, max_items=8)
            if ind_articles:
                industry_rss_count = len(ind_articles)
                sources_used.append(f"산업RSS({industry_key})")
                all_extra.extend(ind_articles)
        except Exception as e:
            print(f"[extra_sources] 산업RSS({industry_key}) 수집 실패: {e}")

    # ── 뉴스 RSS 수집 (라우팅에 연합뉴스가 포함된 경우) ───────
    if "연합뉴스경제" in routed:
        for src in _RSS_SOURCES:
            articles = _fetch_rss(
                rss_url=src["url"],
                source_name=src["name"],
                category=src["category"],
                max_items=kotra_max,
            )
            if articles:
                sources_used.append(src["name"])
                all_extra.extend(articles)
                break  # 첫 번째 성공 RSS 소스만 사용

    # 산업 키워드 필터
    if all_extra:
        all_extra = _filter_by_industry(all_extra, industry_key)

    # ── 산업부 보도자료 수집 (라우팅에 포함된 경우) ────────────
    if "산업부" in routed:
        try:
            from core.motie_source import fetch_motie_news
            motie_articles = fetch_motie_news(industry_key=industry_key, max_items=5)
            if motie_articles:
                # source_priority 필드 추가
                for art in motie_articles:
                    art.setdefault("source_priority", _SOURCE_PRIORITY.get("산업부", 90))
                sources_used.append("산업부")
                motie_count = len(motie_articles)
                all_extra.extend(motie_articles)
        except Exception as e:
            print(f"[extra_sources] 산업부 보도자료 수집 실패: {e}")

    # ── KITA 수출동향 기사 수집 (라우팅에 포함된 경우) ─────────
    if "kita" in routed:
        try:
            from core.kita_source import fetch_kita_export_trend
            kita_data = fetch_kita_export_trend(industry_key=industry_key or "일반")
            if kita_data and kita_data.get("title"):
                # V10.1: KITA 통계 기사 — 개별 기사 URL 없음 → KITA 통계 페이지 URL 사용
                _kita_url = "https://www.kita.net/cmmrcInfo/tradeStatistics/tradeStatistics.do"
                kita_art = {
                    "doc_id": _make_doc_id("kita", kita_data.get("title", "")),
                    "title": kita_data["title"],
                    "summary": (
                        f"{kita_data.get('industry', '')} "
                        f"{kita_data.get('period', '')} "
                        f"수출 {kita_data.get('export_amount', '')} "
                        f"(전년비 {kita_data.get('yoy_change', '')})"
                    ).strip(),
                    "body": (
                        f"{kita_data.get('industry', '')} "
                        f"{kita_data.get('period', '')} "
                        f"수출액 {kita_data.get('export_amount', '')} "
                        f"전년 동기 대비 {kita_data.get('yoy_change', '')} 변동. "
                        f"주요 수출 시장: {', '.join(kita_data.get('top_markets', []))}. "
                        f"출처: KITA 무역통계."
                    ).strip(),
                    "url": _kita_url,
                    "no_fetch": True,  # V10.1: 본문 fetch 불필요 표시
                    "issue_yyyymm": datetime.now().strftime("%Y%m"),
                    "category": "KITA",
                    "source": "kita",
                    "source_priority": _SOURCE_PRIORITY.get("kita", 85),
                }
                sources_used.append("KITA")
                kita_count = 1
                all_extra.append(kita_art)
        except Exception as e:
            print(f"[extra_sources] KITA 수출동향 수집 실패: {e}")

    # ── V16.1: 코트라(KITA/KOTRA) 뉴스 기사 수집 ─────────────────
    # "소비재", "일반" 탭 전용 — KITA RSS 불안정 대체
    if "코트라" in routed:
        try:
            from core.kita_source import fetch_kita_news
            kotra_articles = fetch_kita_news(
                industry_key=industry_key or "일반", max_items=5,
            )
            if kotra_articles:
                # ── V17: KOTRA 관련성 스코어 재정렬 (industry_key 지정 시) ──
                # 소비재·배터리 등 타겟 산업과 무관한 KOTRA 기사(경제통상리포트 등) 필터
                if industry_key:
                    try:
                        from core.kotra_parser import rank_articles_by_relevance
                        _before = len(kotra_articles)
                        kotra_articles = rank_articles_by_relevance(
                            kotra_articles, industry_key=industry_key, top_n=5,
                        )
                        # score=0 기사 제거하되, 최소 2건은 보장 (전량 폐기 방지)
                        _scored = [
                            a for a in kotra_articles if a.get("relevance_score", 0) > 0
                        ]
                        if len(_scored) >= 2:
                            kotra_articles = _scored
                        elif kotra_articles:
                            kotra_articles = kotra_articles[:max(2, len(_scored))]
                        print(
                            f"[extra_sources] KOTRA 관련성 재정렬: "
                            f"{_before}건 -> {len(kotra_articles)}건 (industry={industry_key})"
                        )
                    except Exception as _re:
                        print(f"[extra_sources] KOTRA 관련성 재정렬 실패(무시): {_re}")

                for art in kotra_articles:
                    _src = art.get("source", "코트라")
                    art.setdefault(
                        "source_priority",
                        _SOURCE_PRIORITY.get(_src, _SOURCE_PRIORITY.get("코트라", 83)),
                    )
                _label = kotra_articles[0].get("source", "코트라") if kotra_articles else "코트라"
                sources_used.append(_label)
                kotra_count = len(kotra_articles)
                all_extra.extend(kotra_articles)
                print(
                    f"[extra_sources] [OK] {_label} 뉴스 {kotra_count}건 추가 "
                    f"(산업: {industry_key})"
                )
            else:
                print(
                    f"[extra_sources] [WARN] KITA/KOTRA 뉴스 수집 결과 없음 "
                    f"(산업: {industry_key})"
                )
        except Exception as e:
            print(f"[extra_sources] KITA/KOTRA 뉴스 수집 실패: {e}")

    # 중복 제거 (동일 소스 내에서만)
    all_extra = _deduplicate_articles(all_extra)

    src_names = ", ".join(sources_used) if sources_used else "없음"
    print(f"[extra_sources] KDI {len(kdi_articles)}건 + 외부 {len(all_extra)}건 ({src_names})")

    merged = merge_articles(kdi_articles, all_extra)
    print(f"[extra_sources] 중복 제거 후 총 {len(merged)}건")

    _rss_count = len(all_extra) - motie_count - kita_count - kotra_count - industry_rss_count
    source_stats = {
        "total": len(merged),
        "kdi": len(kdi_articles),
        "industry_rss": industry_rss_count,
        "rss": max(0, _rss_count),
        "motie": motie_count,
        "kita": kita_count,
        "kotra": kotra_count,          # V16.1
        "sources_used": sources_used,
    }

    return merged, source_stats
