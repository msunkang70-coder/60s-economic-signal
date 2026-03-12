"""
core/fetcher.py
KDI EIEC 페이지 HTML 수집, 기사 링크 추출, 본문 추출 담당.

main.py의 fetch_html / extract_article_links / fetch_article_text 로직을
모듈로 분리하여 Streamlit 앱에서 재사용할 수 있도록 패키징.
추가: readability-lxml 본문 추출, 기사 점수 기반 선별, collect_articles 오케스트레이터
"""

import re
import warnings
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs

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
        return requests.get(url, headers=HEADERS, timeout=15, verify=verify)

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
                  (KDI_MONTH_URL 전달 필요 — BASE_URL 아님)
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
    r"|광고\s*문의|제보하기|오탈자\s*신고)",
    re.IGNORECASE,
)

def _remove_noise(text: str) -> str:
    """
    ★ MODIFIED: 기사 본문에서 광고·저작권 표기·소셜 버튼 등 상용구를 제거한다.
    줄 단위로 처리하며, 노이즈 패턴과 일치하는 줄은 삭제한다.
    """
    lines = text.split("\n")
    cleaned = [ln for ln in lines if not _NOISE_PATTERNS.search(ln)]
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
        return "접근 거부 (HTTP 403) — 봇 차단 또는 로그인 필요"
    if sc == 404:
        return "페이지를 찾을 수 없음 (HTTP 404) — URL을 확인해 주세요"
    if sc == 429:
        return "요청 횟수 초과 (HTTP 429) — 잠시 후 다시 시도해 주세요"
    if sc and sc >= 500:
        return f"서버 오류 (HTTP {sc}) — 사이트 일시 장애"
    if "타임아웃" in err:
        return "응답 시간 초과 — 사이트 응답이 없습니다"
    if "SSL" in err:
        return "SSL 인증서 오류 — 보안 연결 불가"
    if "연결 실패" in err:
        return "네트워크 연결 실패 — 인터넷 연결을 확인하세요"
    return f"수집 실패: {err or '알 수 없는 오류'}"


def _fetch_with_diagnostics(url: str, max_retries: int = 2, timeout: int = 15) -> dict:
    """
    ★ NEW: HTTP GET — retry(최대 max_retries+1회) + SSL fallback + 진단 정보.

    Returns dict:
        ok          : bool       — 성공 여부
        html        : str        — 응답 HTML (성공 시)
        status_code : int|None   — HTTP 상태 코드
        final_url   : str        — 리디렉션 후 최종 URL
        content_type: str        — Content-Type 헤더
        text_length : int        — 원본 응답 텍스트 길이
        html_preview: str        — 응답 앞 500자 (실패 진단용)
        error       : str        — 실패 메시지
        attempts    : int        — 실제 시도 횟수
    """
    import time
    import urllib3

    info: dict = {
        "ok": False, "html": "", "status_code": None,
        "final_url": url, "content_type": "", "text_length": 0,
        "html_preview": "", "error": "", "attempts": 0,
    }

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
            return info   # 4xx/5xx — 재시도 불필요

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
      0) trafilatura — 최우선, 광고·내비 자동 제거, 전문 본문 추출 (설치된 경우)
      1) readability-lxml — trafilatura 실패 또는 짧을 때
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
                if len(candidate) >= MIN_ARTICLE_CHARS:
                    text = candidate
                    print(f"[fetcher] OK  trafilatura 성공: {len(text)}자")
                else:
                    print(
                        f"[fetcher] WARN  trafilatura 결과 부족 ({len(candidate)}자 < {MIN_ARTICLE_CHARS}자), 다음 단계로"
                    )
            else:
                print(f"[fetcher] WARN  trafilatura 결과 없음, 다음 단계로")
        except Exception as e:
            print(f"[fetcher] ERR  trafilatura 오류: {e}")
            text = ""
    else:
        print("[fetcher] trafilatura 미설치 — readability 단계로")

    # ── 1) readability-lxml fallback ──────────────────
    if len(text) < MIN_ARTICLE_CHARS and HAS_READABILITY:
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
    if len(text) < MIN_ARTICLE_CHARS:
        print(f"[fetcher] CSS 셀렉터 단계 시작 (현재 {len(text)}자)")
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
                if len(candidate) >= MIN_ARTICLE_CHARS:
                    text = candidate
                    print(
                        f"[fetcher] OK  CSS 셀렉터 '{sel}' 성공: {len(text)}자"
                    )
                    break

    # ── 3) <p> 태그 수집 fallback ──────────────────────
    if len(text) < MIN_ARTICLE_CHARS:
        print(f"[fetcher] <p> 태그 단계 시작 (현재 {len(text)}자)")
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
    if final_len < MIN_ARTICLE_CHARS:
        print(
            f"[fetcher] ERR  추출 실패 수준: 최종 {final_len}자 "
            f"(최소 {MIN_ARTICLE_CHARS}자 미달) | {short_url}"
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

        if len(text) < MIN_ARTICLE_CHARS:
            errors.append(
                f"제외 ({MIN_ARTICLE_CHARS}자 미만, 현재 {len(text)}자): "
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

    Returns:
        [{"doc_id", "title", "url", "issue_yyyymm", "category"}, ...]
    """
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
            print(f"[fetch_list] cidx 없음 — doc_id fallback 사용: {art['url'][:80]}")
        result.append({
            "doc_id":       doc_id,
            "title":        art["title"],
            "url":          art["url"],          # ← 이 URL이 fetch_detail에 그대로 전달됨
            "issue_yyyymm": issue_yyyymm,
            "category":     category,
        })
        print(f"[fetch_list] 수집: [{doc_id}] {art['title'][:40]} | {art['url'][:60]}")
    return result


def fetch_detail(doc_id: str, url: str, title: str = "", industry_key: str = "일반") -> dict:
    """
    Detail 단계: 개별 문서 본문·요약·키워드 수집 (사용자 클릭 시).

    ★ MODIFIED:
      - _fetch_with_diagnostics() 로 retry(최대 3회) + 진단 정보 수집
      - _extract_longest_block() 포함 4단계 본문 추출 전략
      - 성공 시만 요약 생성, 실패 시 구조화된 fail 정보 반환

    parse_status 값:
      "success" — 본문 추출 + 요약 성공
      "short"   — 본문 추출됐으나 최소 길이 미달
      "fail"    — HTTP 오류 또는 본문 추출 완전 실패

    Returns dict:
        body_text, summary_3lines, keywords, body_len, body_hash,
        fetched_at, parse_status, fetch_info, fail_reason
    """
    from core.summarizer import summarize_3line as summarize_rule_based   # v3: LLM 우선 / 규칙 기반 폴백
    import hashlib

    _short_url = url[:70]

    # ── Stage A: HTTP 수집 (retry 2회 + SSL fallback) ───
    fetch_info = _fetch_with_diagnostics(url, max_retries=2)
    print(
        f"[fetch_detail] {_short_url}\n"
        f"  status_code={fetch_info['status_code']}  "
        f"final_url={fetch_info['final_url'][:70]}\n"
        f"  content_type={fetch_info['content_type'][:60]}  "
        f"text_length={fetch_info['text_length']:,}  "
        f"attempts={fetch_info['attempts']}"
    )

    if not fetch_info["ok"]:
        print(
            f"[fetch_detail] FAIL  error={fetch_info['error']}\n"
            f"  html_preview={fetch_info['html_preview'][:200]}"
        )
        return {
            "body_text":      "",
            "summary_3lines": "",
            "keywords":       [],
            "body_len":       0,
            "body_hash":      "",
            "fetched_at":     datetime.now().isoformat(),
            "parse_status":   "fail",
            "fetch_info":     fetch_info,
            "fail_reason":    _classify_fetch_failure(fetch_info),
            "source_url":     url,
        }

    raw_html = fetch_info["html"]

    # ── Stage B: 본문 추출 (4단계) ─────────────────────
    body_text   = ""
    used_method = "none"

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
                print(f"[fetch_detail] trafilatura: {len(candidate)}자")
                if len(candidate) >= MIN_ARTICLE_CHARS:
                    body_text   = candidate
                    used_method = "trafilatura"
        except Exception as e:
            print(f"[fetch_detail] trafilatura 오류: {e}")

    # B-2) readability (trafilatura 부족 시)
    if len(body_text) < MIN_ARTICLE_CHARS and HAS_READABILITY:
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
    if len(body_text) < MIN_ARTICLE_CHARS:
        block_text, block_sel = _extract_longest_block(raw_html)
        print(f"[fetch_detail] 최장블록({block_sel}): {len(block_text)}자")
        if len(block_text) > len(body_text):
            body_text   = block_text
            used_method = f"block({block_sel})"

    # B-4) <p> 태그 수집 fallback
    if len(body_text) < MIN_ARTICLE_CHARS:
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

    # ── 진단 정보 추가 ───────────────────────────────
    fetch_info["extracted_body_length"] = len(body_text)
    fetch_info["used_method"]           = used_method

    # ── 본문 길이 검사 ───────────────────────────────
    if not body_text:
        print(
            f"[fetch_detail] FAIL  본문 추출 불가\n"
            f"  html_preview={raw_html[:200]}"
        )
        return {
            "body_text":      "",
            "summary_3lines": "",
            "keywords":       [],
            "body_len":       0,
            "body_hash":      "",
            "fetched_at":     datetime.now().isoformat(),
            "parse_status":   "fail",
            "fetch_info":     fetch_info,
            "fail_reason":    "본문을 추출할 수 없습니다 (동적 렌더링 가능성)",
            "source_url":     url,
        }

    if len(body_text) < MIN_ARTICLE_CHARS:
        print(
            f"[fetch_detail] SHORT  {len(body_text)}자 < {MIN_ARTICLE_CHARS}자"
        )
        return {
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
                f"({len(body_text)}자 / 최소 {MIN_ARTICLE_CHARS}자 필요)"
            ),
            "source_url":     url,
        }

    # ── 성공: 요약 생성 ────────────────────────────────
    body_text = body_text[:MAX_CHARS_PER_ARTICLE]
    keywords  = _extract_keywords_from_body(body_text)
    summary        = ""
    summary_source = "rule"
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

    body_hash = hashlib.sha256(body_text.encode()).hexdigest()[:16]
    print(
        f"[fetch_detail] SUCCESS  body={len(body_text)}자  "
        f"method={used_method}  summary_source={summary_source}  keywords={keywords[:3]}"
    )

    _result = {
        "body_text":        body_text,
        "summary_3lines":   summary,
        "summary_source":   summary_source,   # "gemini" | "rule"
        "keywords":         keywords,
        "body_len":         len(body_text),
        "body_hash":        body_hash,
        "fetched_at":       datetime.now().isoformat(),
        "parse_status":     "success",
        "fetch_info":       fetch_info,
        "fail_reason":      "",
        "source_url":       url,              # 원본 URL 보존
    }

    # Phase 13: 캐시 저장
    try:
        from core.article_cache import get_cache
        get_cache().set(doc_id, _result)
    except Exception:
        pass

    return _result
