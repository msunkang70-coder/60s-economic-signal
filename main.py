"""
60초 경제신호 v1
KDI EIEC 월간 경제 이슈 대시보드 → 유튜브 쇼츠 스크립트 생성기
"""

import re
import os
import sys
import io
import warnings
from datetime import datetime
from urllib.parse import urljoin, urlparse

# Windows 터미널 UTF-8 출력 강제
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# requests/urllib3 버전 불일치 경고 억제 (import 전에 설정해야 동작)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# 설정 변수
# ──────────────────────────────────────────────
KDI_MONTH_URL = "https://eiec.kdi.re.kr/publish/naraList.do"
TOP_N = 3

BASE_URL = "https://eiec.kdi.re.kr"
MAX_CHARS_PER_ARTICLE = 3000
MAX_TOTAL_CHARS = 10000
MIN_ARTICLE_CHARS = 500

OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "outputs")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "output_script.txt")
SRT_FILE    = os.path.join(OUTPUT_DIR, "output_script.srt")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


# ──────────────────────────────────────────────
# 1. HTML 가져오기
# ──────────────────────────────────────────────
def fetch_html(url: str) -> str:
    """
    주어진 URL의 HTML을 가져와 문자열로 반환한다.
    SSL 검증 실패 시 verify=False로 재시도한다 (정부 사이트 인증서 호환).
    네트워크 오류 발생 시 예외를 일으킨다.
    """
    import urllib3

    def _get(verify: bool) -> requests.Response:
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.get(url, headers=HEADERS, timeout=15, verify=verify)

    try:
        response = _get(verify=True)
    except requests.exceptions.SSLError:
        print("  [경고] SSL 인증서 검증 실패 → verify=False로 재시도 (신뢰할 수 있는 정부 사이트)")
        try:
            response = _get(verify=False)
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"[오류] 네트워크 연결 실패: {e}") from e
        except requests.exceptions.Timeout as e:
            raise RuntimeError(f"[오류] 요청 시간 초과 (URL: {url}): {e}") from e
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"[오류] 요청 실패 (URL: {url}): {e}") from e
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"[오류] 네트워크 연결 실패: {e}") from e
    except requests.exceptions.Timeout as e:
        raise RuntimeError(f"[오류] 요청 시간 초과 (URL: {url}): {e}") from e
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"[오류] HTTP 오류 {response.status_code} (URL: {url}): {e}"
        ) from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"[오류] 요청 실패 (URL: {url}): {e}") from e

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"[오류] HTTP 오류 {response.status_code} (URL: {url}): {e}"
        ) from e

    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


# ──────────────────────────────────────────────
# 2. 페이지 제목 추출
# ──────────────────────────────────────────────
def extract_page_title(html: str) -> str:
    """
    HTML에서 페이지 제목을 추출한다.
    우선순위: og:title 메타태그 → <title> 태그 → 기본값
    """
    soup = BeautifulSoup(html, "html.parser")

    # og:title 우선
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        return og_title["content"].strip()

    # <title> 태그
    title_tag = soup.find("title")
    if title_tag and title_tag.text.strip():
        return title_tag.text.strip()

    # 헤딩 fallback
    h1 = soup.find("h1")
    if h1 and h1.text.strip():
        return h1.text.strip()

    return "KDI 경제정보센터 월간 경제이슈"


# ──────────────────────────────────────────────
# 3. 기사 링크 수집
# ──────────────────────────────────────────────
def extract_article_links(html: str, base_url: str, top_n: int = TOP_N) -> list[dict]:
    """
    기사 목록 페이지 HTML에서 상위 top_n개의 기사 링크와 제목을 추출한다.

    전략:
      1) 특정 리스트 컨테이너(ul.review_main_list 등)에서 먼저 수집
      2) top_n 미만이면 페이지 전체 naraView.do 링크로 보충
      3) 중복 URL 제거, cidx 기준으로 빠른(작은) cidx 우선

    반환: [{"title": str, "url": str}, ...]
    """
    soup = BeautifulSoup(html, "html.parser")

    def _extract_from_tags(tag_pairs: list) -> list[dict]:
        """(a_tag, container) 쌍 목록에서 기사 dict 목록을 만든다."""
        seen = set()
        result = []
        for a_tag, container in tag_pairs:
            href = a_tag.get("href", "").strip()
            if not href or href == "#":
                continue
            full_url = urljoin(base_url, href)
            if "naraView.do" not in full_url or full_url in seen:
                continue
            seen.add(full_url)

            # 제목 추출
            title = ""
            for sel in ["p.txt_tit", ".tit", "strong", "p", "span"]:
                t = container.select_one(sel)
                if t:
                    candidate = t.get_text(" ", strip=True)
                    # 너무 짧거나 내비게이션 텍스트 제외
                    if 10 < len(candidate) <= 120:
                        title = candidate
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

    # ── 1단계: 특정 컨테이너 셀렉터 ──
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
                if a and a.get("href") and "naraView.do" in a.get("href", ""):
                    tag_pairs.append((a, item))
            if tag_pairs:
                break

    articles = _extract_from_tags(tag_pairs)
    seen_urls = {art["url"] for art in articles}

    # ── 2단계: top_n 미만이면 전체 naraView 링크로 보충 ──
    if len(articles) < top_n:
        all_a = soup.find_all("a", href=re.compile(r"naraView\.do"))
        extra_pairs = [(a, a) for a in all_a
                       if urljoin(base_url, a.get("href", "")) not in seen_urls]
        extra = _extract_from_tags(extra_pairs)
        articles.extend(extra)

    if not articles:
        raise ValueError(
            "[오류] 기사 링크 추출 실패: naraView.do 패턴의 링크를 찾을 수 없습니다.\n"
            "  → URL이 올바른지, 또는 사이트 구조가 변경되었는지 확인하세요."
        )

    return articles[:top_n]


# ──────────────────────────────────────────────
# 4. 기사 본문 추출
# ──────────────────────────────────────────────
def fetch_article_text(url: str, max_chars: int = MAX_CHARS_PER_ARTICLE) -> str:
    """
    기사 상세 페이지 URL에서 본문 텍스트를 추출한다.
    최대 max_chars 글자까지만 반환한다.
    """
    try:
        html = fetch_html(url)
    except RuntimeError as e:
        print(f"  [경고] 기사 HTML 가져오기 실패: {e}")
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # 불필요한 태그 제거
    for tag in soup.find_all(["script", "style", "nav", "header", "footer",
                               "aside", "figure", "noscript", "iframe"]):
        tag.decompose()

    # 본문 후보 셀렉터 (우선순위 순)
    content_selectors = [
        "div.editor.nara",
        "div.view_body",
        "div.view_comm_style",
        "article#ui_contents",
        ".cont_area",
        ".view_content",
        ".article_body",
        "article",
    ]

    text = ""
    for sel in content_selectors:
        element = soup.select_one(sel)
        if element:
            text = element.get_text(separator="\n", strip=True)
            if len(text) >= MIN_ARTICLE_CHARS:
                break
            text = ""  # 너무 짧으면 다음 셀렉터 시도

    # 모든 셀렉터 실패 시 <main> or <body> fallback
    if not text:
        main = soup.find("main") or soup.find("body")
        if main:
            text = main.get_text(separator="\n", strip=True)

    # 연속 공백·줄바꿈 정리
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()

    return text[:max_chars]


# ──────────────────────────────────────────────
# 5. 규칙 기반 요약
# ──────────────────────────────────────────────
def summarize_rule_based(text: str, title: str = "", max_sentences: int = 3) -> str:
    """
    텍스트에서 중요 문장을 규칙 기반으로 추출하여 요약한다.

    점수 기준:
      - 경제 키워드 포함 여부
      - 문장 앞부분(상위 30%) 가중치
      - 적절한 문장 길이 (20~120자)
      - 숫자/퍼센트 포함 시 추가 점수
    """
    if not text:
        return title or "본문 없음"

    # 경제 핵심 키워드
    keywords = [
        "성장", "GDP", "물가", "인플레이션", "금리", "환율", "수출", "수입",
        "무역", "고용", "실업", "경기", "소비", "투자", "재정", "통화",
        "기준금리", "부채", "생산", "산업", "제조업", "서비스", "가계",
        "기업", "정책", "경제", "시장", "주가", "채권", "불황", "회복",
        "위기", "전망", "개선", "악화", "상승", "하락", "증가", "감소",
        "억원", "조원", "달러", "유로", "%", "퍼센트", "만명", "천명",
    ]

    # 문장 분리: 마침표/물음표/느낌표 기준
    raw_sentences = re.split(r"(?<=[.?!。])\s+|(?<=다\.)\s*\n|(?<=다\.\s)\s*", text)
    sentences = []
    for s in raw_sentences:
        s = s.strip()
        if 15 <= len(s) <= 200:
            sentences.append(s)

    if not sentences:
        # 줄 단위로 fallback
        sentences = [ln.strip() for ln in text.split("\n") if 15 <= len(ln.strip()) <= 200]

    if not sentences:
        return (text[:150] + "...") if len(text) > 150 else text

    total = len(sentences)
    scored = []
    for idx, sent in enumerate(sentences):
        score = 0.0

        # 키워드 점수
        kw_hits = sum(1 for kw in keywords if kw in sent)
        score += kw_hits * 2.0

        # 앞부분 가중치 (상위 30%)
        if idx < total * 0.3:
            score += 3.0

        # 숫자/% 포함
        if re.search(r"\d+[\.,]?\d*\s*(%|원|달러|배|명|개|개월|분기)", sent):
            score += 2.0

        # 길이 적정성 (30~100자 선호)
        if 30 <= len(sent) <= 100:
            score += 1.0

        # 제목과 단어 겹침
        if title:
            title_words = set(re.findall(r"[가-힣]{2,}", title))
            sent_words = set(re.findall(r"[가-힣]{2,}", sent))
            overlap = len(title_words & sent_words)
            score += overlap * 1.5

        scored.append((score, idx, sent))

    # 점수 내림차순 정렬 후 상위 문장 선택
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:max_sentences]

    # 원래 순서대로 재정렬
    top.sort(key=lambda x: x[1])
    summary = " ".join(s for _, _, s in top)

    return summary if summary else (text[:150] + "...")


# ──────────────────────────────────────────────
# 6. 쇼츠 스크립트 생성
# ──────────────────────────────────────────────
def generate_shorts_script(
    page_title: str,
    articles: list[dict],
    summaries: list[str],
) -> str:
    """
    수집된 요약을 바탕으로 60초 유튜브 쇼츠 스크립트를 생성한다.

    구간 구성:
      [0~5초]   훅 (Hook) - 관심을 끄는 한 문장
      [5~25초]  핵심 이슈 3개 - 각 기사 핵심 1문장
      [25~45초] 해석 - 종합적 의미 2문장
      [45~60초] 시사점 + 클로징 - 2문장
    """

    def _clean(text: str) -> str:
        """줄바꿈·연속 공백을 공백 1개로 정리한다."""
        return re.sub(r"\s+", " ", text).strip()

    def first_sentence(text: str, max_len: int = 60) -> str:
        """텍스트에서 첫 문장만 추출하고 max_len으로 자른다."""
        text = _clean(text)
        # 마침표/물음표/느낌표 + 공백 기준으로 분리
        parts = re.split(r"(?<=[.?!다.요])\s+", text)
        s = parts[0].strip() if parts else text.strip()
        s = _clean(s)
        return (s[:max_len] + "...") if len(s) > max_len else s

    def nth_sentence(text: str, n: int = 1, max_len: int = 70) -> str:
        """n번째 문장을 추출한다 (1-indexed)."""
        text = _clean(text)
        parts = re.split(r"(?<=[.?!다.요])\s+", text)
        parts = [_clean(p) for p in parts if _clean(p)]
        if len(parts) >= n:
            s = parts[n - 1]
        elif parts:
            s = parts[-1]
        else:
            s = text.strip()
        return (s[:max_len] + "...") if len(s) > max_len else s

    # 기사가 부족할 경우 채우기
    while len(summaries) < 3:
        summaries.append("관련 경제 동향을 주목해야 합니다.")
    while len(articles) < 3:
        articles.append({"title": "추가 경제 이슈", "url": ""})

    s0, s1, s2 = [_clean(s) for s in (summaries[0], summaries[1], summaries[2])]

    # ── [0~5초] 훅 ──────────────────────────────
    # 첫 번째 기사 요약에 숫자가 있으면 더 구체적인 훅
    num_match = re.search(r"\d+[\.,]?\d*\s*(%|원|달러|배)", s0)
    if num_match:
        hook = f"경제 지표에 {num_match.group()}! 이게 무슨 의미인지 60초에 알려드립니다."
    else:
        hook = "이번 달 꼭 알아야 할 경제 핵심 이슈 3가지, 60초로 정리해드립니다!"

    # ── [5~25초] 핵심 이슈 3개 ──────────────────
    issue1 = f"첫째, {first_sentence(s0, 60)}"
    issue2 = f"둘째, {first_sentence(s1, 60)}"
    issue3 = f"셋째, {first_sentence(s2, 60)}"

    # ── [25~45초] 해석 ───────────────────────────
    # 두 번째 문장 활용 또는 첫 번째 기사 요약의 다른 부분
    interp1_raw = nth_sentence(s0, 2, 70) if len(re.split(r"(?<=[.?!])\s", s0)) > 1 else nth_sentence(s1, 1, 70)
    interp2_raw = nth_sentence(s1, 2, 70) if len(re.split(r"(?<=[.?!])\s", s1)) > 1 else nth_sentence(s2, 1, 70)

    # 접속사·조사로 시작하는 문장은 접두어 추가 생략
    _CONJUNCTIONS = ("이", "그", "이는", "또한", "하지만", "따라서", "그러나",
                     "특히", "우리", "현재", "이에", "이로", "이와")
    interpretation1 = (
        interp1_raw if any(interp1_raw.startswith(c) for c in _CONJUNCTIONS)
        else f"이는 {interp1_raw}"
    )
    interpretation2 = (
        interp2_raw if any(interp2_raw.startswith(c) for c in _CONJUNCTIONS)
        else f"특히 {interp2_raw}"
    )

    # ── [45~60초] 시사점 + 클로징 ───────────────
    # 세 번째 기사에서 마지막 문장 추출 시도
    s2_parts = [p.strip() for p in re.split(r"(?<=[.?!])\s", s2) if p.strip()]
    implication_raw = s2_parts[-1] if len(s2_parts) >= 2 else first_sentence(s2, 65)
    implication = f"개인과 기업 모두 {implication_raw}" if not implication_raw.startswith(("개인", "기업", "우리")) else implication_raw
    if len(implication) > 80:
        implication = implication[:80] + "..."

    closing = "경제 흐름을 미리 읽으면 기회가 보입니다. 구독하고 매월 놓치지 마세요!"

    # ── 스크립트 조립 ────────────────────────────
    script = f"""\
[0~5초 훅]
{hook}

[5~25초 핵심 이슈 3개]
{issue1}
{issue2}
{issue3}

[25~45초 해석]
{interpretation1}
{interpretation2}

[45~60초 개인/기업 시사점 + 클로징]
{implication}
{closing}"""

    return script


# ──────────────────────────────────────────────
# 7. 결과 저장
# ──────────────────────────────────────────────
def save_output(page_title: str, script: str, articles: list[dict]) -> None:
    """
    생성된 스크립트를 outputs/output_script.txt 로 저장한다.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "60초 경제신호 v1",
        f"페이지 제목: {page_title}",
        f"생성일시: {now}",
        "-" * 40,
        "",
        script,
        "",
        "-" * 40,
        "※ 참고 기사 목록",
    ]
    for i, art in enumerate(articles, 1):
        lines.append(f"  [{i}] {art['title']}")
        lines.append(f"      {art['url']}")

    content = "\n".join(lines) + "\n"

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n[저장 완료] {OUTPUT_FILE}")


# ──────────────────────────────────────────────
# 메인 실행
# ──────────────────────────────────────────────
def main():
    print("=" * 50)
    print("60초 경제신호 v1 - 스크립트 생성 시작")
    print("=" * 50)
    print(f"대상 URL: {KDI_MONTH_URL}\n")

    # ── Step 1: 목록 페이지 HTML 가져오기 ──
    print("[1/5] 목록 페이지 HTML 가져오는 중...")
    try:
        list_html = fetch_html(KDI_MONTH_URL)
    except RuntimeError as e:
        print(e)
        sys.exit(1)
    print("  → 완료")

    # ── Step 2: 페이지 제목 추출 ──
    print("[2/5] 페이지 제목 추출 중...")
    page_title = extract_page_title(list_html)
    print(f"  → 제목: {page_title}")

    # ── Step 3: 기사 링크 수집 ──
    print(f"[3/5] 상위 {TOP_N}개 기사 링크 수집 중...")
    try:
        # KDI_MONTH_URL을 base로 써야 ./naraView.do → /publish/naraView.do 로 변환됨
        articles = extract_article_links(list_html, KDI_MONTH_URL, TOP_N)
    except ValueError as e:
        print(e)
        sys.exit(1)

    for i, art in enumerate(articles, 1):
        print(f"  [{i}] {art['title'][:50]}...")
        print(f"       {art['url']}")

    # ── Step 4 & 5: 기사 본문 수집 + 규칙 기반 요약 ──
    print("\n[4/5] 기사 본문 수집 및 요약 중...")
    summaries = []
    valid_articles = []
    total_chars = 0

    for i, art in enumerate(articles, 1):
        print(f"  [{i}/{len(articles)}] '{art['title'][:40]}' 처리 중...")
        text = fetch_article_text(art["url"], MAX_CHARS_PER_ARTICLE)

        if len(text) < MIN_ARTICLE_CHARS:
            print(f"    → [제외] 본문이 {MIN_ARTICLE_CHARS}자 미만입니다 (현재: {len(text)}자)")
            continue

        # 전체 최대 글자 수 제한
        remaining = MAX_TOTAL_CHARS - total_chars
        if remaining <= 0:
            print(f"    → [제외] 전체 최대 글자 수({MAX_TOTAL_CHARS:,}자) 초과")
            break

        text = text[:remaining]
        total_chars += len(text)
        print(f"    → 본문 {len(text):,}자 수집 완료 (누계: {total_chars:,}/{MAX_TOTAL_CHARS:,}자)")

        summary = summarize_rule_based(text, title=art["title"], max_sentences=3)
        print(f"    → 요약: {summary[:80]}...")

        summaries.append(summary)
        valid_articles.append(art)

    if not valid_articles:
        print("\n[오류] 유효한 기사를 수집하지 못했습니다.")
        print("  → 네트워크 상태 또는 사이트 구조를 확인하세요.")
        sys.exit(1)

    # ── Step 6: 쇼츠 스크립트 생성 ──
    print("\n[5/5] 60초 쇼츠 스크립트 생성 중...")
    script = generate_shorts_script(page_title, valid_articles, summaries)

    # ── 결과 출력 ──
    print("\n" + "=" * 50)
    print("60초 경제신호 v1")
    print(f"페이지 제목: {page_title}")
    print(f"생성일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 40)
    print(script)
    print("=" * 50)

    # ── Step 7: 파일 저장 ──
    save_output(page_title, script, valid_articles)

    # ── Step 8: SRT 자막 파일 생성 ──
    srt_result_path = None
    try:
        from core.srt_generator import generate_srt
        srt_result_path = generate_srt(OUTPUT_FILE, SRT_FILE)
        print(f"[SRT 생성 완료] {srt_result_path}")
    except Exception as e:
        print(f"[경고] SRT 생성 실패 (스크립트 생성은 정상 완료): {e}")

    # ── Step 9: Content DB 기록 저장 ──────────────────────────
    # content_db.json에 이번 실행 결과를 upsert한다.
    # 파일 생성/업데이트 실패 시 경고만 출력, 메인 파이프라인은 영향 없음.
    try:
        from core.content_manager import save_content_record
        _macro_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "macro.json"
        )
        save_content_record(
            topic="경제",
            macro_data_path=_macro_path,
            script_path=OUTPUT_FILE,
            srt_path=srt_result_path or SRT_FILE,
        )
    except Exception as e:
        print(f"[경고] Content DB 저장 실패 (스크립트 생성은 정상 완료): {e}")

    # ── Step 10: 거시지표 임계값 알림 이메일 (S2-4) ─────────────────
    # macro.json 기반으로 임계값 초과 지표 자동 감지 → 알림 이메일 발송.
    # EMAIL 설정이 있을 때만 동작. 발송 실패해도 메인 파이프라인 영향 없음.
    try:
        from core.emailer import check_macro_alerts, send_alert_email, is_configured
        import json as _json, pathlib as _pathlib
        _macro_path = _pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "macro.json"
        if is_configured() and _macro_path.exists():
            _macro_raw = _json.loads(_macro_path.read_text(encoding="utf-8"))
            _macro     = {k: v for k, v in _macro_raw.items() if not k.startswith("_")}
            _alerts    = check_macro_alerts(_macro)
            if _alerts:
                print(f"\n[알림] 임계값 초과 지표 {len(_alerts)}개 감지 — 알림 이메일 발송 중...")
                for _a in _alerts:
                    print(f"  [{_a['level'].upper()}] {_a['msg']}: {_a['value']}{_a['unit']}")
                send_alert_email(macro=_macro)
            else:
                print("[알림] 임계값 초과 지표 없음 — 알림 건너뜀")
        else:
            print("[알림] 이메일 설정 없음 또는 macro.json 없음 — 알림 건너뜀")
    except Exception as e:
        print(f"[경고] 알림 이메일 실패 (스크립트 생성은 정상 완료): {e}")

    # ── Step 11: 스크립트 이메일 발송 ────────────────────────────────
    # EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECIPIENTS 환경변수 또는
    # Streamlit Secrets [email] 설정이 있을 때만 발송.
    # 설정 없거나 발송 실패해도 메인 파이프라인에 영향 없음.
    try:
        from core.emailer import send_script_email, is_configured
        if is_configured():
            send_script_email(
                script_path=OUTPUT_FILE,
                srt_path=srt_result_path or SRT_FILE,
            )
        else:
            print("[이메일] 설정 없음 — 발송 건너뜀 (환경변수 EMAIL_SENDER/PASSWORD/RECIPIENTS 설정 시 활성화)")
    except Exception as e:
        print(f"[경고] 이메일 발송 실패 (스크립트 생성은 정상 완료): {e}")


if __name__ == "__main__":
    main()
