"""
core/kotra_parser.py
KOTRA 해외시장뉴스 구조화 파서 (V1.0 — 2026-03-15)

KOTRA 기사에는 단순 HTML 본문 외에 다음 구조가 존재한다:
  1. 핵심 요약 박스  — 기사 상단 요약 블록
  2. 본문 텍스트    — 본문 주요 내용
  3. 비교/데이터 표 — table 요소 → bullet 형태로 변환
  4. 첨부 PDF       — 핵심 분석 내용이 PDF에 포함된 경우 존재

이 모듈은 위 4가지를 추출·병합하여 LLM 입력을 구조화된 형태로 구성한다.

출력 구조:
  [기사 핵심 요약]
  ...

  [본문 주요 내용]
  ...

  [표 요약]
  ...

  [PDF 핵심 내용]
  ...

사용처:
  core/fetcher.py → fetch_detail() → KOTRA URL 감지 시 이 모듈 호출
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

_log = logging.getLogger(__name__)

# ── 라이브러리 선택적 임포트 ────────────────────────────
try:
    from bs4 import BeautifulSoup, Tag
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    _log.warning("[kotra_parser] bs4 미설치 — KOTRA 파서 비활성")

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

try:
    from pdfminer.high_level import extract_text as pdfminer_extract
    HAS_PDFMINER = True
except ImportError:
    HAS_PDFMINER = False

try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

import requests

# ── 상수 ────────────────────────────────────────────────
_KOTRA_DOMAINS = ("dream.kotra.or.kr", "www.kotra.or.kr", "kotra.or.kr")

# 핵심 요약 박스 CSS 셀렉터 후보 (우선순위 순)
_SUMMARY_BOX_SELECTORS = [
    "div.summary-box",
    "div.cont-summary",
    "#summaryBox",
    "div.view-summary",
    "div.article-summary",
    "div.point-box",
    "div.key-point",
    "div.highlight-box",
    "table.summary-tbl",
    "div.sumBox",
    "div.view_summary",
    "div.sum_box",
    "div.article_summary",
    # KOTRA 해외시장뉴스 특화 패턴
    "div.market-summary",
    "div.report-summary",
    "div.info-box",
    "div.notice-box",
]

# 본문 영역 CSS 셀렉터 후보
_BODY_SELECTORS = [
    "div.view-content",
    "#viewContent",
    "div.article-content",
    "div.cont-view",
    "div.board-view",
    "#boardContent",
    "div.view_con",
    "div.news-content",
    "div.contentArea",
    "div.view-area",
    "td.content",
    "div.cont_view",
    "div.bbs_view",
    "div.view_wrap",
    # KOTRA 실제 페이지 관찰(2026-03-15) — Chrome DOM 확인
    "div.view_txt",        # 경제통상 리포트 등 view_txt 클래스 사용
    "div.view-txt",        # 하이픈 변형
    "div.news_txt",        # 뉴스 본문 변형
    "div.contents_view",   # 일부 보드 타입
    "article",
]

# article 태그 fallback 사용 시 제거할 노이즈 태그/클래스
# (네비게이션, 버튼 영역 — 실제 본문이 아님)
_ARTICLE_NOISE_SELECTORS = [
    ".board_area",   # 이전글/다음글 네비게이션
    ".prevNnext",    # 이전/다음 링크
    ".btnAreaC",     # 목록 버튼
    ".aiRecommArea", # AI 추천 영역
    ".photoSlider",  # 사진 슬라이더
    ".lineList_v",   # 사진 목록
    ".util_l",       # 공감 버튼
    ".btn_scrap",    # 스크랩 버튼
    "nav", "script", "style", "header", "footer",
]

# PDF 첨부파일 링크 판별 패턴 (href 기반)
_PDF_HREF_PATTERNS = [
    r"fileDown\.do",
    r"bbsFileDown\.do",
    r"FileDown\.do",
    r"attachFileDown",
    r"downloadFile",
    r"file_download",
    r"filedownload",
    r"\.pdf(\?|$|#)",
    r"pdfDownload",
    r"reportDown",
]

# PDF 링크 텍스트 패턴 (앵커 텍스트 기반)
_PDF_TEXT_PATTERNS = [
    "미리보기", "첨부파일", "PDF", "pdf",
    "다운로드", "보고서", "원문", "전문",
]

# 표 변환 시 최대 행 수 (너무 긴 표는 앞 N행만 처리)
_TABLE_MAX_ROWS = 20
# 섹션별 최대 글자 수 (토큰 절약)
_SUMMARY_MAX_CHARS = 500
_BODY_MAX_CHARS    = 1500
_TABLE_MAX_CHARS   = 600
_PDF_MAX_CHARS     = 1500
# PDF 다운로드 타임아웃 (초)
_PDF_TIMEOUT = 10
# PDF 다운로드 최대 크기 (5MB)
_PDF_MAX_BYTES = 5 * 1024 * 1024


# ───────────────────────────────────────────────────────
# 1. URL 판별
# ───────────────────────────────────────────────────────

def is_kotra_url(url: str) -> bool:
    """URL이 KOTRA 도메인인지 판별."""
    if not url:
        return False
    parsed = urlparse(url)
    return any(parsed.netloc.endswith(d) for d in _KOTRA_DOMAINS)


# ───────────────────────────────────────────────────────
# 2. 핵심 요약 박스 추출
# ───────────────────────────────────────────────────────

def _extract_summary_box(soup: "BeautifulSoup") -> str:
    """
    KOTRA 기사 핵심 요약 박스 추출.
    CSS 셀렉터 후보를 순서대로 시도하고 첫 번째 히트를 반환.
    """
    for sel in _SUMMARY_BOX_SELECTORS:
        try:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator="\n", strip=True)
                text = _clean_text(text)
                if len(text) > 30:
                    return text[:_SUMMARY_MAX_CHARS]
        except Exception:
            continue

    # fallback: class/id에 'summary' 또는 'sum' 포함하는 div
    for div in soup.find_all("div"):
        cls_id = " ".join(div.get("class", [])) + (div.get("id") or "")
        if re.search(r'summar|sum[_-]|핵심|요약|point', cls_id, re.I):
            text = _clean_text(div.get_text(separator="\n", strip=True))
            if len(text) > 30:
                return text[:_SUMMARY_MAX_CHARS]

    return ""


# ───────────────────────────────────────────────────────
# 3. 본문 텍스트 추출
# ───────────────────────────────────────────────────────

def _extract_body_text(soup: "BeautifulSoup") -> str:
    """
    KOTRA 기사 본문 영역 추출.
    summary box와 table은 별도 추출하므로 여기서는 순수 텍스트만 반환.

    article 태그 fallback 시 네비게이션/버튼 영역(_ARTICLE_NOISE_SELECTORS)을
    먼저 제거하여 이전글/다음글 텍스트가 본문으로 오인식되지 않도록 처리.
    """
    for sel in _BODY_SELECTORS:
        try:
            el = soup.select_one(sel)
            if el:
                # 기본 노이즈 태그 제거
                for tag in el.find_all(["table", "nav", "script", "style", "header", "footer"]):
                    tag.decompose()
                # article fallback 시 KOTRA 전용 노이즈 CSS 클래스 추가 제거
                if sel == "article":
                    for noise_sel in _ARTICLE_NOISE_SELECTORS:
                        for tag in el.select(noise_sel):
                            try:
                                tag.decompose()
                            except Exception:
                                pass
                text = _clean_text(el.get_text(separator="\n", strip=True))
                if len(text) > 100:
                    return text[:_BODY_MAX_CHARS]
        except Exception:
            continue

    return ""


# ───────────────────────────────────────────────────────
# 4. 표(table) → bullet 변환
# ───────────────────────────────────────────────────────

def _table_to_bullets(table: "Tag") -> str:
    """
    BeautifulSoup table 요소를 bullet 텍스트로 변환.

    변환 방식:
      - 헤더(th) 행이 있으면 컬럼명을 앞에 붙여서 "컬럼: 값" 형태로 출력
      - 헤더 없으면 각 행을 " | " 로 구분된 한 줄로 출력
    """
    rows = table.find_all("tr")
    if not rows:
        return ""

    headers: list[str] = []
    bullets: list[str] = []

    for i, row in enumerate(rows[:_TABLE_MAX_ROWS]):
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        cell_texts = [_clean_text(c.get_text(strip=True)) for c in cells]
        cell_texts = [t for t in cell_texts if t]
        if not cell_texts:
            continue

        # 첫 행 or th만 있는 행 → 헤더로 처리
        is_header_row = all(c.name == "th" for c in cells)
        if i == 0 or is_header_row:
            headers = cell_texts
            continue

        # 데이터 행 → bullet 생성
        if headers and len(headers) == len(cell_texts):
            parts = [f"{h}: {v}" for h, v in zip(headers, cell_texts) if v]
            bullets.append("• " + " / ".join(parts))
        else:
            bullets.append("• " + " | ".join(cell_texts))

    return "\n".join(bullets)


def _extract_tables(soup: "BeautifulSoup", body_selector_used: Optional[str] = None) -> str:
    """
    KOTRA 기사 내 모든 표를 bullet 형태로 변환하여 반환.
    광고/내비 영역 표는 제외 (행 수 2 미만 또는 셀 수 과다).
    """
    results: list[str] = []
    total_chars = 0

    # 본문 영역 내 표만 추출 (가능한 경우)
    search_root = soup
    if body_selector_used:
        try:
            el = soup.select_one(body_selector_used)
            if el:
                search_root = el
        except Exception:
            pass

    for table in search_root.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue  # 1행짜리 표는 레이아웃용으로 스킵
        cells = table.find_all(["td", "th"])
        if len(cells) > 80:
            continue  # 셀 80개 초과 = 복잡한 레이아웃 표, 스킵

        bullets = _table_to_bullets(table)
        if bullets and len(bullets) > 20:
            results.append(bullets)
            total_chars += len(bullets)
            if total_chars >= _TABLE_MAX_CHARS:
                break

    return "\n\n".join(results)[:_TABLE_MAX_CHARS]


# ───────────────────────────────────────────────────────
# 5. PDF 첨부파일 링크 탐지
# ───────────────────────────────────────────────────────

def _find_pdf_links(soup: "BeautifulSoup", base_url: str) -> list[str]:
    """
    KOTRA 기사 페이지에서 PDF 첨부파일 다운로드 링크를 탐지.
    반환: 절대 URL 목록 (중복 제거, 최대 3개)
    """
    found: list[str] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue

        # href 패턴 매칭
        href_match = any(re.search(p, href, re.I) for p in _PDF_HREF_PATTERNS)

        # 앵커 텍스트 패턴 매칭
        anchor_text = a.get_text(strip=True)
        text_match = any(p in anchor_text for p in _PDF_TEXT_PATTERNS)

        # title/class 속성에 pdf 포함
        title_attr = (a.get("title", "") + " " + " ".join(a.get("class", []))).lower()
        attr_match = "pdf" in title_attr or "attach" in title_attr or "file" in title_attr

        if href_match or text_match or attr_match:
            # 절대 URL 변환
            abs_url = urljoin(base_url, href)
            if abs_url not in seen and abs_url.startswith("http"):
                seen.add(abs_url)
                found.append(abs_url)
                if len(found) >= 3:
                    break

    return found


# ───────────────────────────────────────────────────────
# 6. PDF 다운로드 및 텍스트 추출
# ───────────────────────────────────────────────────────

def _extract_pdf_text(pdf_url: str, referer: str = "") -> str:
    """
    PDF URL에서 텍스트를 추출한다.
    추출 엔진 우선순위: pdfplumber → pdfminer → pypdf

    반환: 추출된 텍스트 (실패 시 빈 문자열)
    """
    if not pdf_url:
        return ""

    # ── 다운로드 ──
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,*/*",
    }
    if referer:
        headers["Referer"] = referer

    try:
        resp = requests.get(
            pdf_url, headers=headers,
            timeout=_PDF_TIMEOUT, stream=True, verify=False,
        )
        if resp.status_code != 200:
            print(f"[kotra_parser] PDF 다운로드 실패: HTTP {resp.status_code} | {pdf_url[:60]}")
            return ""

        # 크기 제한
        pdf_bytes = b""
        for chunk in resp.iter_content(chunk_size=65536):
            pdf_bytes += chunk
            if len(pdf_bytes) > _PDF_MAX_BYTES:
                print(f"[kotra_parser] PDF 크기 초과 (>{_PDF_MAX_BYTES // 1024}KB) — 부분 추출")
                break

        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not pdf_url.lower().endswith(".pdf"):
            # Content-Type이 PDF가 아니고 URL에도 .pdf 없으면 확인
            if not pdf_bytes[:4] == b"%PDF":
                print(f"[kotra_parser] PDF 시그니처 불일치, 스킵: {pdf_url[:60]}")
                return ""

        print(f"[kotra_parser] PDF 다운로드 성공: {len(pdf_bytes) // 1024}KB | {pdf_url[:60]}")

    except Exception as e:
        print(f"[kotra_parser] PDF 다운로드 오류: {type(e).__name__}: {e} | {pdf_url[:60]}")
        return ""

    # ── 텍스트 추출 (pdfplumber → pdfminer → pypdf) ──
    pdf_io = io.BytesIO(pdf_bytes)
    text = ""

    # 1순위: pdfplumber (표·레이아웃 인식 우수)
    if HAS_PDFPLUMBER and not text:
        try:
            with pdfplumber.open(pdf_io) as pdf:
                pages_text = []
                for page in pdf.pages[:15]:  # 최대 15페이지
                    t = page.extract_text() or ""
                    if t.strip():
                        pages_text.append(t.strip())
                text = "\n\n".join(pages_text)
            print(f"[kotra_parser] pdfplumber 추출: {len(text)}자")
        except Exception as e:
            print(f"[kotra_parser] pdfplumber 오류: {e}")
            pdf_io.seek(0)

    # 2순위: pdfminer
    if HAS_PDFMINER and len(text) < 100:
        try:
            pdf_io.seek(0)
            text = pdfminer_extract(pdf_io) or ""
            text = text.strip()
            print(f"[kotra_parser] pdfminer 추출: {len(text)}자")
        except Exception as e:
            print(f"[kotra_parser] pdfminer 오류: {e}")
            pdf_io.seek(0)

    # 3순위: pypdf
    if HAS_PYPDF and len(text) < 100:
        try:
            pdf_io.seek(0)
            reader = pypdf.PdfReader(pdf_io)
            pages_text = []
            for page in reader.pages[:15]:
                t = page.extract_text() or ""
                if t.strip():
                    pages_text.append(t.strip())
            text = "\n\n".join(pages_text)
            print(f"[kotra_parser] pypdf 추출: {len(text)}자")
        except Exception as e:
            print(f"[kotra_parser] pypdf 오류: {e}")

    if not text or len(text) < 50:
        print(f"[kotra_parser] PDF 텍스트 추출 실패 (최종 {len(text)}자)")
        return ""

    return _clean_text(text)[:_PDF_MAX_CHARS]


# ───────────────────────────────────────────────────────
# 7. 텍스트 정제 유틸
# ───────────────────────────────────────────────────────

def _clean_text(text: str) -> str:
    """공백·반복 줄바꿈 정리."""
    if not text:
        return ""
    # 연속 공백 → 단일 공백
    text = re.sub(r"[ \t]+", " ", text)
    # 3줄 이상 연속 빈 줄 → 2줄
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 앞뒤 공백 제거
    return text.strip()


# ───────────────────────────────────────────────────────
# 8. 메인 진입점: KOTRA 구조화 파서
# ───────────────────────────────────────────────────────

def parse_kotra_article(
    html: str,
    url: str,
    html_body_fallback: str = "",
) -> dict:
    """
    KOTRA 기사 HTML을 구조화 파싱하여 LLM 입력용 텍스트를 생성한다.

    Args:
        html:              기사 페이지 원본 HTML
        url:               기사 URL (PDF 상대경로 → 절대경로 변환 기준)
        html_body_fallback: fetch_detail이 이미 추출한 HTML 본문 (있으면 활용)

    Returns dict:
        structured_text   : LLM 입력용 구조화 텍스트 (4개 섹션)
        summary_text      : 핵심 요약 박스 텍스트
        body_text         : 본문 텍스트
        table_text        : 표 변환 텍스트
        pdf_text          : PDF 추출 텍스트
        pdf_urls          : 탐지된 PDF URL 목록
        has_pdf           : PDF 탐지 여부
        parse_method      : 각 섹션 추출에 사용된 방법
        total_chars       : structured_text 총 글자 수
    """
    result = {
        "structured_text": "",
        "summary_text":    "",
        "body_text":       "",
        "table_text":      "",
        "pdf_text":        "",
        "pdf_urls":        [],
        "has_pdf":         False,
        "parse_method":    {},
        "total_chars":     0,
    }

    if not HAS_BS4 or not html:
        result["structured_text"] = html_body_fallback
        result["total_chars"] = len(html_body_fallback)
        return result

    _t0 = time.time()
    soup = BeautifulSoup(html, "lxml")

    # ── 섹션 1: 핵심 요약 박스 ──
    summary = _extract_summary_box(soup)
    result["summary_text"] = summary
    result["parse_method"]["summary"] = "css_selector" if summary else "not_found"
    print(f"[kotra_parser] 요약박스: {len(summary)}자")

    # ── 섹션 3: 표 → bullet 변환 (본문 추출 전 먼저 실행)
    # NOTE: _extract_body_text()가 table 태그를 decompose하므로 반드시 먼저 실행해야 함
    _table_count_raw = len(soup.find_all("table"))
    table_text = _extract_tables(soup)
    result["table_text"] = table_text
    result["parse_method"]["table"] = f"{_table_count_raw}개 탐지→{len(table_text)}자" if table_text else f"{_table_count_raw}개 탐지→없음"
    print(f"[kotra_parser] 표변환: {len(table_text)}자 (원본 테이블 {_table_count_raw}개)")

    # ── 섹션 2: 본문 텍스트 (표 추출 후 실행) ──
    body = _extract_body_text(soup)
    if not body and html_body_fallback:
        body = html_body_fallback[:_BODY_MAX_CHARS]
        result["parse_method"]["body"] = "html_fallback"
    else:
        result["parse_method"]["body"] = "css_selector" if body else "not_found"
    result["body_text"] = body
    print(f"[kotra_parser] 본문: {len(body)}자")

    # ── 섹션 4: PDF 첨부파일 (유형별 분기) ──
    # URL 기반 유형 분류 (HTML 없이 — soup은 이미 변형됨)
    kotra_type = classify_kotra_type(url, "")
    result["kotra_type"] = kotra_type

    pdf_urls = _find_pdf_links(soup, url)
    result["pdf_urls"] = pdf_urls
    result["has_pdf"] = len(pdf_urls) > 0

    pdf_text = ""
    if kotra_type == KOTRA_TYPE_PDF_ATTACHMENT:
        # PDF_ATTACHMENT 타입: data-atfilesn API 직접 호출 우선
        print(f"[kotra_parser] PDF_ATTACHMENT 타입 — API 직접 호출 시도")
        pdf_text = fetch_kotra_pdf_text(soup, url)
        if pdf_text:
            result["parse_method"]["pdf"] = f"api_direct ({len(pdf_text)}자)"
        elif pdf_urls:
            print(f"[kotra_parser] API 실패 — URL fallback: {pdf_urls[0][:60]}")
            pdf_text = _extract_pdf_text(pdf_urls[0], referer=url)
            result["parse_method"]["pdf"] = (
                f"url_fallback ({len(pdf_text)}자)" if pdf_text else "추출실패"
            )
        else:
            result["parse_method"]["pdf"] = "첨부없음"
    elif pdf_urls:
        print(f"[kotra_parser] PDF 링크 탐지: {len(pdf_urls)}개 → {pdf_urls[0][:60]}")
        pdf_text = _extract_pdf_text(pdf_urls[0], referer=url)
        result["parse_method"]["pdf"] = (
            f"추출성공 ({len(pdf_text)}자)" if pdf_text else "추출실패"
        )
    else:
        result["parse_method"]["pdf"] = "링크없음"
    result["pdf_text"] = pdf_text
    print(f"[kotra_parser] PDF텍스트: {len(pdf_text)}자")

    # ── LLM 입력 구조화 ──
    structured = _build_structured_input(summary, body, table_text, pdf_text)
    result["structured_text"] = structured
    result["total_chars"] = len(structured)

    _elapsed = round(time.time() - _t0, 2)
    print(
        f"[kotra_parser] ✅ 파싱 완료 — 총 {result['total_chars']}자 "
        f"(요약{len(summary)}+본문{len(body)}+표{len(table_text)}+PDF{len(pdf_text)}) "
        f"| {_elapsed}s"
    )
    return result


def _build_structured_input(
    summary: str,
    body: str,
    table_text: str,
    pdf_text: str,
) -> str:
    """
    4개 섹션을 LLM 입력용 구조화 텍스트로 조립한다.

    PDF 텍스트가 충분하면(≥ 300자) 본문보다 PDF를 우선 배치.
    각 섹션은 구분 태그로 감싸서 LLM이 섹션별 중요도를 파악할 수 있도록 한다.
    """
    sections: list[str] = []

    if summary:
        sections.append(f"[기사 핵심 요약]\n{summary}")

    # PDF 우선 전략: PDF가 충분히 길면 본문보다 먼저 배치
    if pdf_text and len(pdf_text) >= 300:
        if body:
            sections.append(f"[본문 주요 내용]\n{body}")
        if table_text:
            sections.append(f"[표 요약]\n{table_text}")
        sections.append(f"[PDF 핵심 내용 — 우선 참고]\n{pdf_text}")
    else:
        if body:
            sections.append(f"[본문 주요 내용]\n{body}")
        if table_text:
            sections.append(f"[표 요약]\n{table_text}")
        if pdf_text:
            sections.append(f"[PDF 핵심 내용]\n{pdf_text}")

    return "\n\n".join(sections)


# ───────────────────────────────────────────────────────
# 9. fetcher.py 통합용 래퍼
# ───────────────────────────────────────────────────────

def enrich_kotra_body(
    raw_html: str,
    url: str,
    existing_body: str = "",
) -> tuple[str, dict]:
    """
    fetch_detail()에서 호출하는 단일 진입점.
    KOTRA URL이면 구조화 파싱 후 강화된 본문 텍스트를 반환.
    비KOTRA URL이면 existing_body를 그대로 반환.

    Returns:
        (enriched_body, parse_info)
        enriched_body : LLM에 전달할 최종 텍스트
        parse_info    : 로깅·디버깅용 파싱 메타데이터
    """
    # ── [KOTRA] url detected ──────────────────────────────
    _is_kotra = is_kotra_url(url)
    print(f"[KOTRA] url detected={_is_kotra} | {url[:80]}")

    if not _is_kotra:
        return existing_body, {"skipped": "non-kotra-url"}

    # 유형 분류 (raw_html이 있으면 HTML 특징도 반영)
    _kotra_type = classify_kotra_type(url, raw_html)
    print(f"[KOTRA] type={_kotra_type}")

    if not raw_html:
        print(f"[KOTRA] enrich fallback=empty_html | existing_body={len(existing_body)}자")
        return existing_body, {"skipped": "empty-html"}

    # ── SPA/이미지형 기사 — full_body 분석 제외, 메타 fallback 적용 ──
    # Chrome 실측(2026-03-15): MENU_ID=1560 기사의 본문이 PNG 이미지로 삽입됨.
    # getKotraBoardContents.do AJAX 엔드포인트 미호출 확인 → 텍스트 추출 불가.
    # full_body 파싱 시도 없이 즉시 메타데이터 기반 fallback 반환.
    if _kotra_type == KOTRA_TYPE_SPA_AJAX:
        try:
            from bs4 import BeautifulSoup as _BS
            soup_spa = _BS(raw_html, "html.parser")
            fallback_text = _build_spa_image_fallback(soup_spa, url, existing_body)
        except Exception as _spa_e:
            print(f"[KOTRA] SPA fallback 오류(무시): {_spa_e}")
            fallback_text = existing_body
        return fallback_text, {
            "decision": "spa_image_fallback",
            "kotra_type": KOTRA_TYPE_SPA_AJAX,
            "total_chars": len(fallback_text),
        }

    parse_result = parse_kotra_article(raw_html, url, html_body_fallback=existing_body)

    # ── [KOTRA] 섹션별 길이 리포트 ───────────────────────
    _sum_len   = len(parse_result["summary_text"])
    _body_len  = len(parse_result["body_text"])
    _table_len = len(parse_result["table_text"])
    _pdf_urls  = parse_result["pdf_urls"]
    _pdf_len   = len(parse_result["pdf_text"])
    _total     = parse_result["total_chars"]

    print(f"[KOTRA] summary_box length={_sum_len}")
    print(f"[KOTRA] body_text length={_body_len}")
    print(f"[KOTRA] tables length={_table_len}")
    print(f"[KOTRA] pdf_links found={len(_pdf_urls)} | {_pdf_urls[:1]}")
    print(f"[KOTRA] pdf_text length={_pdf_len}")

    structured = parse_result["structured_text"]
    total_chars = _total

    # 구조화 텍스트가 기존 본문보다 짧으면 기존 본문 유지
    if total_chars < len(existing_body) * 0.8 and not parse_result["has_pdf"]:
        print(
            f"[KOTRA] enrich fallback=kept_original | structured={total_chars}자 < existing={len(existing_body)}자"
        )
        print(f"[KOTRA] final structured body length={len(existing_body)} (original kept)")
        return existing_body, {**parse_result["parse_method"], "decision": "kept_original"}

    _final_body = structured or existing_body
    _decision = "used_structured" if structured else "fallback_to_existing"
    print(f"[KOTRA] enrich success={bool(structured)} | decision={_decision}")
    print(f"[KOTRA] final structured body length={len(_final_body)}")

    parse_info = {
        **parse_result["parse_method"],
        "decision": _decision,
        "total_chars": total_chars,
        "has_pdf": parse_result["has_pdf"],
        "pdf_urls": _pdf_urls,
    }
    return _final_body, parse_info


# ═══════════════════════════════════════════════════════════
# 10. KOTRA 기사 유형 분류기 (V1.1 신규, 2026-03-15)
# ═══════════════════════════════════════════════════════════

# ── 기사 유형 상수 ────────────────────────────────────────
KOTRA_TYPE_STATIC_HTML    = "STATIC_HTML"    # 정적 HTML에 본문 포함
KOTRA_TYPE_SPA_AJAX       = "SPA_AJAX"       # AJAX 동적 로드 — 정적 fetch 불가
KOTRA_TYPE_PDF_ATTACHMENT = "PDF_ATTACHMENT" # 본문 없고 PDF 첨부만 있는 타입

# MENU_ID 기준 분류 맵 (Chrome DOM 관찰 결과, 2026-03-15)
# 추후 실측으로 계속 업데이트 필요
_KOTRA_SPA_MENU_IDS = {
    "1560",  # 글로벌 공급망 인사이트  — SPA AJAX 방식 확인
}
_KOTRA_PDF_MENU_IDS = {
    "1580",  # 글로벌 이슈 모니터링 경제통상 리포트 — PDF 첨부 위주
    "1010",  # 보고서 (추정)
    "40",    # 보고서 메인 메뉴 (추정)
}
# 나머지 MENU_ID는 기본적으로 STATIC_HTML 처리


def classify_kotra_type(url: str, html: str = "") -> str:
    """
    KOTRA 기사 URL + HTML 특징으로 콘텐츠 타입을 분류.

    분류 우선순위:
      1. URL 파라미터 (MENU_ID) → SPA / PDF / STATIC 판별
      2. HTML 구조 특징 (view_txt 존재 여부, board_area 비어 있는지)
      3. PDF 첨부 여부

    Returns:
      "STATIC_HTML"    — div.view_txt 등에 본문 있음, 정적 fetch 가능
      "SPA_AJAX"       — board_area만 있고 본문 없음, AJAX 렌더링 필요
      "PDF_ATTACHMENT" — 본문 매우 짧고 PDF 첨부 있음
    """
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    menu_id = qs.get("MENU_ID", qs.get("menu_id", [""]))[0]

    # ── URL 기반 1차 분류 ──
    if menu_id in _KOTRA_SPA_MENU_IDS:
        kotra_type = KOTRA_TYPE_SPA_AJAX
    elif menu_id in _KOTRA_PDF_MENU_IDS:
        kotra_type = KOTRA_TYPE_PDF_ATTACHMENT
    else:
        kotra_type = KOTRA_TYPE_STATIC_HTML  # default

    # ── HTML 특징 기반 2차 검증 (HTML이 있을 때만) ──
    if html and HAS_BS4:
        try:
            soup = BeautifulSoup(html, "lxml")

            # view_txt 또는 view-content 존재 → STATIC
            has_body_div = bool(
                soup.select_one("div.view_txt") or
                soup.select_one("div.view-content") or
                soup.select_one("div.view_content") or
                soup.select_one("div.news-content")
            )

            # board_area 내 content div가 비어 있는지
            board_area = soup.select_one(".board_area")
            board_has_content = False
            if board_area:
                # content div 존재 여부 (네비 제외)
                for sel in ["div.view_txt", "div.content", "div.news_txt", "p.content"]:
                    if board_area.select_one(sel):
                        board_has_content = True
                        break

            # PDF 링크 존재
            has_pdf_link = bool(_find_pdf_links(soup, url))

            # 본문 텍스트 실제 길이 (article 기준, 노이즈 제거)
            art = soup.select_one("article")
            art_text_len = 0
            if art:
                for noise in _ARTICLE_NOISE_SELECTORS:
                    for t in art.select(noise):
                        try: t.decompose()
                        except: pass
                # 노이즈 제거 후 view_txt 존재하면 그 텍스트 길이
                vt = art.select_one("div.view_txt")
                if vt:
                    art_text_len = len(vt.get_text(strip=True))

            # 재분류 로직
            if has_body_div and art_text_len > 80:
                # 본문 컨텐츠 확인 → STATIC 또는 PDF_ATTACHMENT
                if has_pdf_link and art_text_len < 200:
                    kotra_type = KOTRA_TYPE_PDF_ATTACHMENT
                else:
                    kotra_type = KOTRA_TYPE_STATIC_HTML
            elif not has_body_div and not board_has_content:
                # 본문 없음 → SPA 가능성
                if has_pdf_link:
                    kotra_type = KOTRA_TYPE_PDF_ATTACHMENT
                else:
                    kotra_type = KOTRA_TYPE_SPA_AJAX
            # URL 기반 분류가 이미 PDF면 유지
            elif menu_id in _KOTRA_PDF_MENU_IDS and has_pdf_link:
                kotra_type = KOTRA_TYPE_PDF_ATTACHMENT

        except Exception as e:
            _log.debug("[classify_kotra_type] HTML 분석 오류: %s", e)

    print(f"[KOTRA] type={kotra_type} | menu_id={menu_id} | {url[-60:]}")
    return kotra_type


# ═══════════════════════════════════════════════════════════
# 11. PDF 첨부파일 직접 API 다운로드 (V1.1 신규, 2026-03-15)
# ═══════════════════════════════════════════════════════════

# KOTRA 파일 다운로드 API 엔드포인트
_KOTRA_FILE_DOWNLOAD_API = (
    "https://dream.kotra.or.kr/ajaxa/fileCpnt/fileDown.do"
    "?gbn=n01&nttSn={ntt_sn}&atFileSn={at_file_sn}&pFrontYn=Y"
)

# ── SPA/이미지형 기사에서 메타데이터 추출용 셀렉터 ────────
_KOTRA_TITLE_SELECTORS = [
    "h2.tit_view", "h2.view-title", "h1.article-title",
    ".view_title h2", ".view_title h1", ".tit_news",
    "h2", "h1",
]
_KOTRA_META_SELECTORS = [
    "ul.news_info", ".view_info", ".news_date", ".view_date",
    ".article-info", ".news_meta",
]


def _build_spa_image_fallback(soup: "BeautifulSoup", url: str, existing_body: str = "") -> str:
    """
    SPA/이미지형 KOTRA 기사의 텍스트 추출 fallback.

    본문이 이미지(PNG)로 삽입된 KOTRA 기사의 경우 텍스트 추출이 불가능하므로,
    제목·카테고리·날짜·국가·출처 등 메타데이터 기반 요약을 생성한다.
    Chrome 실측 결과(2026-03-15): MENU_ID=1560 기사의 본문 전체가 이미지 삽입.

    Returns:
        메타데이터 기반 fallback 텍스트 (기존 본문이 충분하면 그대로 반환)
    """
    # 기존 본문이 충분하면 그대로 사용
    if len(existing_body) >= 200:
        return existing_body

    parts: list[str] = []

    # 1) 제목 추출
    title = ""
    for sel in _KOTRA_TITLE_SELECTORS:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            title = el.get_text(strip=True)
            break
    if not title:
        # og:title 태그 시도
        og = soup.find("meta", property="og:title")
        if og:
            title = og.get("content", "")
    if title:
        parts.append(f"[제목] {title}")

    # 2) 메타정보 추출 (날짜·국가·카테고리·저자)
    for sel in _KOTRA_META_SELECTORS:
        el = soup.select_one(sel)
        if el:
            meta_text = _clean_text(el.get_text(separator=" ", strip=True))
            if len(meta_text) > 5:
                parts.append(f"[메타] {meta_text[:120]}")
            break

    # 3) og:description (SNS 공유 요약)
    og_desc = soup.find("meta", property="og:description")
    if og_desc:
        desc = og_desc.get("content", "").strip()
        if len(desc) > 20:
            parts.append(f"[요약] {desc[:200]}")

    # 4) 기사 본문 이미지 alt 텍스트 수집 (내용 힌트)
    body_imgs = []
    for img in soup.select("div.board_area img, div.view_txt img, article img")[:5]:
        alt = img.get("alt", "").strip()
        if alt and len(alt) > 10:
            body_imgs.append(alt)
    if body_imgs:
        parts.append(f"[이미지 설명] {' / '.join(body_imgs[:3])}")

    fallback_text = "\n".join(parts) if parts else existing_body

    print(f"[KOTRA] image-based article fallback applied | {len(fallback_text)}자 생성")
    return fallback_text or existing_body


def _extract_attachment_params(soup: "BeautifulSoup") -> list[dict]:
    """
    HTML에서 KOTRA 첨부파일 파라미터 추출.

    KOTRA 첨부파일 링크 패턴:
      <a href="#;" onclick="fn_fileDown(this)"
         data-atfilesn="12345"
         data-filename="report.pdf">
      또는:
      <a href="#;" onclick="fn_filePreview(this)"
         data-atfilesn="12345">

    nttSn은 sendForm 폼의 hidden input에서 추출:
      <input type="hidden" name="pNttSn" value="239778">

    Returns: [{"ntt_sn": "239778", "at_file_sn": "12345", "filename": "report.pdf"}, ...]
    """
    params_list = []

    # nttSn (게시물 번호) 추출
    ntt_sn = ""
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        if name in ("pNttSn", "nttSn", "NTT_SN"):
            ntt_sn = inp.get("value", "").strip()
            if ntt_sn:
                break

    # URL에서도 pNttSn 추출 시도 (백업)
    # (URL 파라미터는 호출 측에서 전달받아야 하므로 여기서는 form 우선)

    if not ntt_sn:
        return params_list

    # 첨부파일 링크 수집
    for a in soup.find_all("a", {"href": "#;"}):
        onclick = a.get("onclick", "")
        at_file_sn = a.get("data-atfilesn", "").strip()
        filename = a.get("data-filename", a.get_text(strip=True)).strip()

        if not at_file_sn:
            continue
        if "fileDown" not in onclick and "filePreview" not in onclick:
            continue

        # PDF 파일인지 확인
        is_pdf = (
            filename.lower().endswith(".pdf") or
            "pdf" in filename.lower() or
            "fileDown" in onclick
        )
        if not is_pdf:
            continue

        params_list.append({
            "ntt_sn":     ntt_sn,
            "at_file_sn": at_file_sn,
            "filename":   filename[:100],
        })

    return params_list


def download_kotra_attachment(
    ntt_sn: str,
    at_file_sn: str,
    referer: str = "",
    timeout: int = 15,
) -> bytes:
    """
    KOTRA 첨부파일 다운로드 API 직접 호출.

    KOTRA 파일 다운로드 엔드포인트:
      GET /ajaxa/fileCpnt/fileDown.do?gbn=n01&nttSn={N}&atFileSn={M}&pFrontYn=Y

    Args:
        ntt_sn:     게시물 번호 (pNttSn, 예: "239778")
        at_file_sn: 첨부파일 번호 (data-atfilesn, 예: "12345")
        referer:    Referer 헤더에 설정할 기사 원문 URL
        timeout:    요청 타임아웃 (초)

    Returns:
        성공 시 PDF 바이트 데이터, 실패 시 b""
    """
    api_url = _KOTRA_FILE_DOWNLOAD_API.format(
        ntt_sn=ntt_sn, at_file_sn=at_file_sn
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream,*/*",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    if referer:
        headers["Referer"] = referer

    print(f"[KOTRA] PDF API 호출: nttSn={ntt_sn} atFileSn={at_file_sn}")
    print(f"[KOTRA] PDF API URL: {api_url}")

    try:
        resp = requests.get(
            api_url,
            headers=headers,
            timeout=timeout,
            stream=True,
            verify=False,
        )

        status = resp.status_code
        content_type = resp.headers.get("Content-Type", "")
        content_disp  = resp.headers.get("Content-Disposition", "")
        print(f"[KOTRA] PDF API 응답: HTTP {status} | Content-Type: {content_type[:60]}")
        print(f"[KOTRA] PDF API Content-Disposition: {content_disp[:80]}")

        if status != 200:
            print(f"[KOTRA] PDF API 실패: HTTP {status}")
            return b""

        # 응답 수집
        pdf_bytes = b""
        for chunk in resp.iter_content(chunk_size=65536):
            pdf_bytes += chunk
            if len(pdf_bytes) > _PDF_MAX_BYTES:
                print(f"[KOTRA] PDF API 크기 초과 — 부분 수집 ({len(pdf_bytes)//1024}KB)")
                break

        # PDF 시그니처 확인
        if pdf_bytes[:4] == b"%PDF":
            print(f"[KOTRA] PDF API 성공: {len(pdf_bytes)//1024}KB (PDF 시그니처 확인)")
            return pdf_bytes
        elif "pdf" in content_type.lower() or "pdf" in content_disp.lower():
            print(f"[KOTRA] PDF API 성공: {len(pdf_bytes)//1024}KB (Content-Type 기반)")
            return pdf_bytes
        else:
            # 응답은 있지만 PDF가 아닌 경우 (로그인 페이지 등)
            preview = pdf_bytes[:200].decode("utf-8", errors="replace")
            print(f"[KOTRA] PDF API 비PDF 응답: {len(pdf_bytes)}바이트 | 미리보기: {preview[:80]}")
            return b""

    except Exception as e:
        print(f"[KOTRA] PDF API 오류: {type(e).__name__}: {e}")
        return b""


def fetch_kotra_pdf_text(soup: "BeautifulSoup", url: str) -> str:
    """
    KOTRA 기사 HTML에서 첨부파일 파라미터를 추출하고
    API를 통해 PDF를 직접 다운로드하여 텍스트를 반환.

    기존 _find_pdf_links() + _extract_pdf_text() 방식이
    href="#;" 구조에서 실패하는 문제를 해결하기 위한 대체 함수.

    Returns: 추출된 텍스트 (실패 시 "")
    """
    params_list = _extract_attachment_params(soup)
    if not params_list:
        print(f"[KOTRA] PDF 파라미터 추출 실패 — 첨부파일 없음")
        return ""

    print(f"[KOTRA] PDF 파라미터 {len(params_list)}건 추출: {params_list[0]}")

    for params in params_list[:2]:  # 최대 2개 시도
        pdf_bytes = download_kotra_attachment(
            ntt_sn=params["ntt_sn"],
            at_file_sn=params["at_file_sn"],
            referer=url,
        )
        if pdf_bytes:
            # BytesIO로 텍스트 추출
            import io
            pdf_io = io.BytesIO(pdf_bytes)
            text = _extract_pdf_text_from_bytes(pdf_io)
            if text:
                print(f"[KOTRA] PDF API 텍스트 추출 성공: {len(text)}자 | {params['filename'][:50]}")
                return text
            else:
                print(f"[KOTRA] PDF API 텍스트 추출 실패: {params['filename'][:50]}")

    return ""


def _extract_pdf_text_from_bytes(pdf_io) -> str:
    """BytesIO PDF 객체에서 텍스트 추출 (pdfplumber → pdfminer → pypdf 순)."""
    text = ""

    if HAS_PDFPLUMBER:
        try:
            with pdfplumber.open(pdf_io) as pdf:
                pages_text = []
                for page in pdf.pages[:15]:
                    t = page.extract_text() or ""
                    if t.strip():
                        pages_text.append(t.strip())
                text = "\n\n".join(pages_text)
            if text:
                return _clean_text(text)[:_PDF_MAX_CHARS]
        except Exception as e:
            print(f"[KOTRA] pdfplumber 오류: {e}")
            pdf_io.seek(0)

    if HAS_PDFMINER and len(text) < 100:
        try:
            pdf_io.seek(0)
            text = pdfminer_extract(pdf_io) or ""
            text = text.strip()
            if text:
                return _clean_text(text)[:_PDF_MAX_CHARS]
        except Exception as e:
            print(f"[KOTRA] pdfminer 오류: {e}")
            pdf_io.seek(0)

    if HAS_PYPDF and len(text) < 100:
        try:
            pdf_io.seek(0)
            reader = pypdf.PdfReader(pdf_io)
            pages_text = []
            for page in reader.pages[:15]:
                t = page.extract_text() or ""
                if t.strip():
                    pages_text.append(t.strip())
            text = "\n\n".join(pages_text)
            if text:
                return _clean_text(text)[:_PDF_MAX_CHARS]
        except Exception as e:
            print(f"[KOTRA] pypdf 오류: {e}")

    return ""


# ═══════════════════════════════════════════════════════════
# 12. 소비재 산업 기사 관련성 스코어링 (V1.1 신규, 2026-03-15)
# ═══════════════════════════════════════════════════════════

# 산업별 키워드 사전 (소비재·식품 특화)
_INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "소비재": [
        # 뷰티/화장품
        "화장품", "뷰티", "K-뷰티", "스킨케어", "색조", "기초화장", "BB크림",
        "마스크팩", "선크림", "화장품 수출", "코스메틱", "cosmetic",
        # 식품/농식품
        "식품", "K-푸드", "라면", "김치", "간식", "냉동식품", "즉석식품",
        "농식품", "수출식품", "반찬", "소주", "막걸리", "한국식품",
        # 생활용품
        "생활용품", "가정용품", "유아용품", "의류", "패션", "잡화",
        # 유통/소비
        "소비재", "소비재 수출", "유통", "리테일", "편의점", "온라인쇼핑",
        "H&B", "드럭스토어", "인플루언서",
        # 시장/국가
        "미국 소비", "일본 소비", "중국 소비", "동남아 소비", "유럽 소비",
        "K-콘텐츠", "한류", "K-pop",
    ],
    "반도체": [
        "반도체", "메모리", "DRAM", "NAND", "파운드리", "칩", "웨이퍼",
        "시스템반도체", "HBM", "AI칩", "semiconductor",
    ],
    "자동차": [
        "자동차", "전기차", "EV", "완성차", "SUV", "배터리차", "USMCA",
        "관세", "차관세", "automotive",
    ],
    "2차전지": [
        "배터리", "2차전지", "리튬", "양극재", "음극재", "전해질",
        "배터리셀", "ESG배터리", "battery",
    ],
    "조선": [
        "조선", "선박", "LNG선", "컨테이너선", "해운", "조선소", "VLCC",
    ],
    "철강": [
        "철강", "강철", "냉연", "열연", "철판", "스테인리스", "알루미늄",
        "H빔", "금속",
    ],
    "석유화학": [
        "석유화학", "화학", "플라스틱", "에틸렌", "납사", "정유", "폴리머",
        "페트로케미컬",
    ],
    "일반수출": [
        "수출", "무역", "FTA", "관세", "통상", "RCEP", "공급망",
    ],
    "일반": [
        "수출", "수입", "무역", "FTA", "관세", "통상", "RCEP", "공급망",
        "환율", "달러", "경상수지", "글로벌", "해외시장", "시장동향",
        "경제", "산업", "기업", "투자", "성장", "제조",
    ],
}

# KOTRA MENU_ID별 카테고리 가중치 (소비재 산업 기준)
_KOTRA_MENU_RELEVANCE: dict[str, float] = {
    # 소비재 관련 높음
    "1120": 1.5,  # 소비재 (추정)
    "1130": 1.5,  # 식품
    "1140": 1.3,  # 생활·화학
    # 통상 일반 — 소비재 기사가 혼재
    "1580": 0.7,  # 글로벌 이슈 모니터링 (경제통상 리포트)
    "1560": 0.6,  # 글로벌 공급망 인사이트
    # 기타
    "1080": 0.9,  # 글로벌 이슈 뉴스
    "10":   1.0,  # 뉴스 메인 (기본)
}
_MENU_RELEVANCE_DEFAULT = 1.0


def score_kotra_relevance(
    title: str,
    url: str = "",
    body: str = "",
    industry_key: str = "소비재",
    category: str = "",
) -> dict:
    """
    KOTRA 기사의 산업 관련성 점수를 계산한다.

    점수 구성:
      - 제목 키워드 히트: 0~40점 (핵심 지표)
      - 본문 키워드 히트: 0~30점
      - MENU_ID 카테고리 보정: ×0.6~×1.5
      - 카테고리 텍스트 히트: 0~15점
      - 소비재 역관련 패널티: -20점 (공급망/반도체/자동차 등 명백한 타산업)

    Args:
        title:        기사 제목
        url:          기사 URL (MENU_ID 추출용)
        body:         기사 본문 텍스트 (있으면 활용)
        industry_key: 산업 키워드 (기본 "소비재")
        category:     KOTRA 카테고리 텍스트 (예: "글로벌 이슈 모니터링")

    Returns dict:
        score:       최종 관련성 점수 (0~100)
        title_hits:  제목에서 히트한 키워드 목록
        body_hits:   본문에서 히트한 키워드 목록
        menu_factor: MENU_ID 보정 계수
        reason:      점수 산출 근거 설명
    """
    from urllib.parse import urlparse, parse_qs

    keywords = _INDUSTRY_KEYWORDS.get(
        industry_key,
        _INDUSTRY_KEYWORDS.get("일반", _INDUSTRY_KEYWORDS.get("일반수출", [])),
    )
    # 타산업 키워드 (패널티 기준)
    other_industries = [k for k in _INDUSTRY_KEYWORDS if k != industry_key and k != "일반수출"]
    other_kw = []
    for ind in other_industries:
        other_kw.extend(_INDUSTRY_KEYWORDS[ind])

    title_lower = title.lower()
    body_lower  = (body or "")[:500].lower()
    cat_lower   = (category or "").lower()

    # ── 1. 제목 키워드 히트 (최대 40점) ──
    title_hits = [kw for kw in keywords if kw.lower() in title_lower]
    title_score = min(40, len(title_hits) * 12)

    # ── 2. 본문 키워드 히트 (최대 30점) ──
    body_hits = [kw for kw in keywords if kw.lower() in body_lower]
    body_score = min(30, len(body_hits) * 8)

    # ── 3. 카테고리 텍스트 히트 (최대 15점) ──
    cat_hits = [kw for kw in keywords if kw.lower() in cat_lower]
    cat_score = min(15, len(cat_hits) * 10)

    # ── 4. MENU_ID 보정 계수 ──
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    menu_id = qs.get("MENU_ID", qs.get("menu_id", ["10"]))[0]
    menu_factor = _KOTRA_MENU_RELEVANCE.get(menu_id, _MENU_RELEVANCE_DEFAULT)

    # ── 5. 타산업 패널티 ──
    penalty = 0
    penalty_kw = []
    for kw in other_kw:
        if kw.lower() in title_lower:
            penalty_kw.append(kw)
    # 타산업 키워드가 2개 이상이면 패널티 부과
    if len(penalty_kw) >= 2:
        penalty = 20
    elif len(penalty_kw) == 1:
        penalty = 8

    # ── 최종 점수 계산 ──
    raw_score  = title_score + body_score + cat_score - penalty
    final_score = min(100, max(0, int(raw_score * menu_factor)))

    # ── 근거 설명 ──
    reason_parts = []
    if title_hits:
        reason_parts.append(f"제목 히트: {title_hits}")
    if body_hits:
        reason_parts.append(f"본문 히트: {body_hits}")
    if cat_hits:
        reason_parts.append(f"카테고리 히트: {cat_hits}")
    if menu_factor != 1.0:
        reason_parts.append(f"MENU_ID={menu_id} 보정 ×{menu_factor}")
    if penalty_kw:
        reason_parts.append(f"타산업 패널티: {penalty_kw[:3]} → -{penalty}점")
    if not reason_parts:
        reason_parts.append("키워드 미히트 — 기본 점수")

    return {
        "score":       final_score,
        "raw_score":   raw_score,
        "title_hits":  title_hits,
        "body_hits":   body_hits,
        "penalty_kw":  penalty_kw,
        "menu_id":     menu_id,
        "menu_factor": menu_factor,
        "reason":      " | ".join(reason_parts),
    }


def rank_articles_by_relevance(
    articles: list[dict],
    industry_key: str = "소비재",
    top_n: int = 5,
) -> list[dict]:
    """
    기사 목록을 관련성 점수 기준으로 재정렬하여 Top N을 반환.

    Args:
        articles:     {"title": ..., "url": ..., "body": ..., "category": ...} 리스트
        industry_key: 산업 키워드
        top_n:        반환할 상위 기사 수

    Returns:
        각 기사에 "relevance_score", "relevance_reason" 필드 추가된 목록 (점수 내림차순)
    """
    scored = []
    for art in articles:
        score_result = score_kotra_relevance(
            title=art.get("title", ""),
            url=art.get("url", ""),
            body=art.get("body", art.get("summary", "")),
            industry_key=industry_key,
            category=art.get("category", ""),
        )
        art_copy = dict(art)
        art_copy["relevance_score"]  = score_result["score"]
        art_copy["relevance_reason"] = score_result["reason"]
        art_copy["relevance_detail"] = score_result
        scored.append(art_copy)

        try:
            print(
                f"[KOTRA] relevance score={score_result['score']:>3} "
                f"| {art.get('title','')[:50]} "
                f"| {score_result['reason'][:80]}"
            )
        except (UnicodeEncodeError, OSError):
            pass

    scored.sort(key=lambda x: x["relevance_score"], reverse=True)
    return scored[:top_n]
