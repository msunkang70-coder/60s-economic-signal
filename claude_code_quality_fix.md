# 60s Economic Signal — 품질 수정 지시서 (3대 이슈)
> 작성일: 2026-03-06 | 스크린샷 품질 컨설팅 기반
> **이 파일을 Claude Code에 그대로 붙여넣어 순서대로 수정하라.**

---

## 사전 확인

```bash
# 작업 전 백업
cp app.py app.py.bak2
cp core/fetcher.py core/fetcher.py.bak
cp core/summarizer.py core/summarizer.py.bak

# 구문 검증 alias
alias check_app="python -c \"import ast; ast.parse(open('app.py').read()); print('✅ app.py OK')\""
alias check_fetch="python -c \"import ast; ast.parse(open('core/fetcher.py').read()); print('✅ fetcher.py OK')\""
alias check_summ="python -c \"import ast; ast.parse(open('core/summarizer.py').read()); print('✅ summarizer.py OK')\""
```

---

## 문제 1 — 정책 원문 링크 정합성 수정

### 원인 분석

`fetch_list()` 는 `extract_article_links()` 에서 `<a href="naraView.do?...">` 를 수집한다.
`_make_doc_id()` 는 URL의 `cidx` 파라미터로 doc_id를 만든다.
`fetch_detail(doc_id, url, title)` 은 이 URL로 본문을 가져온다.

**취약점**: KDI 사이트에서 목록 페이지의 링크 href가 `naraView.do?cidx=XXX` 형태가 아닐 경우,
혹은 JavaScript 렌더링 페이지일 경우 href와 실제 이동 URL이 다를 수 있다.

### FIX 1-A: `fetch_list()` 반환값에 URL 검증 로그 추가

**파일**: `core/fetcher.py`
**함수**: `fetch_list()`

`result.append(...)` 직전에 URL 검증 코드를 추가하라:

```python
# fetch_list() 내 result.append(...) 직전에 추가
# URL에 cidx가 없으면 경고 출력
p_check = parse_qs(urlparse(art["url"]).query)
if not p_check.get("cidx"):
    print(f"[fetch_list] ⚠️ cidx 없음 — doc_id fallback 사용: {art['url'][:80]}")

result.append({
    "doc_id":       doc_id,
    "title":        art["title"],
    "url":          art["url"],          # ← 이 URL이 fetch_detail에 그대로 전달됨
    "issue_yyyymm": issue_yyyymm,
    "category":     category,
})
print(f"[fetch_list] 수집: [{doc_id}] {art['title'][:40]} | {art['url'][:60]}")
```

### FIX 1-B: `app.py` 문서 뷰어에 원문 URL 명시적 표시 강화

**파일**: `app.py`
**위치**: `render_ui()` 내 우측 문서 뷰어 (`with col_r:`) 섹션

아래 기존 코드를 찾아서:
```python
if doc.get("url"):
    st.markdown(f"[🔗 원문 링크]({doc['url']})")
```

아래와 같이 교체하라 (URL 전체 표시 + 복사 가능):
```python
if doc.get("url"):
    st.markdown(
        f"**🔗 원문**: [{doc['url'][:70]}{'...' if len(doc['url'])>70 else ''}]({doc['url']})"
    )
    # URL 불일치 디버깅용: doc_id와 URL의 cidx 비교
    import re as _re2
    cidx_in_id  = doc["doc_id"].split("_")[1] if "_" in doc["doc_id"] else ""
    cidx_in_url = _re2.search(r"cidx=(\d+)", doc["url"])
    if cidx_in_url and cidx_in_id and cidx_in_url.group(1) != cidx_in_id:
        st.warning(
            f"⚠️ doc_id({cidx_in_id})와 URL cidx({cidx_in_url.group(1)})가 다릅니다. "
            f"fetch_detail이 올바른 URL을 사용하는지 확인하세요."
        )
```

### FIX 1-C: `fetch_detail()` 호출 시 URL 명시적 로그

**파일**: `app.py`
**위치**: `with col_r:` 섹션 내 `fetch_detail` 호출 부분

```python
# 수정 전
detail = fetch_detail(doc["doc_id"], doc["url"], doc["title"])

# 수정 후 — URL 일치 여부 확인 로그 추가
print(f"[app] fetch_detail 요청: doc_id={doc['doc_id']} url={doc['url'][:70]}")
detail = fetch_detail(doc["doc_id"], doc["url"], doc["title"])
print(f"[app] fetch_detail 결과: parse_status={detail.get('parse_status')} body_len={detail.get('body_len',0):,}")
```

**검증**: 목록 불러오기 → 기사 클릭 → 터미널 로그에서 doc_id/URL cidx 일치 확인

---

## 문제 2 — 3줄 요약 품질 및 구조 개선

### 원인 분석

현재 `summarize_rule_based()` 는 단순 **추출 요약** (extractive summarization):
- 점수 높은 문장 3개를 원문에서 그대로 뽑아 이어 붙임
- ① 정책 목적, ② 주요 내용, ③ 기대 효과 구조가 없음
- 3개 문장이 문맥 없이 나열되어 읽기 어려움

### FIX 2-A: `summarize_rule_based()` 에 구조화된 3줄 출력 추가

**파일**: `core/summarizer.py`
**함수**: `summarize_rule_based()`

기존 함수 **끝** 부분 (`return summary ...` 직전) 을 아래와 같이 교체하라:

```python
# ── 기존 추출 요약 ───────────────────────────────────
scored.sort(key=lambda x: (-x[0], x[1]))
top = sorted(scored[:max_sentences], key=lambda x: x[1])
extracted = [s for _, _, s in top]

# ── 구조화 3줄 요약 (목적 / 내용 / 효과) ─────────────
if max_sentences >= 3 and len(extracted) >= 2:
    return _structured_3line(sentences, extracted, title)

summary = " ".join(extracted)
return summary if summary else text[:150]
```

같은 파일 (`summarizer.py`) 에 아래 함수를 `summarize_rule_based()` **위**에 추가하라:

```python
# ── 목적·내용·효과 키워드 사전 ────────────────────────
_PURPOSE_KW  = ["위해", "목적", "추진", "도입", "시행", "마련", "검토", "계획", "방침", "의결"]
_CONTENT_KW  = ["통해", "적용", "확대", "강화", "지원", "개선", "조정", "변경", "시행", "운영"]
_EFFECT_KW   = ["기대", "전망", "예상", "효과", "영향", "결과", "증가", "감소", "완화", "개선"]


def _structured_3line(
    all_sents: list[str],
    top_sents: list[str],
    title: str = "",
) -> str:
    """
    정책 요약을 3줄 구조로 재구성한다.

    줄 1 (목적/배경): PURPOSE_KW 포함 문장 우선
    줄 2 (핵심 내용): CONTENT_KW 포함 문장 우선
    줄 3 (기대 효과): EFFECT_KW 포함 문장 우선

    각 줄이 채워지지 않으면 top_sents에서 순서대로 대체한다.
    """
    def _pick(candidates: list[str], keywords: list[str], used: set) -> str:
        # 키워드 있는 문장 우선
        for s in candidates:
            if s not in used and any(kw in s for kw in keywords):
                used.add(s)
                return s
        # 없으면 사용 안 된 candidates 중 첫 번째
        for s in candidates:
            if s not in used:
                used.add(s)
                return s
        return ""

    used: set[str] = set()

    line1 = _pick(all_sents[:max(1, len(all_sents) // 3)], _PURPOSE_KW,  used)
    line2 = _pick(all_sents,                                 _CONTENT_KW, used)
    line3 = _pick(all_sents,                                 _EFFECT_KW,  used)

    # 빈 줄은 top_sents로 보충
    fallback = [s for s in top_sents if s not in used]
    if not line1 and fallback: line1 = fallback.pop(0)
    if not line2 and fallback: line2 = fallback.pop(0)
    if not line3 and fallback: line3 = fallback.pop(0)

    # 너무 긴 문장 자르기 (70자 제한)
    def _clip(s: str, n: int = 70) -> str:
        return s[:n] + "…" if len(s) > n else s

    lines = [f"① {_clip(line1)}", f"② {_clip(line2)}", f"③ {_clip(line3)}"]
    return "\n".join(l for l in lines if l.strip() not in ["① ", "② ", "③ "])
```

### FIX 2-B: `app.py` 요약 표시 방식 개선

**파일**: `app.py`
**위치**: `with col_r:` 섹션 내 `📝 3줄 요약` 컨테이너

아래 기존 코드를 찾아서:
```python
if _pstatus == "success" and detail.get("summary_3lines"):
    st.write(detail["summary_3lines"])
```

아래로 교체하라 (줄바꿈 + 번호별 강조):
```python
if _pstatus == "success" and detail.get("summary_3lines"):
    summary_text = detail["summary_3lines"]
    # ① ② ③ 구조이면 줄별로 분리해서 표시
    if "①" in summary_text or "②" in summary_text:
        for line in summary_text.split("\n"):
            if line.strip():
                num = line[:1]  # ① ② ③
                rest = line[2:].strip()
                color = {"①": "#1e40af", "②": "#065f46", "③": "#7c2d12"}.get(num, "#1e293b")
                st.html(
                    f'<div style="padding:8px 12px;margin-bottom:6px;'
                    f'border-left:3px solid {color};background:#f8fafc;'
                    f'border-radius:0 6px 6px 0;font-size:13px;line-height:1.7;color:#1e293b">'
                    f'<span style="font-weight:800;color:{color}">{num}</span> {rest}'
                    f'</div>'
                )
    else:
        st.write(summary_text)
```

**검증**: 기사 클릭 후 3줄 요약이 ①②③ 구조로 파란/초록/갈색 왼쪽 테두리로 표시되는지 확인

---

## 문제 3 — 숫자 표기 형식 통일

### 원인 분석

현재 `val_str = str(data.get("value", ""))` 로 raw string을 그대로 사용한다.
- 환율 `1476` → `,` 없이 표시 (가독성 낮음)
- CPI `2.3` → 소수점 1자리 (기준 불일치)
- 수출증가율 `14.8` → 소수점 1자리

### FIX 3-A: 중앙 포매팅 함수 추가

**파일**: `app.py`
**위치**: `_auto_business_impact()` 함수 바로 **아래**에 추가

```python
# ── 지표별 숫자 표기 통일 함수 ────────────────────────────────
_FMT_CURRENCY = frozenset({"환율(원/$)", "원/100엔 환율"})   # 천 단위 콤마, 소수점 2자리
_FMT_PCT_2    = frozenset({                                  # % 소수점 2자리
    "소비자물가(CPI)", "수출증가율", "기준금리",
    "수출물가지수", "수입물가지수",
})


def _fmt_value(label: str, value_raw) -> str:
    """
    지표 레이블에 따라 숫자 표기 규칙을 적용한 문자열 반환.

    환율류 → 천 단위 콤마 + 소수점 2자리  예) 1,476.00
    %류    → 소수점 2자리                  예) 14.80
    기타    → 원본 유지
    """
    try:
        val = float(str(value_raw).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return str(value_raw)

    if label in _FMT_CURRENCY:
        return f"{val:,.2f}"          # 1,476.00
    if label in _FMT_PCT_2:
        return f"{val:.2f}"           # 14.80
    return str(value_raw)             # 기타: 원본 유지
```

### FIX 3-B: KPI 카드 `_render_kpi_section()` 에 포매팅 적용

**파일**: `app.py`
**함수**: `_render_kpi_section()`

아래를 찾아서:
```python
val_str  = str(data.get("value", ""))
```

아래로 교체:
```python
val_str  = _fmt_value(label, data.get("value", ""))
```

### FIX 3-C: Key Insights `_generate_macro_insights()` 에 포매팅 적용

**파일**: `app.py`
**함수**: `_generate_macro_insights()`

함수 내 `val_str = str(data.get("value", "0"))` 를 찾아서:
```python
val_str = str(data.get("value", "0"))
# ↓ 아래로 교체
val_str = _fmt_value(label, data.get("value", "0"))
```

### FIX 3-D: 보조 지표 `_render_secondary_indicators()` 에 포매팅 적용

**파일**: `app.py`
**함수**: `_render_secondary_indicators()`

```python
val_str = str(data.get("value", ""))
# ↓ 아래로 교체
val_str = _fmt_value(label, data.get("value", ""))
```

### FIX 3-E: HTML 리포트 `generate_report_html()` 에 포매팅 적용

**파일**: `app.py`
**함수**: `generate_report_html()` 내 `_card_html()` 또는 `macro_cards` 생성 부분

`d["value"]` 를 그대로 사용하는 곳을 찾아서 모두 `_fmt_value(label, d.get("value",""))` 로 교체:

```python
# macro_cards 생성 또는 _card_html() 내부
# 수정 전
f'<div class="macro-val">{d["value"]}{d.get("unit","")} ...'

# 수정 후
f'<div class="macro-val">{_fmt_value(label, d.get("value",""))}{d.get("unit","")} ...'
```

### FIX 3-F: Pulse Strip `_render_status_pulse_strip()` 에 포매팅 적용

**파일**: `app.py`
**함수**: `_render_status_pulse_strip()`

```python
val_str = str(data.get("value", ""))
# ↓ 아래로 교체
val_str = _fmt_value(label, data.get("value", ""))
```

---

## 최종 검증

```bash
# 1. 구문 오류 없는지 확인
check_app
check_fetch
check_summ

# 2. 포매팅 단위 테스트
python3 -c "
import sys; sys.path.insert(0, '.')
# app.py에서 _fmt_value 임포트 불가 → 직접 테스트
tests = [
    ('환율(원/$)',      '1476',  '1,476.00'),
    ('소비자물가(CPI)', '2.3',   '2.30'),
    ('수출증가율',      '14.8',  '14.80'),
    ('기준금리',        '2.5',   '2.50'),
    ('원/100엔 환율',   '913.38','913.38'),
    ('수출물가지수',    '12.2',  '12.20'),
    ('수입물가지수',    '8.7',   '8.70'),
]
from app import _fmt_value
all_pass = True
for label, raw, expected in tests:
    result = _fmt_value(label, raw)
    status = '✅' if result == expected else '❌'
    if result != expected: all_pass = False
    print(f'{status} {label}: {raw} → {result} (기대: {expected})')
print('\\n' + ('✅ 전체 통과' if all_pass else '❌ 일부 실패'))
"

# 3. 앱 실행 후 체크리스트
# streamlit run app.py

# 체크리스트
# [ ] 환율 KPI 카드: "1,476.00 원/$" 형식 (쉼표 포함)
# [ ] CPI: "2.30 %" 형식 (소수점 2자리)
# [ ] 수출증가율: "14.80 %" 형식
# [ ] Key Insights: 지표값 볼드 + 포매팅 적용
# [ ] 기사 클릭 시 3줄 요약이 ①②③ 구조로 표시됨
# [ ] 요약 ① 목적/배경, ② 핵심 내용, ③ 기대 효과 순서
# [ ] 터미널에 doc_id/URL 매핑 로그 출력됨
# [ ] HTML 리포트의 환율 값도 쉼표 포함
```

---

## 수정 범위 요약

| FIX | 파일 | 함수 | 예상 시간 |
|-----|------|------|----------|
| 1-A | `core/fetcher.py` | `fetch_list()` | 5분 |
| 1-B | `app.py` | `render_ui()` col_r 섹션 | 10분 |
| 1-C | `app.py` | `render_ui()` col_r 섹션 | 5분 |
| 2-A | `core/summarizer.py` | `summarize_rule_based()` + `_structured_3line()` 신규 | 30분 |
| 2-B | `app.py` | `render_ui()` 요약 표시 | 15분 |
| 3-A | `app.py` | `_fmt_value()` 신규 추가 | 10분 |
| 3-B~F | `app.py` | 각 렌더 함수 val_str 교체 | 20분 |
