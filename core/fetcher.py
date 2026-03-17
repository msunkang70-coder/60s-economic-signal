"""
core/fetcher.py
KDI EIEC 페이지 HTML 수집, 기사 링크 추출, 본문 추출 담당.

main.py의 fetch_html / extract_article_links / fetch_article_text 로직을
모듈로 분리하여 Streamlit 앱에서 재사용할 수 있도록 패키징.
추가: readability-lxml 본문 추출, 기사 점수 기반 선별, collect_articles 오케스트레이터
"""

import logging
import re
import threading
import warnings
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs

_log = logging.getLogger(__name__)

# Fix A: fetch_list 결과 모듈 레벨 캐시 (TTL 30분)
# - naraList.do 반복 HTTP 요청 방지 (산업 전환 / Streamlit rerun 시)
_fetch_list_cache: dict[str, tuple[float, list]] = {}  # url → (timestamp, result)
_fetch_list_lock = threading.Lock()
_FETCH_LIST_TTL = 1800  # 30분

# V13-perf: 성능 통계 누적기 (세션 단위)
# - 병목 리포트용: fetch/extract/summarize 시간 합계 + cache hit 수
_perf_stats: dict = {
    "fetch_total_s":     0.0,
    "extract_total_s":   0.0,
    "summarize_total_s": 0.0,
    "calls":             0,
    "cache_hits":        0,
    "early_hits":        0,
}
_perf_stats_lock = threading.Lock()


def _record_perf(fetch_s: float = 0.0, extract_s: float = 0.0, summarize_s: float = 0.0,
                 cache_hit: bool = False, early_hit: bool = False) -> None:
    """fetch_detail 호출 시 단계별 소요 시간 누적."""
    with _perf_stats_lock:
        _perf_stats["fetch_total_s"]     += fetch_s
        _perf_stats["extract_total_s"]   += extract_s
        _perf_stats["summarize_total_s"] += summarize_s
        _perf_stats["calls"]             += 1
        if cache_hit:
            _perf_stats["cache_hits"] += 1
        if early_hit:
            _perf_stats["early_hits"] += 1


def get_fetch_perf_stats() -> dict:
    """현재 세션 누적 성능 통계 반환 (복사본)."""
    with _perf_stats_lock:
        s = dict(_perf_stats)
    total = s["fetch_total_s"] + s["extract_total_s"] + s["summarize_total_s"]
    calls = max(s["calls"], 1)
    s["total_s"] = round(total, 2)
    s["avg_total_s"] = round(total / calls, 2)
    s["cache_hit_rate"] = round((s["cache_hits"] + s["early_hits"]) / calls * 100, 1)
    return s


def reset_fetch_perf_stats() -> None:
    """누적 통계 초기화 (새 세션 시작 시 호출)."""
    with _perf_stats_lock:
        for k in _perf_stats:
            _perf_stats[k] = 0.0 if isinstance(_perf_stats[k], float) else 0


# ── V13-disk: KDI body 디스크 영구 캐시 ─────────────────────
# 앱 재시작 이후에도 동일 KDI URL 재수집 방지 (24h TTL)
# 저장 필드: body_text, body_len, parse_status, used_method, fetched_at
# (summary는 산업별로 다르므로 제외)
import json as _json
import os as _os

_DISK_BODY_CACHE_PATH = _os.path.join(
    _os.path.dirname(_os.path.dirname(__file__)), "data", "article_body_cache.json"
)
_DISK_BODY_CACHE_TTL = 86400   # 24시간
_DISK_BODY_CACHE_MAX = 300     # 최대 300건 (오래된 순 삭제)
_disk_cache_lock = threading.Lock()
_disk_cache: dict | None = None  # 모듈 레벨 lazy 로드


def _load_disk_cache() -> dict:
    """디스크 캐시 파일 읽기 (최초 1회, 이후 메모리 사용)."""
    global _disk_cache
    if _disk_cache is not None:
        return _disk_cache
    with _disk_cache_lock:
        if _disk_cache is not None:
            return _disk_cache
        try:
            with open(_DISK_BODY_CACHE_PATH, "r", encoding="utf-8") as f:
                _disk_cache = _json.load(f)
        except (FileNotFoundError, _json.JSONDecodeError):
            _disk_cache = {}
    return _disk_cache


def _save_disk_cache(cache: dict) -> None:
    """디스크 캐시 저장 (LRU 초과 시 오래된 항목 제거)."""
    import time as _t
    # TTL 만료 + 초과 항목 정리
    now = _t.time()
    valid = {k: v for k, v in cache.items()
             if now - v.get("disk_ts", 0) < _DISK_BODY_CACHE_TTL}
    if len(valid) > _DISK_BODY_CACHE_MAX:
        # 가장 오래된 항목부터 삭제
        sorted_items = sorted(valid.items(), key=lambda x: x[1].get("disk_ts", 0))
        valid = dict(sorted_items[-_DISK_BODY_CACHE_MAX:])
    try:
        _os.makedirs(_os.path.dirname(_DISK_BODY_CACHE_PATH), exist_ok=True)
        with open(_DISK_BODY_CACHE_PATH, "w", encoding="utf-8") as f:
            _json.dump(valid, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        _log.debug("disk body cache 저장 실패: %s", e)


def get_disk_body(doc_id: str) -> dict | None:
    """디스크 캐시에서 body 조회. 만료/미존재 시 None."""
    import time as _t
    cache = _load_disk_cache()
    entry = cache.get(doc_id)
    if entry and _t.time() - entry.get("disk_ts", 0) < _DISK_BODY_CACHE_TTL:
        return entry
    return None


def set_disk_body(doc_id: str, body_text: str, parse_status: str,
                  body_len: int, used_method: str) -> None:
    """성공적으로 수집된 body를 디스크 캐시에 저장."""
    import time as _t
    cache = _load_disk_cache()
    with _disk_cache_lock:
        cache[doc_id] = {
            "body_text":    body_text,
            "body_len":     body_len,
            "parse_status": parse_status,
            "used_method":  used_method,
            "disk_ts":      _t.time(),
        }
    # 비동기 저장 (UI 블로킹 방지)
    try:
        _save_disk_cache(dict(cache))
    except Exception as e:
        _log.debug("disk body cache 비동기 저장 실패: %s", e)


# ── 경고 억제 (import 전에 설정) ─────────────────────
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")

import requests
from bs4 import BeautifulSoup

# ★ MODIFIED: trafilatura 선택적 import (1순위 추출 엔진, 미설치 시 다음 단계로)
try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

# readability-lxml 선택적 import (미설치 시 BeautifulSoup 셀렉터로만 동작)
try:
    from readability import Document as ReadabilityDoc
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False

from core.utils import clean_text

# ──────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

MIN_ARTICLE_CHARS = 800       # 이 글자 수 미만 기사는 제외 (품질 기준 상향)
MAX_CHARS_PER_ARTICLE = 5000  # ★ MODIFIED: 기사당 최대 수집 글자 수 (3000 → 5000)
MAX_TOTAL_CHARS = 15000       # ★ MODIFIED: 전체 기사 누적 최대 글자 수 (10000 → 15000)

# V16 Fix P1-2: 도메인별 최소 글자 수 분기 - 보도자료/관보 등 짧은 원문 도메인 허용
_DOMAIN_MIN_CHARS: dict[str, int] = {
    "yonhapnewstv.co.kr":  300,   # 연합뉴스TV - 방송기사 특성상 짧음 (V16.2 BUG FIX: .com→.co.kr)
    "yonhapnews.co.kr":    400,   # 연합뉴스 텍스트 기사
    "motie.go.kr":         200,   # 산업부 보도자료 - 제목+요약 구조
    "moef.go.kr":          200,   # 기획재정부 보도자료
    "korea.kr":            200,   # 정책브리핑
    "kita.net":            300,   # KITA 무역뉴스
    "kotra.or.kr":         300,   # KOTRA 해외시장뉴스
    "thinkfood.co.kr":     650,   # 식품음료신문 - 소비재 기사 650~800자대 다수 (V17.3)
}


def _get_min_chars(url: str) -> int:
    """도메인별 최소 글자 수 반환. 미등록 도메인은 전역 기준 적용."""
    try:
        from urllib.parse import urlparse
        _host = urlparse(url).hostname or ""
        for _domain, _min in _DOMAIN_MIN_CHARS.items():
            if _domain in _host:
                return _min
    except Exception:
        pass
    return MIN_ARTICLE_CHARS

# ── 기사 선별용 경제 키워드 (+2점) ─────────────────────
ECON_SELECTION_KEYWORDS = [
    # 거시경제
    "성장", "GDP", "물가", "인플레이션", "디플레이션", "경기", "경제",
    # 통화·금융
    "금리", "기준금리", "환율", "통화", "채권", "주가", "부채", "금융",
    # 무역·생산
    "수출", "수입", "무역", "생산", "제조업", "산업", "수급",
    # 고용·소득
    "고용", "실업", "소비", "가계", "소득", "임금", "노동",
    # 재정·정책
    "재정", "정책", "투자", "예산", "세수", "공공",
    # 시장 동향
    "기업", "시장", "불황", "회복", "위기", "전망", "경쟁",
    "개선", "악화", "상승", "하락", "증가", "감소",
    # 수치 단위
    "억원", "조원", "달러", "퍼센트",
    # 지수·지표
    "지수", "지표", "소비자물가", "생산자물가",
]

# ── 비경제 주제 키워드 (-3점) ───────────────────────────
NON_ECON_KEYWORDS = [
    "스포츠", "연예", "드라마", "영화", "음악", "축구", "야구",
    "패션", "뷰티", "여행", "맛집", "요리", "레시피",
    "게임", "애니메이션", "웹툰", "날씨", "기상",
]


# ──────────────────────────────────────────────────────
# 1. HTML 가져오기
# ──────────────────────────────────────────────────────
def fetch_html(url: str) -> str:
    """
    주어진 URL의 HTML을 가져와 문자열로 반환한다.
    SSL 인증서 검증 실패 시 verify=False 로 자동 재시도한다.
    (한국 정부 사이트의 인증서 체인 문제에 대한 호환 처리)

    Raises:
        RuntimeError: 네트워크 오류, 타임아웃, HTTP 오류
    """
    import urllib3

    def _get(verify: bool) -> requests.Response:
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.get(url, headers=HEADERS, timeout=10, verify=verify)

    # 1차 시도: SSL 검증 ON
    try:
        resp = _get(verify=True)
    except requests.exceptions.SSLError:
        # 2차 시도: SSL 검증 OFF (정부 사이트 인증서 이슈 대응)
        try:
            resp = _get(verify=False)
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"네트워크 연결 실패: {e}") from e
        except requests.exceptions.Timeout as e:
            raise RuntimeError(f"요청 시간 초과 (URL: {url})") from e
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"요청 실패 (URL: {url}): {e}") from e
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"네트워크 연결 실패: {e}") from e
    except requests.exceptions.Timeout as e:
        raise RuntimeError(f"요청 시간 초과 (URL: {url})") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"요청 실패 (URL: {url}): {e}") from e

    # HTTP 에러 체크
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"HTTP 오류 {resp.status_code} (URL: {url})") from e

    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


# ──────────────────────────────────────────────────────
# 2. 페이지 제목 추출
# ──────────────────────────────────────────────────────
def extract_page_title(html: str) -> str:
    """
    HTML에서 페이지 제목을 추출한다.
    우선순위: 메인 컨텐츠 영역 H1/H2 → 전체 H1 → H2 → og:title → <title>
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) 메인 컨텐츠 영역 내 H1/H2 우선 (가장 의미있는 제목)
    for area_sel in ["main", "article", ".content_wrap", "#content",
                     ".main_content", ".container", "#wrap"]:
        area = soup.select_one(area_sel)
        if area:
            for tag_name in ["h1", "h2"]:
                tag = area.find(tag_name)
                if tag:
                    text = tag.get_text(strip=True)
                    if len(text) > 3:
                        return text

    # 2) 페이지 전체 H1
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(strip=True)
        if len(text) > 3:
            return text

    # 3) H2
    h2 = soup.find("h2")
    if h2:
        text = h2.get_text(strip=True)
        if len(text) > 3:
            return text

    # 4) og:title 메타태그
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()

    # 5) <title> 태그
    if soup.title and soup.title.text.strip():
        return soup.title.text.strip()

    return "KDI 경제정보센터 월간 경제이슈"


# ──────────────────────────────────────────────────────
# 3. URL에서 month_key 추출
# ──────────────────────────────────────────────────────
def extract_month_key(url: str) -> str:
    """
    URL의 쿼리 파라미터(sel_year, sel_month)에서 'YYYY-MM' 형태의
    month_key를 추출한다. 파라미터가 없으면 현재 년월을 반환한다.

    예시:
        "...?sel_year=2026&sel_month=02" → "2026-02"
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    year = params.get("sel_year", [None])[0]
    month = params.get("sel_month", [None])[0]
    if year and month:
        return f"{year}-{month.zfill(2)}"
    return datetime.now().strftime("%Y-%m")


# ──────────────────────────────────────────────────────
# 4. 페이지 키워드 추출 (기사 선별 점수 계산용)
# ──────────────────────────────────────────────────────
def _extract_page_keywords(html: str) -> set:
    """
    목록 페이지의 H1/H2/H3 헤딩에서 한국어 단어(2글자 이상)를 추출한다.
    기사 제목 점수 계산 시 페이지 맥락 반영에 사용.
    """
    soup = BeautifulSoup(html, "html.parser")
    words: set = set()
    for tag in soup.find_all(["h1", "h2", "h3"]):
        text = tag.get_text(strip=True)
        words.update(re.findall(r"[가-힣]{2,}", text))
    return words


# ──────────────────────────────────────────────────────
# 5. 기사 후보 점수 계산
# ──────────────────────────────────────────────────────
def _score_article_candidate(title: str, page_keywords: set) -> float:
    """
    기사 제목을 기반으로 선별 점수를 계산한다.

    점수 기준:
      +3.0: 제목 단어가 페이지 헤딩 키워드와 일치할 때 (단어당)
      +2.0: 제목에 경제 사전 키워드 포함 (키워드당)
      -3.0: 제목에 비경제 주제 키워드 포함 (키워드당)
    """
    score = 0.0
    title_words = set(re.findall(r"[가-힣]{2,}", title))

    # 페이지 헤딩 키워드 매칭 (+3 each)
    score += len(title_words & page_keywords) * 3.0

    # 경제 사전 키워드 매칭 (+2 each)
    for kw in ECON_SELECTION_KEYWORDS:
        if kw in title:
            score += 2.0

    # 비경제 주제 패널티 (-3 each)
    for kw in NON_ECON_KEYWORDS:
        if kw in title:
            score -= 3.0

    return score


# ──────────────────────────────────────────────────────
# 6. 기사 링크 수집
# ──────────────────────────────────────────────────────
def extract_article_links(html: str, base_url: str, top_n: int = 3) -> list:
    """
    목록 페이지 HTML에서 기사 링크와 제목을 top_n개 추출한다.

    전략:
      1) ul.review_main_list 등 특정 리스트 컨테이너에서 먼저 수집
      2) top_n 미만이면 페이지 전체 naraView.do 링크로 보충
      3) 중복 URL 제거

    Args:
        html:     목록 페이지 HTML 문자열
        base_url: 상대경로 → 절대경로 변환을 위한 기준 URL
                  (KDI_MONTH_URL 전달 필요 - BASE_URL 아님)
        top_n:    수집할 최대 기사 수

    Returns:
        [{"title": str, "url": str}, ...] (최대 top_n개)

    Raises:
        ValueError: 기사 링크를 하나도 찾지 못한 경우
    """
    soup = BeautifulSoup(html, "html.parser")

    def _parse_links(tag_pairs: list) -> list:
        """(a_tag, container) 쌍 목록에서 기사 dict 리스트를 만든다."""
        seen: set = set()
        result: list = []
        for a_tag, container in tag_pairs:
            href = (a_tag.get("href") or "").strip()
            if not href or href == "#":
                continue
            full_url = urljoin(base_url, href)
            if "naraView.do" not in full_url or full_url in seen:
                continue
            seen.add(full_url)

            # 제목 추출: 컨테이너 내 텍스트 우선, 길이 10~120자
            title = ""
            for sel in ["p.txt_tit", ".tit", "strong", "p", "span"]:
                t = container.select_one(sel)
                if t:
                    cand = t.get_text(" ", strip=True)
                    if 10 < len(cand) <= 120:
                        title = cand
                        break
            if not title:
                raw = a_tag.get_text(" ", strip=True)
                title = raw[:100] if raw else "제목 없음"
            if len(title) > 100:
                title = title[:100] + "..."

            # S1-4: 숫자 공백 제거 ("5 ,000" → "5,000") + 연속 공백 정리
            title = re.sub(r'(\d)\s+,', r'\1,', title)
            title = re.sub(r'\s{2,}', ' ', title).strip()
            result.append({"title": title, "url": full_url})
        return result

    # ── 1단계: 특정 리스트 컨테이너 ──────────────────
    container_selectors = [
        ("ul.review_main_list li", "a.hover_img"),
        ("ul.review_main_list li", "a"),
        (".board_list_wrap li", "a"),
        (".list_wrap li", "a"),
        ("ul.list li", "a"),
    ]
    tag_pairs: list = []
    for list_sel, a_sel in container_selectors:
        items = soup.select(list_sel)
        if items:
            for item in items:
                a = item.select_one(a_sel)
                if a and "naraView.do" in (a.get("href") or ""):
                    tag_pairs.append((a, item))
            if tag_pairs:
                break

    articles = _parse_links(tag_pairs)
    seen_urls = {art["url"] for art in articles}

    # ── 2단계: 부족하면 전체 naraView 링크로 보충 ──
    if len(articles) < top_n:
        all_a = soup.find_all("a", href=re.compile(r"naraView\.do"))
        extra_pairs = [
            (a, a) for a in all_a
            if urljoin(base_url, a.get("href", "")) not in seen_urls
        ]
        articles.extend(_parse_links(extra_pairs))

    if not articles:
        raise ValueError(
            "기사 링크를 찾을 수 없습니다.\n"
            "  → URL이 올바른지, 또는 사이트 구조가 변경되었는지 확인하세요."
        )

    return articles[:top_n]


# ──────────────────────────────────────────────────────
# ★ MODIFIED: 노이즈 제거 헬퍼 (광고·네비·저작권 상용구 제거)
# ──────────────────────────────────────────────────────
_NOISE_PATTERNS = re.compile(
    r"(저작권자\s*[©ⓒ].{0,60}무단.{0,20}금지"
    r"|무단\s*전재.{0,30}금지"
    r"|ⓒ\s*\w+\s*All\s*rights?\s*reserved"
    r"|구독\s*신청|뉴스레터\s*구독"
    r"|소셜\s*공유|공유하기|카카오톡\s*공유|페이스북\s*공유"
    r"|이전\s*기사|다음\s*기사|관련\s*기사"
    r"|댓글\s*\d*개?|댓글\s*작성|로그인\s*후\s*댓글"
    r"|광고\s*문의|제보하기|오탈자\s*신고"
    # V7: 사이드바 계산 예시 / 관련없는 교육 콘텐츠 필터
    r"|이\s*회사의\s*ROE는.{0,40}×100"
    r"|ROE\s*[=는].{0,30}×\s*100"
    r"|가정용\s*태양광.{0,30}설치비"
    r"|최대\s*\d+%\s*지원.{0,20}태양광"
    r"|인기\s*기사|많이\s*본\s*기사|추천\s*기사"
    r"|최신\s*기사|실시간\s*기사|포토\s*뉴스"
    r"|기사\s*제공\s*:?\s*[가-힣]+뉴스"
    r"|핫\s*이슈|오늘의\s*이슈"
    # V9: 기자 연락처 / 이메일 / 방송 큐시트 / 메타데이터 제거
    r"|[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}"  # 이메일 주소 포함 줄
    r"|<전화연결\s*:.{0,60}>"                          # 방송 큐시트 전화연결
    r"|기자\s*[:=]\s*[가-힣]{2,4}\s*기자"              # "기자: 홍길동 기자"
    r"|[가-힣]{2,4}\s*기자\s*[(（][a-zA-Z0-9_.@]+"     # "홍길동 기자(email@..."
    r"|취재\s*[:=]|사진\s*[:=]|편집\s*[:=]"            # 크레딧 라인
    r"|영상취재\s*[:=]|촬영\s*[:=]|앵커\s*[:=]"        # 방송 크레딧
    r"|<앵커>|</앵커>|<리포트>|</리포트>"               # 방송 스크립트 태그
    r"|\[앵커\]|\[기자\]|\[리포터\]"                    # 방송 스크립트 마커
    r"|MBC뉴스\s*[가-힣]+입니다"                       # 방송 클로징
    r"|YTN\s*[가-힣]+입니다|SBS\s*[가-힣]+입니다"
    r"|KBS\s*[가-힣]+입니다"
    r"|연합뉴스TV\s*[가-힣]+입니다"
    r"|[가-힣]{2,4}\s+\S*(?:금융|은행|연구|증권|리서치|자산운용|투자|경제)[가-힣]*\s+(?:연구위원|연구원|수석연구원|선임연구원|애널리스트|수석|팀장|센터장)"
    r"|[가-힣]{2,4}\s+(?:연구위원|수석연구원|선임연구원|수석이코노미스트)\s"
    r"|\[사람이\s*되고\s*싶어요\d*\]"
    r"|\[[^\]]{0,20}싶어요\d*\])",
    re.IGNORECASE,
)

def _remove_noise(text: str) -> str:
    """
    ★ MODIFIED V7: 기사 본문에서 광고·저작권·사이드바 노이즈를 제거한다.
    1단계: 사이드바/관련기사 블록 감지 → 해당 줄 이후 전부 절단 (먼저 실행)
    2단계: 나머지 줄에서 패턴 매칭으로 개별 노이즈 줄 제거
    """
    lines = text.split("\n")

    # V7-1단계: 사이드바 블록 절단 (먼저 실행 - 패턴 제거보다 우선)
    _sidebar_markers = re.compile(
        r"^(관련\s*기사|추천\s*콘텐츠|함께\s*읽으면|많이\s*본\s*뉴스"
        r"|인기\s*기사\s*TOP|이\s*기사를?\s*본\s*사람|다른\s*기사"
        r"|원문\s*보기|출처\s*:\s|Tags?\s*:)",
        re.IGNORECASE,
    )
    truncated = []
    for ln in lines:
        if _sidebar_markers.search(ln.strip()):
            break  # 사이드바 시작점 이후 모두 제거
        truncated.append(ln)

    # V7-2단계: 개별 노이즈 줄 제거
    cleaned = [ln for ln in truncated if not _NOISE_PATTERNS.search(ln)]

    return "\n".join(cleaned)


# ──────────────────────────────────────────────────────
# ★ NEW: Extended content selectors for "longest block" strategy
# ──────────────────────────────────────────────────────
_CONTENT_SELECTORS_ALL = [
    # KDI 사이트 특화
    "div.editor.nara", "div.view_body", "div.view_comm_style",
    "article#ui_contents", ".cont_area", ".view_content", ".article_body",
    # 범용 뉴스/블로그
    "article", "main",
    ".content", "#content", "#main-content",
    ".post-content", ".entry-content",
    ".article-content", ".article-body",
    # 국내 주요 뉴스 포털
    "#articleBodyContents", ".newsct_article",
    ".article_txt", ".view_con", ".news_content",
    "#article-view-content-div", ".read_body",
    ".news_body_area", ".article__body",
]


def _extract_longest_block(html: str) -> tuple:
    """
    ★ NEW: 모든 후보 셀렉터를 전수 시도해 가장 긴 본문을 반환한다.

    기존 fetch_article_text 의 "첫 번째 성공" 전략 대신
    "모든 후보 중 최장" 전략으로 수집 성공률을 높인다.

    Returns:
        (best_text: str, used_selector: str)
    """
    soup = BeautifulSoup(html, "html.parser")

    # 노이즈 태그 사전 제거
    for tag in soup.find_all(
        ["script", "style", "nav", "header", "footer",
         "aside", "figure", "noscript", "iframe"]
    ):
        tag.decompose()

    best_text = ""
    best_sel  = ""

    for sel in _CONTENT_SELECTORS_ALL:
        el = soup.select_one(sel)
        if not el:
            continue
        candidate = _remove_noise(
            clean_text(el.get_text(separator="\n", strip=True))
        )
        if len(candidate) > len(best_text):
            best_text = candidate
            best_sel  = sel

    return best_text, best_sel


def _classify_fetch_failure(fetch_info: dict) -> str:
    """★ NEW: HTTP 수집 실패 원인을 사용자 친화적인 메시지로 변환한다."""
    sc  = fetch_info.get("status_code")
    err = fetch_info.get("error", "")
    if sc == 403:
        return "접근 거부 (HTTP 403) - 봇 차단 또는 로그인 필요"
    if sc == 404:
        return "페이지를 찾을 수 없음 (HTTP 404) - URL을 확인해 주세요"
    if sc == 429:
        return "요청 횟수 초과 (HTTP 429) - 잠시 후 다시 시도해 주세요"
    if sc and sc >= 500:
        return f"서버 오류 (HTTP {sc}) - 사이트 일시 장애"
    if "타임아웃" in err:
        return "응답 시간 초과 - 사이트 응답이 없습니다"
    if "SSL" in err:
        return "SSL 인증서 오류 - 보안 연결 불가"
    if "연결 실패" in err:
        return "네트워크 연결 실패 - 인터넷 연결을 확인하세요"
    return f"수집 실패: {err or '알 수 없는 오류'}"


def _fetch_with_diagnostics(url: str, max_retries: int = 2, timeout: int = 10) -> dict:
    """
    ★ NEW: HTTP GET - retry(최대 max_retries+1회) + SSL fallback + 진단 정보.

    Returns dict:
        ok          : bool       - 성공 여부
        html        : str        - 응답 HTML (성공 시)
        status_code : int|None   - HTTP 상태 코드
        final_url   : str        - 리디렉션 후 최종 URL
        content_type: str        - Content-Type 헤더
        text_length : int        - 원본 응답 텍스트 길이
        html_preview: str        - 응답 앞 500자 (실패 진단용)
        error       : str        - 실패 메시지
        attempts    : int        - 실제 시도 횟수
    """
    import time
    import urllib3

    info: dict = {
        "ok": False, "html": "", "status_code": None,
        "final_url": url, "content_type": "", "text_length": 0,
        "html_preview": "", "error": "", "attempts": 0,
    }

    # V10.1: 빈/무효 URL 최종 방어선 - requests.get("") 호출 자체를 차단
    if not url or not url.strip() or not url.startswith(("http://", "https://")):
        info["error"] = f"무효 URL: '{url}'"
        print(f"[fetcher] [BLOCK] URL 검증 실패: '{url}'")
        return info

    def _do_get(verify: bool) -> "requests.Response":
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.get(url, headers=HEADERS, timeout=timeout, verify=verify)

    last_error = ""
    for attempt in range(max_retries + 1):   # 0,1,2 → 최대 3회
        info["attempts"] = attempt + 1
        if attempt > 0:
            wait_s = 1.5 * attempt
            print(
                f"[fetcher] 재시도 {attempt}/{max_retries} ({wait_s:.1f}s 대기)"
                f" | {url[:60]}"
            )
            time.sleep(wait_s)

        # ── HTTP 요청 (SSL fallback 포함) ──────────────
        resp = None
        try:
            resp = _do_get(verify=True)
        except requests.exceptions.SSLError:
            print(f"[fetcher] SSL 오류 → verify=False 재시도 | {url[:60]}")
            try:
                resp = _do_get(verify=False)
            except Exception as e:
                last_error = f"SSL 오류: {str(e)[:80]}"
                continue
        except requests.exceptions.Timeout:
            last_error = f"타임아웃 ({timeout}s)"
            continue
        except requests.exceptions.ConnectionError as e:
            last_error = f"연결 실패: {str(e)[:80]}"
            continue
        except requests.exceptions.RequestException as e:
            last_error = f"요청 실패: {str(e)[:80]}"
            continue

        # ── 응답 파싱 ─────────────────────────────────
        resp.encoding = resp.apparent_encoding or "utf-8"
        resp_text = resp.text
        info["status_code"]  = resp.status_code
        info["final_url"]    = resp.url
        info["content_type"] = resp.headers.get("Content-Type", "")
        info["text_length"]  = len(resp_text)
        info["html_preview"] = resp_text[:500]

        if resp.status_code >= 400:
            last_error      = f"HTTP {resp.status_code}"
            info["error"]   = last_error
            print(f"[fetcher] HTTP {resp.status_code} | {url[:60]}")
            return info   # 4xx/5xx - 재시도 불필요

        # ── 성공 ─────────────────────────────────────
        info["ok"]   = True
        info["html"] = resp_text
        print(
            f"[fetcher] OK  attempts={info['attempts']}  "
            f"status={resp.status_code}  len={len(resp_text):,}  "
            f"ctype={info['content_type'][:40]}  | {url[:60]}"
        )
        return info

    # 모든 시도 실패
    info["error"] = last_error
    print(
        f"[fetcher] FAIL  attempts={info['attempts']}  "
        f"error={last_error} | {url[:60]}"
    )
    return info


# ──────────────────────────────────────────────────────
# 7. 기사 본문 추출
# ──────────────────────────────────────────────────────
def fetch_article_text(url: str, max_chars: int = MAX_CHARS_PER_ARTICLE) -> str:
    """
    기사 상세 페이지 URL에서 본문 텍스트를 추출한다.

    ★ MODIFIED 추출 전략 (순서대로 시도):
      0) trafilatura - 최우선, 광고·내비 자동 제거, 전문 본문 추출 (설치된 경우)
      1) readability-lxml - trafilatura 실패 또는 짧을 때
      2) BeautifulSoup CSS 셀렉터 체인 (KDI 특화 → 범용 순서)
      3) <p> 태그 수집 fallback (본문 문단만 수집)
      4) <body> 전체 텍스트 최후 수단

    각 단계마다 추출 결과 길이를 print()로 로깅한다.

    Args:
        url:       기사 상세 페이지 URL
        max_chars: 반환할 최대 글자 수 (기본 5000)

    Returns:
        본문 텍스트 문자열 (최대 max_chars 글자)

    Raises:
        RuntimeError: HTML 가져오기 실패
    """
    raw_html = fetch_html(url)
    short_url = url[:70]
    text = ""
    # V16.1 Fix P1-2: 도메인별 최소 글자 수 - fetch_article_text에도 적용
    _min_chars = _get_min_chars(url)

    # ── 0) ★ trafilatura 최우선 시도 ──────────────────
    if HAS_TRAFILATURA:
        try:
            extracted = trafilatura.extract(
                raw_html,
                include_comments=False,
                include_tables=False,
                no_fallback=False,
                favor_recall=True,
            )
            if extracted:
                candidate = _remove_noise(clean_text(extracted))
                print(
                    f"[fetcher] trafilatura 추출: {len(candidate)}자 | {short_url}"
                )
                if len(candidate) >= _min_chars:
                    text = candidate
                    print(f"[fetcher] OK  trafilatura 성공: {len(text)}자")
                else:
                    print(
                        f"[fetcher] WARN  trafilatura 결과 부족 ({len(candidate)}자 < {_min_chars}자 기준), 다음 단계로"
                    )
            else:
                print(f"[fetcher] WARN  trafilatura 결과 없음, 다음 단계로")
        except Exception as e:
            print(f"[fetcher] ERR  trafilatura 오류: {e}")
            text = ""
    else:
        print("[fetcher] trafilatura 미설치 - readability 단계로")

    # ── 1) readability-lxml fallback ──────────────────
    if len(text) < _min_chars and HAS_READABILITY:
        try:
            doc = ReadabilityDoc(raw_html)
            content_html = doc.summary(html_partial=True)
            soup_r = BeautifulSoup(content_html, "lxml")
            candidate = _remove_noise(
                clean_text(soup_r.get_text(separator="\n", strip=True))
            )
            print(
                f"[fetcher] readability 추출: {len(candidate)}자 | {short_url}"
            )
            if len(candidate) > len(text):
                text = candidate
                print(f"[fetcher] OK  readability 채택: {len(text)}자")
        except Exception as e:
            print(f"[fetcher] ERR  readability 오류: {e}")

    # ── 2) CSS 셀렉터 체인 fallback ────────────────────
    if len(text) < _min_chars:
        print(f"[fetcher] CSS 셀렉터 단계 시작 (현재 {len(text)}자, 기준 {_min_chars}자)")
        soup = BeautifulSoup(raw_html, "html.parser")

        # 불필요한 태그 사전 제거
        for tag in soup.find_all(
            ["script", "style", "nav", "header", "footer",
             "aside", "figure", "noscript", "iframe"]
        ):
            tag.decompose()

        # KDI 사이트 특화 셀렉터 → 범용 셀렉터 순서로 시도
        content_selectors = [
            "div.editor.nara",
            "div.view_body",
            "div.view_comm_style",
            "article#ui_contents",
            ".cont_area",
            ".view_content",
            ".article_body",
            "article",
            "main",
        ]
        for sel in content_selectors:
            el = soup.select_one(sel)
            if el:
                candidate = _remove_noise(
                    clean_text(el.get_text(separator="\n", strip=True))
                )
                if len(candidate) >= _min_chars:
                    text = candidate
                    print(
                        f"[fetcher] OK  CSS 셀렉터 '{sel}' 성공: {len(text)}자"
                    )
                    break

    # ── 3) <p> 태그 수집 fallback ──────────────────────
    if len(text) < _min_chars:
        print(f"[fetcher] <p> 태그 단계 시작 (현재 {len(text)}자, 기준 {_min_chars}자)")
        soup = BeautifulSoup(raw_html, "html.parser")
        paragraphs = []
        for p in soup.find_all("p"):
            p_text = p.get_text(strip=True)
            if len(p_text) > 30:   # 의미 없는 짧은 p 태그 제외
                paragraphs.append(p_text)
        if paragraphs:
            candidate = _remove_noise(clean_text("\n".join(paragraphs)))
            if len(candidate) > len(text):
                text = candidate
                print(f"[fetcher] OK  <p> 태그 채택: {len(text)}자")

    # ── 4) body 전체 최후 fallback ─────────────────────
    if not text:
        print(f"[fetcher] body 전체 fallback 시작")
        soup = BeautifulSoup(raw_html, "html.parser")
        body = soup.find("body")
        if body:
            text = _remove_noise(
                clean_text(body.get_text(separator="\n", strip=True))
            )
            print(f"[fetcher] body fallback: {len(text)}자")

    # ── 최종 결과 로깅 ────────────────────────────────
    final_len = len(text[:max_chars])
    if final_len < _min_chars:
        print(
            f"[fetcher] ERR  추출 실패 수준: 최종 {final_len}자 "
            f"(최소 {_min_chars}자 미달, 도메인 기준) | {short_url}"
        )
    else:
        print(f"[fetcher] 최종 반환: {final_len}자 | {short_url}")

    return text[:max_chars]


# ──────────────────────────────────────────────────────
# 8. 오케스트레이터: 전체 수집 파이프라인
# ──────────────────────────────────────────────────────
def collect_articles(month_url: str, top_n: int = 3) -> dict:
    """
    KDI 월간 이슈 URL을 받아 기사 수집 → 본문 추출까지 수행하는
    오케스트레이터 함수.

    처리 순서:
      1) 목록 페이지 HTML 가져오기
      2) 페이지 제목 & month_key 추출
      3) 기사 링크 수집 (top_n * 5 후보)
      4) 페이지 키워드 추출 (H1/H2 헤딩 기반)
      5) 기사 제목 점수 계산 → 상위 top_n*2 선별
      6) 각 기사 본문 수집 (800자 미만 제외, MAX_TOTAL_CHARS 누적 제한)

    Args:
        month_url: KDI 월간 이슈 목록 페이지 URL
        top_n:     수집할 유효 기사 최대 수

    Returns:
        dict with keys:
            page_title     (str)       : 목록 페이지 제목
            month_key      (str)       : "YYYY-MM" 형식
            source_url     (str)       : 입력 URL
            articles       (list[dict]): 점수 기반 상위 top_n 후보 (참고용)
            valid_articles (list[dict]): 실제 사용된 유효 기사 (최대 top_n)
            texts          (list[str]) : 각 유효 기사 본문
            errors         (list[str]) : 수집 중 경고/에러 메시지

    Raises:
        RuntimeError: 목록 페이지 접근 실패 또는 기사 링크 없음
    """
    errors: list = []

    # ── Step 1: 목록 페이지 HTML ────────────────────
    try:
        list_html = fetch_html(month_url)
    except RuntimeError as e:
        raise RuntimeError(f"목록 페이지 가져오기 실패: {e}") from e

    # ── Step 2: 메타데이터 추출 ─────────────────────
    page_title = extract_page_title(list_html)
    month_key = extract_month_key(month_url)

    # ── Step 3: 기사 링크 수집 (후보 top_n*5개) ─────
    try:
        candidates = extract_article_links(list_html, month_url, top_n * 5)
    except ValueError as e:
        raise RuntimeError(str(e)) from e

    # ── Step 4: 페이지 키워드 추출 ──────────────────
    page_keywords = _extract_page_keywords(list_html)

    # ── Step 5: 기사 점수 계산 & 정렬 ───────────────
    scored: list = []
    for art in candidates:
        score = _score_article_candidate(art["title"], page_keywords)
        scored.append((score, art))
    scored.sort(key=lambda x: -x[0])

    # 상위 top_n*2개만 본문 수집 대상으로 (버퍼 확보)
    fetch_targets = [art for _, art in scored[: top_n * 2]]

    # ── Step 6: 본문 수집 & 필터링 ──────────────────
    valid_articles: list = []
    texts: list = []
    total_chars = 0

    for art in fetch_targets:
        if len(valid_articles) >= top_n:
            break
        if total_chars >= MAX_TOTAL_CHARS:
            errors.append(
                f"전체 최대 글자 수({MAX_TOTAL_CHARS:,}자) 초과로 이후 기사 생략"
            )
            break

        try:
            text = fetch_article_text(art["url"], MAX_CHARS_PER_ARTICLE)
        except RuntimeError as e:
            errors.append(f"본문 수집 실패 ({art['title'][:30]}…): {e}")
            continue

        # V16.1 Fix P1-2: 도메인별 최소 글자 수 적용
        _art_min = _get_min_chars(art["url"])
        if len(text) < _art_min:
            errors.append(
                f"제외 ({_art_min}자 미만, 현재 {len(text)}자): "
                f"{art['title'][:40]}…"
            )
            continue

        remaining = MAX_TOTAL_CHARS - total_chars
        text = text[:remaining]
        total_chars += len(text)

        valid_articles.append(art)
        texts.append(text)

    return {
        "page_title": page_title,
        "month_key": month_key,
        "source_url": month_url,
        "articles": [art for _, art in scored[:top_n]],   # 점수 상위 (참고용)
        "valid_articles": valid_articles,                   # 실제 사용 기사
        "texts": texts,                                     # 각 기사 본문
        "errors": errors,                                   # 경고/에러 메시지
    }


# ══════════════════════════════════════════════════════
# 9. List/Detail 2단계 수집 (신규)  # FIX: 구조 분리
# ══════════════════════════════════════════════════════

def _make_doc_id(url: str) -> str:
    """URL cidx 파라미터 기반 고유 doc_id 생성."""  # FIX: hash() 대신 cidx 사용
    p = parse_qs(urlparse(url).query)
    cidx  = p.get("cidx",      [""])[0]
    year  = p.get("sel_year",  [""])[0]
    month = p.get("sel_month", [""])[0].zfill(2)
    return f"kdi_{cidx}_{year}{month}" if cidx else f"kdi_{abs(hash(url)) % 10**8}"


def _extract_keywords_from_body(text: str) -> list:
    """본문에서 경제 키워드 상위 10개 추출."""  # FIX: 신규
    from collections import Counter
    words = re.findall(r"[가-힣]{2,}", text)
    c = Counter(words)
    return [w for w, _ in c.most_common(50) if w in ECON_SELECTION_KEYWORDS][:10]


def fetch_list(url: str, top_n: int = 20) -> list[dict]:
    """
    List 단계: naraList.do에서 기사 메타데이터만 수집 (본문 미포함).  # FIX: 신규
    doc_id 기준 중복 제거.

    Fix A: 모듈 레벨 TTL 캐시 (30분) - naraList.do 반복 요청 방지.

    Returns:
        [{"doc_id", "title", "url", "issue_yyyymm", "category"}, ...]
    """
    import time as _t
    _cache_key = f"{url}|{top_n}"
    with _fetch_list_lock:
        _cached = _fetch_list_cache.get(_cache_key)
        if _cached is not None:
            _ts, _res = _cached
            if _t.time() - _ts < _FETCH_LIST_TTL:
                print(f"[fetch_list] [FAST] 캐시 히트 ({int(_FETCH_LIST_TTL - (_t.time() - _ts))}초 남음) - {url[:60]}")
                return list(_res)  # 방어 복사

    html = fetch_html(url)
    issue_yyyymm = extract_month_key(url).replace("-", "")

    try:
        raw = extract_article_links(html, url, top_n)
    except ValueError as e:
        raise RuntimeError(str(e)) from e

    seen, result = set(), []
    for art in raw:
        doc_id = _make_doc_id(art["url"])
        if doc_id in seen:                          # FIX: doc_id 중복 제거
            continue
        seen.add(doc_id)
        p = parse_qs(urlparse(art["url"]).query)
        category = p.get("fcode", [""])[0]
        # URL에 cidx가 없으면 경고 출력
        p_check = parse_qs(urlparse(art["url"]).query)
        if not p_check.get("cidx"):
            print(f"[fetch_list] cidx 없음 - doc_id fallback 사용: {art['url'][:80]}")
        result.append({
            "doc_id":       doc_id,
            "title":        art["title"],
            "url":          art["url"],          # ← 이 URL이 fetch_detail에 그대로 전달됨
            "issue_yyyymm": issue_yyyymm,
            "category":     category,
        })
        print(f"[fetch_list] 수집: [{doc_id}] {art['title'][:40]} | {art['url'][:60]}")

    # Fix A: 결과 캐시 저장 (TTL 30분)
    import time as _t
    with _fetch_list_lock:
        _fetch_list_cache[_cache_key] = (_t.time(), list(result))
    print(f"[fetch_list] [OK] {len(result)}건 수집 완료 → 캐시 저장 (TTL 30분)")
    return result


def _is_google_news_url(url: str) -> bool:
    """Google News 중간 리디렉션 URL 여부 판정 (fetcher 내부용)."""
    return bool(url and "news.google.com" in url)


def fetch_detail(doc_id: str, url: str, title: str = "", industry_key: str = "일반", skip_summary: bool = False) -> dict:
    """
    Detail 단계: 개별 문서 본문·요약·키워드 수집 (사용자 클릭 시).

    ★ MODIFIED:
      - _fetch_with_diagnostics() 로 retry(최대 3회) + 진단 정보 수집
      - _extract_longest_block() 포함 4단계 본문 추출 전략
      - 성공 시만 요약 생성, 실패 시 구조화된 fail 정보 반환
      - V10.1: 빈 URL 사전 방어 추가
      - V12-perf: Google News URL fast-fail (fetch 파이프라인 전체 생략)
      - V12-perf: 단계별 타이밍 로그 추가 (fetch/extract/summarize/total)
      - V13-cache: 함수 진입 시 article_cache 선조회 (defense-in-depth)
        동일 doc_id 재호출 시 HTTP/LLM 완전 생략 → KDI fetch 중복 방지

    parse_status 값:
      "success" - 본문 추출 + 요약 성공
      "short"   - 본문 추출됐으나 최소 길이 미달
      "fail"    - HTTP 오류 또는 본문 추출 완전 실패
      "google_news_snippet" - Google News URL, RSS 스니펫 사용

    Returns dict:
        body_text, summary_3lines, keywords, body_len, body_hash,
        fetched_at, parse_status, fetch_info, fail_reason
    """
    from core.summarizer import summarize_3line as summarize_rule_based   # v3: LLM 우선 / 규칙 기반 폴백
    import hashlib
    import time as _time

    _t_start = _time.time()

    # ── V17.4 TRACE: 이탈리아 기사 단일 추적 ──────────────────────────────
    def _ftrace(msg: str) -> None:
        import os as _os3, datetime as _dt3
        try:
            _lp = _os3.path.join(
                _os3.path.dirname(_os3.path.dirname(_os3.path.abspath(__file__))),
                "data", "debug_trace.log"
            )
            ts = _dt3.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            with open(_lp, "a", encoding="utf-8") as _lf:
                _lf.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    if title and "이탈리아" in title and "스킨케어" in title:
        _ftrace("[FETCH_TRACE][IN]")
        _ftrace(f"url={url}")
        _ftrace(f"doc_id={doc_id}")
        _ftrace(f"title={title[:80] if title else ''}")

    # V13-cache: defense-in-depth - article_cache 선조회
    # main_content.py / prefetch_worker.py 이외 경로에서 직접 호출될 때도 중복 HTTP/LLM 방지
    if doc_id:
        try:
            from core.article_cache import get_cache as _get_ac
            _early = _get_ac().get(doc_id)
            if _early and _early.get("parse_status") in (
                "success", "partial", "short", "fail", "google_news_snippet"
            ):
                _log.debug("[fetch_detail] [FAST] early-cache HIT: %s (status=%s)", doc_id, _early.get("parse_status"))
                _record_perf(early_hit=True)
                return _early
        except Exception:
            pass

    # V10.1: 빈/무효 URL 사전 방어 - requests.get("") → InvalidURL 예외 방지
    if not url or not url.strip() or not url.startswith(("http://", "https://")):
        print(f"[fetch_detail] [WARN] 무효 URL 차단: '{url}' (doc_id={doc_id}, title={title[:30]})")
        return {
            "body_text":      "",
            "summary_3lines": "",
            "keywords":       [],
            "body_len":       0,
            "body_hash":      "",
            "fetched_at":     datetime.now().isoformat(),
            "parse_status":   "fail",
            "fetch_info":     {"ok": False, "error": f"무효 URL: '{url}'", "attempts": 0,
                               "status_code": None, "final_url": "", "content_type": "",
                               "text_length": 0, "html_preview": "", "html": ""},
            "fail_reason":    "원문 링크 없음",
            "url":            url,
            "source_url":     url,
        }

    _short_url = url[:70]

    # ── V12-perf: Google News URL fast-fail (V17.4 수정) ─────────────────────
    # Google News RSS 링크(news.google.com)는 기본적으로 JavaScript 기반 리디렉션.
    # V17.4: fast-fail 전에 HTTP redirect / HTML 파싱으로 실제 URL 해소 시도.
    #   성공 시 url 변수를 업데이트하여 정상 fetch 파이프라인으로 진입.
    #   실패 시 기존 google_news_snippet fast-fail 유지.
    #
    # 이 로직은 kita_source.py V17.4 _try_resolve_gnews_url()이 실패했을 때의
    # 안전망(defense-in-depth) 역할을 한다.
    if _is_google_news_url(url):
        if title and "이탈리아" in title and "스킨케어" in title:
            _ftrace("[FETCH_TRACE][RESOLVE] Google News URL 감지 → resolve 시도")
            _ftrace(f"input_url={url[:100]}")
        _resolved_url: str | None = None
        try:
            import re as _re_gn
            _redir_resp = requests.get(
                url,
                headers=HEADERS,
                timeout=5,
                allow_redirects=True,
                verify=False,
            )
            # 1) HTTP 리다이렉트 성공
            if "news.google.com" not in _redir_resp.url:
                _resolved_url = _redir_resp.url
                print(f"[fetch_detail] [LINK] Google News HTTP redirect 해소: {_resolved_url[:70]}")
            else:
                _gn_html = _redir_resp.text
                # 2) JavaScript window.location 파싱
                _m_js = _re_gn.search(
                    r'window\.location(?:\.href)?\s*=\s*["\']([^"\']{20,})["\']',
                    _gn_html,
                )
                if _m_js and "news.google.com" not in _m_js.group(1):
                    _resolved_url = _m_js.group(1)
                    print(f"[fetch_detail] [LINK] Google News JS URL 추출: {_resolved_url[:70]}")
                else:
                    # 3) KOTRA URL 직접 탐색
                    _m_kotra = _re_gn.search(
                        r'href=["\']([^"\']*kotra\.or\.kr[^"\']{10,})["\']', _gn_html
                    )
                    if _m_kotra:
                        _resolved_url = _m_kotra.group(1)
                        print(f"[fetch_detail] [LINK] Google News KOTRA href 추출: {_resolved_url[:70]}")
        except Exception as _gn_e:
            print(f"[fetch_detail] Google News URL 해소 시도 실패(무시): {_gn_e}")

        if _resolved_url:
            # URL 교체 후 정상 fetch 파이프라인 진입
            url = _resolved_url
            _short_url = url[:70]
            print(f"[fetch_detail] [OK] Google News URL 해소 완료 → {_short_url}")
            if title and "이탈리아" in title and "스킨케어" in title:
                _ftrace(f"[FETCH_TRACE][RESOLVE] [OK] 해소 성공 → effective_url={_resolved_url[:100]}")
        else:
            # 해소 실패 → 기존 fast-fail 동작 유지
            _elapsed = _time.time() - _t_start
            print(
                f"[fetch_detail] [FAST] Google News URL fast-fail ({_elapsed:.3f}s) - "
                f"URL 해소 불가 | {_short_url}"
            )
            if title and "이탈리아" in title and "스킨케어" in title:
                _ftrace(f"[FETCH_TRACE][RESOLVE] ❌ 해소 실패 → fast-fail")
            _gn_result = {
                "body_text":      "",
                "summary_3lines": "",
                "keywords":       [],
                "body_len":       0,
                "body_hash":      "",
                "fetched_at":     datetime.now().isoformat(),
                "parse_status":   "google_news_snippet",
                "fetch_info":     {"ok": False, "error": "Google News URL - 해소 불가", "attempts": 0,
                                   "status_code": None, "final_url": url, "content_type": "",
                                   "text_length": 0, "html_preview": ""},
                "fail_reason":    "Google News URL (JS redirect, 본문 추출 불가)",
                "url":            url,
                "source_url":     url,
                "_perf": {"fetch_s": 0.0, "extract_s": 0.0, "summarize_s": 0.0, "total_s": _elapsed},
            }
            try:
                from core.article_cache import get_cache
                get_cache().set(doc_id, _gn_result)
            except Exception:
                pass
            return _gn_result

    # ── V13-disk: HTTP 요청 전 디스크 body 캐시 체크 ─────────────────────────
    # 앱 재시작 후에도 동일 KDI 기사 재수집 방지 (24h TTL)
    _disk_hit = get_disk_body(doc_id) if doc_id else None
    if _disk_hit:
        _disk_body = _disk_hit.get("body_text", "")
        _disk_len = _disk_hit.get("body_len", len(_disk_body))
        _disk_status = _disk_hit.get("parse_status", "success")
        print(
            f"[fetch_detail] 💾 disk-cache HIT: {doc_id} ({_disk_len}자, {_disk_status}) "
            f"- HTTP 스킵 | {_short_url}"
        )
        _fetch_s = 0.0
        _t_extract_start = _time.time()
        # disk-cached body를 summary 생성에 직접 투입
        body_text = _disk_body
        used_method = f"disk_cache({_disk_hit.get('used_method', 'n/a')})"
        _t_extract_end = _time.time()
        _extract_s = _t_extract_end - _t_extract_start

        _t_summarize_start = _time.time()
        summary, summary_source = "", ""
        if not skip_summary and _disk_len >= 100:
            try:
                summary_result = summarize_rule_based(
                    text=body_text, industry_key=industry_key, title=title
                )
                if isinstance(summary_result, tuple):
                    summary, summary_source = summary_result
                else:
                    summary, summary_source = summary_result, "rule"
            except Exception:
                summary, summary_source = "", ""
        _summarize_s = _time.time() - _t_summarize_start
        _total_s = _time.time() - _t_start

        import hashlib
        body_hash = hashlib.sha256(body_text.encode()).hexdigest()[:16]
        # V15: disk-cache 경로 analysis_source 결정
        _disk_analysis_src = (
            "snippet" if _disk_len < 120 else
            "partial_body" if _disk_len < 300 else
            "full_body"
        )
        print(
            f"[fetcher] 💾 disk-cache 반환 | body_length={_disk_len} "
            f"analysis_source={_disk_analysis_src} fetch_status=disk_cache"
        )
        _disk_result = {
            "body_text":        body_text,
            "summary_3lines":   summary,
            "summary_source":   summary_source,
            "keywords":         [],
            "body_len":         _disk_len,
            "body_hash":        body_hash,
            "fetched_at":       datetime.now().isoformat(),
            "parse_status":     _disk_status,
            "analysis_source":  _disk_analysis_src,  # V15
            "fetch_info":       {"ok": True, "attempts": 0, "error": "",
                                 "status_code": None, "final_url": url,
                                 "content_type": "", "text_length": _disk_len,
                                 "html_preview": "", "html": ""},
            "fail_reason":      "",
            "url":              url,
            "source_url":       url,
            "_perf": {"fetch_s": _fetch_s, "extract_s": _extract_s,
                      "summarize_s": _summarize_s, "total_s": _total_s},
            "_disk_cache_hit":  True,
        }
        try:
            from core.article_cache import get_cache as _get_ac2
            _get_ac2().set(doc_id, _disk_result)
        except Exception:
            pass
        _record_perf(fetch_s=0.0, extract_s=_extract_s,
                     summarize_s=_summarize_s, cache_hit=True)
        print(
            f"[fetch_detail] 💾 disk-cache 완료  "
            f"⏱ summarize={_summarize_s:.2f}s total={_total_s:.2f}s"
        )
        return _disk_result

    # ── Stage A: HTTP 수집 (retry 2회 + SSL fallback) ───
    _t_fetch_start = _time.time()
    fetch_info = _fetch_with_diagnostics(url, max_retries=2)
    _t_fetch_end = _time.time()
    _fetch_s = _t_fetch_end - _t_fetch_start
    print(
        f"[fetch_detail] {_short_url}\n"
        f"  ⏱ fetch={_fetch_s:.2f}s  status_code={fetch_info['status_code']}  "
        f"final_url={fetch_info['final_url'][:70]}\n"
        f"  content_type={fetch_info['content_type'][:60]}  "
        f"text_length={fetch_info['text_length']:,}  "
        f"attempts={fetch_info['attempts']}"
    )

    if not fetch_info["ok"]:
        _total_s = _time.time() - _t_start
        print(
            f"[fetch_detail] FAIL  error={fetch_info['error']}\n"
            f"  html_preview={fetch_info['html_preview'][:200]}"
        )
        _fail_result = {
            "body_text":      "",
            "summary_3lines": "",
            "keywords":       [],
            "body_len":       0,
            "body_hash":      "",
            "fetched_at":     datetime.now().isoformat(),
            "parse_status":   "fail",
            "fetch_info":     fetch_info,
            "fail_reason":    _classify_fetch_failure(fetch_info),
            "url":            url,
            "source_url":     url,
            "_perf": {"fetch_s": _fetch_s, "extract_s": 0.0, "summarize_s": 0.0, "total_s": _total_s},
        }
        # Fix D: 실패 결과 캐시 저장 (30분 TTL) - 동일 URL 재시도 방지
        try:
            from core.article_cache import get_cache
            get_cache().set(doc_id, _fail_result, doc_type="fail")
        except Exception:
            pass
        return _fail_result

    raw_html = fetch_info["html"]

    # ── Stage B: 본문 추출 (4단계) ─────────────────────
    _t_extract_start = _time.time()
    body_text   = ""
    used_method = "none"
    # V16.1 Fix P1-2: 도메인별 최소 글자 수 - B-1~B-4 모든 단계에 적용
    _min_chars_b = _get_min_chars(url)
    print(f"[fetch_detail] 도메인 최소기준: {_min_chars_b}자 | {url[:60]}")

    # B-1) trafilatura (최우선)
    if HAS_TRAFILATURA:
        try:
            extracted = trafilatura.extract(
                raw_html,
                include_comments=False,
                include_tables=False,
                no_fallback=False,
                favor_recall=True,
            )
            if extracted:
                candidate = _remove_noise(clean_text(extracted))
                print(f"[fetch_detail] trafilatura: {len(candidate)}자 (기준 {_min_chars_b}자)")
                if len(candidate) >= _min_chars_b:
                    body_text   = candidate
                    used_method = "trafilatura"
        except Exception as e:
            print(f"[fetch_detail] trafilatura 오류: {e}")

    # B-2) readability (trafilatura 부족 시)
    if len(body_text) < _min_chars_b and HAS_READABILITY:
        try:
            doc_r        = ReadabilityDoc(raw_html)
            content_html = doc_r.summary(html_partial=True)
            soup_r       = BeautifulSoup(content_html, "lxml")
            candidate    = _remove_noise(
                clean_text(soup_r.get_text(separator="\n", strip=True))
            )
            print(f"[fetch_detail] readability: {len(candidate)}자")
            if len(candidate) > len(body_text):
                body_text   = candidate
                used_method = "readability"
        except Exception as e:
            print(f"[fetch_detail] readability 오류: {e}")

    # B-3) 최장 블록 전략 (KDI 특화 + 범용 셀렉터 전수 시도)
    if len(body_text) < _min_chars_b:
        block_text, block_sel = _extract_longest_block(raw_html)
        print(f"[fetch_detail] 최장블록({block_sel}): {len(block_text)}자")
        if len(block_text) > len(body_text):
            body_text   = block_text
            used_method = f"block({block_sel})"

    # B-4) <p> 태그 수집 fallback
    if len(body_text) < _min_chars_b:
        soup_p     = BeautifulSoup(raw_html, "html.parser")
        paragraphs = [
            p.get_text(strip=True)
            for p in soup_p.find_all("p")
            if len(p.get_text(strip=True)) > 30
        ]
        if paragraphs:
            candidate = _remove_noise(clean_text("\n".join(paragraphs)))
            print(f"[fetch_detail] <p>태그: {len(candidate)}자")
            if len(candidate) > len(body_text):
                body_text   = candidate
                used_method = "p_tags"

    # ── Stage B-5: KOTRA 구조화 파서 (kotra_parser.py) ──────────
    # KOTRA URL 감지 시 핵심요약박스·표·PDF 첨부파일을 추가 추출하여
    # HTML 단순 텍스트 한계를 극복하고 LLM 입력 품질을 향상시킨다.
    _kotra_parse_info: dict = {}
    try:
        from core.kotra_parser import enrich_kotra_body, is_kotra_url as _is_kotra
        if title and "이탈리아" in title and "스킨케어" in title:
            _ftrace("[FETCH_TRACE][KOTRA]")
            _ftrace(f"is_kotra_url({url[:80]})={_is_kotra(url)}")
            _ftrace(f"body_text_before_kotra_parser={len(body_text)}자")
        if _is_kotra(url):
            print(f"[fetch_detail] [SEARCH] KOTRA URL 감지 - 구조화 파서 실행")
            if title and "이탈리아" in title and "스킨케어" in title:
                _ftrace(f"[FETCH_TRACE][KOTRA] kotra_parser_invoked=True")
            _enriched, _kotra_parse_info = enrich_kotra_body(
                raw_html=raw_html,
                url=url,
                existing_body=body_text,
            )
            if _enriched and len(_enriched) >= len(body_text):
                body_text   = _enriched
                used_method = "kotra_structured"
                print(
                    f"[fetch_detail] [OK] KOTRA 구조화 본문 채택: {len(body_text)}자 "
                    f"(PDF포함={_kotra_parse_info.get('has_pdf', False)})"
                )
                if title and "이탈리아" in title and "스킨케어" in title:
                    _ftrace(f"[FETCH_TRACE][KOTRA] [OK] 구조화 본문 채택: {len(body_text)}자")
        elif title and "이탈리아" in title and "스킨케어" in title:
            _ftrace(f"[FETCH_TRACE][KOTRA] kotra_parser_invoked=False (is_kotra_url=False)")
    except Exception as _kotra_err:
        print(f"[fetch_detail] [WARN] KOTRA 파서 오류 (무시, 기존 본문 유지): {_kotra_err}")
        if title and "이탈리아" in title and "스킨케어" in title:
            _ftrace(f"[FETCH_TRACE][KOTRA] 오류: {_kotra_err}")

    # ── 진단 정보 추가 ───────────────────────────────
    _t_extract_end = _time.time()
    _extract_s = _t_extract_end - _t_extract_start
    fetch_info["extracted_body_length"] = len(body_text)
    fetch_info["used_method"]           = used_method
    if _kotra_parse_info:
        fetch_info["kotra_parse_info"] = _kotra_parse_info

    # ── 본문 길이 검사 ───────────────────────────────
    if not body_text:
        _total_s = _time.time() - _t_start
        print(
            f"[fetch_detail] FAIL  본문 추출 불가\n"
            f"  ⏱ fetch={_fetch_s:.2f}s extract={_extract_s:.2f}s total={_total_s:.2f}s\n"
            f"  html_preview={raw_html[:200]}"
        )
        _no_body_result = {
            "body_text":      "",
            "summary_3lines": "",
            "keywords":       [],
            "body_len":       0,
            "body_hash":      "",
            "fetched_at":     datetime.now().isoformat(),
            "parse_status":   "fail",
            "fetch_info":     fetch_info,
            "fail_reason":    "본문을 추출할 수 없습니다 (동적 렌더링 가능성)",
            "url":            url,
            "source_url":     url,
            "_perf": {"fetch_s": _fetch_s, "extract_s": _extract_s, "summarize_s": 0.0, "total_s": _total_s},
        }
        # Fix D: 실패 결과 캐시 저장 (30분 TTL)
        try:
            from core.article_cache import get_cache
            get_cache().set(doc_id, _no_body_result, doc_type="fail")
        except Exception:
            pass
        return _no_body_result

    # V16 Fix P1-2: 도메인별 최소 글자 수 적용
    _min_chars = _get_min_chars(url)
    if len(body_text) < _min_chars:
        _total_s = _time.time() - _t_start
        print(
            f"[fetch_detail] SHORT  {len(body_text)}자 < {_min_chars}자 (도메인 기준)  "
            f"⏱ fetch={_fetch_s:.2f}s extract={_extract_s:.2f}s total={_total_s:.2f}s"
        )
        _short_result = {
            "body_text":      body_text,
            "summary_3lines": "",
            "keywords":       [],
            "body_len":       len(body_text),
            "body_hash":      hashlib.sha256(body_text.encode()).hexdigest()[:16],
            "fetched_at":     datetime.now().isoformat(),
            "parse_status":   "short",
            "fetch_info":     fetch_info,
            "fail_reason":    (
                f"본문이 너무 짧습니다 "
                f"({len(body_text)}자 / 최소 {_min_chars}자 필요)"
            ),
            "url":            url,
            "source_url":     url,
            "_perf": {"fetch_s": _fetch_s, "extract_s": _extract_s, "summarize_s": 0.0, "total_s": _total_s},
        }
        # Fix D: 짧은 본문 결과 캐시 저장 (30분 TTL) - 재시도 방지
        try:
            from core.article_cache import get_cache
            get_cache().set(doc_id, _short_result, doc_type="fail")
        except Exception:
            pass
        return _short_result

    # ── 성공: 요약 생성 ────────────────────────────────
    body_text = body_text[:MAX_CHARS_PER_ARTICLE]
    keywords  = _extract_keywords_from_body(body_text)
    summary        = ""
    summary_source = "rule"
    _t_summarize_start = _time.time()

    # V6-perf: skip_summary=True면 본문만 캐시 (prefetch용 - LLM 호출 생략)
    if skip_summary:
        summary = ""
        summary_source = "pending"
        print(f"[fetch_detail] SKIP_SUMMARY  body={len(body_text)}자 | {_short_url}")
    else:
        try:
            result = summarize_rule_based(body_text, title=title, industry_key=industry_key)
            # summarize_3line은 (text, source) 튜플 반환
            if isinstance(result, tuple):
                summary, summary_source = result
            else:
                summary = result
        except Exception as e:
            print(f"[fetch_detail] 요약 오류: {e}")
            summary = ""

    _summarize_s = _time.time() - _t_summarize_start
    _total_s = _time.time() - _t_start
    body_hash = hashlib.sha256(body_text.encode()).hexdigest()[:16]
    print(
        f"[fetch_detail] SUCCESS  body={len(body_text)}자  "
        f"method={used_method}  summary_source={summary_source}  keywords={keywords[:3]}\n"
        f"  ⏱ fetch={_fetch_s:.2f}s extract={_extract_s:.2f}s "
        f"summarize={_summarize_s:.2f}s total={_total_s:.2f}s"
    )

    # ── V15: 본문 추출 구조화 로깅 (body_length / analysis_source / fetch_status) ──
    _body_len = len(body_text.strip()) if body_text else 0
    # analysis_source 결정 (summarizer._determine_analysis_mode와 동일 기준)
    if _body_len < 120:
        _analysis_src = "snippet"
    elif _body_len < 300:
        _analysis_src = "partial_body"
    else:
        _analysis_src = "full_body"
    print(
        f"[fetcher] 📰 본문 추출 완료 | title='{title[:30]}...' "
        f"body_length={_body_len} analysis_source={_analysis_src} "
        f"fetch_status=success | 산업: {industry_key}"
    )
    if _body_len < 120:
        print(f"[fetcher] [WARN] snippet 수준 본문 ({_body_len}자) - URL: {url}")
        print(f"[fetcher]    본문 앞 200자: {body_text[:200] if body_text else '(빈 문자열)'}")

    _result = {
        "body_text":        body_text,
        "summary_3lines":   summary,
        "summary_source":   summary_source,   # "gemini" | "rule"
        "keywords":         keywords,
        "body_len":         len(body_text),
        "body_hash":        body_hash,
        "fetched_at":       datetime.now().isoformat(),
        "parse_status":     "success",
        "analysis_source":  _analysis_src,    # V15: "snippet"/"partial_body"/"full_body"
        "fetch_info":       fetch_info,
        "fail_reason":      "",
        "url":              url,
        "source_url":       url,              # 원본 URL 보존 (하위호환)
        "_perf": {"fetch_s": _fetch_s, "extract_s": _extract_s, "summarize_s": _summarize_s, "total_s": _total_s},
    }

    # 본문-제목 최소 연관성 검증
    if body_text and title:
        import re as _re
        title_words = set(_re.findall(r'[가-힣]{2,}', title))
        title_words -= {"우리", "이번", "대한", "관련", "통해", "위해"}
        body_sample = body_text[:1500]
        match_count = sum(1 for w in title_words if w in body_sample)
        if title_words and match_count < min(2, len(title_words)):
            print(f"[fetcher] [WARN] 본문-제목 불일치: '{title[:30]}' → body에 키워드 {match_count}개만 매칭")
            _result["parse_status"] = "partial"
            _result["fail_reason"] = "body_title_mismatch"

    # Phase 13: 인메모리 캐시 저장
    try:
        from core.article_cache import get_cache
        get_cache().set(doc_id, _result)
    except Exception as e:
        _log.debug("Failed to cache article result for doc_id '%s': %s", doc_id, e)

    # V13-disk: 성공/partial body 디스크 영구 캐시 저장 (24h, HTTP 재수집 방지)
    if doc_id and _result.get("parse_status") in ("success", "partial") and body_text:
        try:
            set_disk_body(
                doc_id=doc_id,
                body_text=body_text,
                parse_status=_result["parse_status"],
                body_len=len(body_text),
                used_method=used_method,
            )
        except Exception as e:
            _log.debug("disk body cache 저장 실패 doc_id=%s: %s", doc_id, e)

    # V13-perf: 성능 통계 누적
    _record_perf(fetch_s=_fetch_s, extract_s=_extract_s, summarize_s=_summarize_s)

    # ── V17.4 TRACE: 이탈리아 OUT 로그 ──────────────────────────────
    if title and "이탈리아" in title and "스킨케어" in title:
        _ftrace("[FETCH_TRACE][OUT]")
        _ftrace(f"fetch_status={_result.get('parse_status','?')}")
        _ftrace(f"body_length={_result.get('body_len', len(body_text))}자")
        _ftrace(f"final_url={_result.get('url','')[:100]}")
        _ftrace(f"summary_source={_result.get('summary_source','?')}")

    return _result
