# 60s Economic Signal — Claude Code 개발 핸드오프
> 최종 업데이트: 2026-03-06  |  작업 디렉토리: `60sec_econ_signal/`

---

## ✅ 완료된 작업 (이번 세션)

| 파일 | 변경 내용 |
|------|----------|
| `requirements.txt` | `plotly>=5.18.0`, `pandas>=2.0.0` 추가 |
| `app.py` | Global CSS 주입 (모바일 대응, 헤더/푸터 제거) |
| `app.py` | `_render_kpi_section()` — KPI 카드 값 폰트 46px, Plotly 수평 bullet bar 추가 |
| `app.py` | `_render_trend_charts()` — `st.line_chart` → Plotly `go.Indicator` gauge 차트 교체 |
| `app.py` | `_render_status_pulse_strip()` 신규 추가 — 헤더 직후 4개 지표 색상 스트립 |
| `app.py` | `render_ui()` 레이아웃 순서 재편 — Pulse Strip → KPI → Gauge Charts → Overview |

---

## 🛠️ Sprint 1 — 데이터 신뢰도 수정 (최우선)

### S1-1: 수출증가율 stat_code 재검증

**파일**: `core/ecos.py`
**현재 상태**:
- `stat_code = "403Y001"`, `item_code = "*AA"` (수출금액지수 총지수, YoY 계산)
- 현재 값: `14.8%` (2025-12 기준) — 관세청 발표치와 괴리 가능성 있음

**Claude Code 지시**:
```
ECOS Open API에서 아래 stat_code들을 직접 테스트해서
관세청 기준 수출증가율(YoY %)에 가장 근접한 코드를 확인하라.

API 테스트 URL 패턴:
https://ecos.bok.or.kr/api/StatisticSearch/{API_KEY}/json/kr/1/5/{STAT_CODE}/M/202401/202412/{ITEM_CODE}

후보 1: stat_code=403Y001 / item_code=*AA  (현재 사용 중)
후보 2: stat_code=301Y017 / item_code 확인 필요

검증 기준: 2025-12월 관세청 발표 수출증가율 YoY 값과 ±1%p 이내 일치 여부.
일치하는 stat_code/item_code 조합으로 core/ecos.py 수출증가율 블록을 업데이트하라.
코드 변경 후 python core/ecos.py 로 단독 실행해서 값 확인.
```

---

### S1-2: `_auto_note()` 전년동월 대비 표기 수정

**파일**: `core/ecos.py`  **함수**: `_auto_note()` (~라인 388)
**문제**: 수출증가율(yoy=True)인데 "전월 대비"로 출력되는 경우 있음

**수정 목표**:
```python
# YoY 지표 (CPI, 수출증가율 등) → 반드시 "전년동월 대비"
if yoy:
    return f"전년동월 대비 {abs(d):.1f}%p {up}"
# 절댓값 지표 (환율, 금리 등)
if "환율" in label:
    return f"전일 대비 {abs(d):.1f}원 {up}"
return f"전월 대비 {abs(d):.2f}%p {up}" if d != 0 else "전월 대비 동결"
```

**검증**: `data/macro.json` 에서 `수출증가율.note` 필드가 `"전년동월 대비 ~"` 로 시작하는지 확인

---

### S1-3: `as_of` 날짜 표기 형식 통일

**파일**: `core/ecos.py`  **라인**: ~334, ~354, ~379
**현재**: `"2025-12 (최근 발표월)"`, `"2026-03-05 (최근 거래일 기준)"` — 형식 비일관
**목표**: 괄호 설명 제거, 깔끔한 날짜만 표시

```python
# 일별 (환율)
as_of = f"{t[:4]}-{t[4:6]}-{t[6:]}"    # "2026-03-05"

# 월별 (CPI, 수출 등)
as_of = f"{t[:4]}-{t[4:]}"              # "2025-12"
```

---

### S1-4: 리포트 제목 숫자 공백 제거

**파일**: `app.py`  **함수**: `generate_report_html()`
**현재 문제**: `"나라경제 정책 리포트 #  5"` 처럼 번호 앞뒤 공백 발생
**수정**: 제목 생성 부분에서 `.strip()` 적용 또는 f-string 수정

---

## 🎨 Sprint 2 — UX & 기능 고도화

### S2-1: 환율 알림 배너

**파일**: `app.py`
**위치**: `render_ui()` 내 `_render_dashboard_header()` 호출 직후

```python
def _render_fx_alert() -> None:
    """환율 임계값 돌파 시 상단 경고 배너 표시."""
    fx = _MACRO.get("환율(원/$)", {})
    try:
        val = float(str(fx.get("value", "0")).replace(",", ""))
    except (ValueError, TypeError):
        return
    if val >= 1450:
        st.error(f"🚨 환율 위험 구간 진입 — {val:,.0f}원/$ (기준: 1,450원 초과)")
    elif val >= 1380:
        st.warning(f"⚠️ 환율 주의 구간 — {val:,.0f}원/$ (기준: 1,380원 초과)")
```

`render_ui()` 에서 `_render_dashboard_header()` 다음 줄에 `_render_fx_alert()` 삽입

---

### S2-2: Plotly Gauge — 기준금리 4번째 추가

**파일**: `app.py`  **함수**: `_render_trend_charts()`
`CHART_SPECS` 리스트에 아래 항목 추가:

```python
(
    "기준금리", "기준금리 (%)",
    [0, 6],
    [
        {"range": [0,   2.0], "color": "#fef9c3"},   # 저금리 주의
        {"range": [2.0, 3.5], "color": "#dcfce7"},   # 정상
        {"range": [3.5, 6.0], "color": "#ffedd5"},   # 고금리 경고
    ],
    2.0, "%",
),
```

그리고 `st.columns(3, gap="medium")` → `st.columns(4, gap="small")` 로 변경

---

### S2-3: 지표 8개 확장 — 경상수지 + 실업률 추가

**파일**: `core/ecos.py` `_SPECS` 딕셔너리에 추가
**ECOS stat_code 후보** (API 테스트 후 확인):

| 지표 | stat_code 후보 | item_code | yoy |
|------|---------------|-----------|-----|
| 경상수지(억달러) | `301Y013` | 확인 필요 | False |
| 실업률(%) | `901Y027` | 확인 필요 | False |

추가 후 반드시:
- `_THRESHOLDS` 딕셔너리에 임계값 추가
- `_auto_business_impact()` 에 해당 지표 케이스 추가

---

### S2-4: 주간 이메일 리포트

**신규 파일**: `core/weekly_email.py`
매주 월요일 `data/macro.json` 기반 HTML 리포트 생성 후 SMTP 발송
환경변수: `.env` 파일에서 `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`, `REPORT_TO` 로드

---

## 📱 Sprint 3 — 모바일 & 브랜딩

### S3-1: 모바일 레이아웃 검증

`app.py` 상단 Global CSS에 이미 미디어쿼리 포함됨. 실제 모바일 뷰에서 KPI 카드 4개가 세로로 스택되는지 확인.

### S3-2: 브랜딩 강화

- 헤더 `_render_dashboard_header()` 에 SVG 로고 삽입
- `st.set_page_config(page_title=...)` 을 `"60s Signal | 수출 경제 브리핑"` 으로 업데이트

---

## ⚙️ 로컬 실행 전 체크리스트

```bash
# 1. 의존성 설치 (plotly 신규 추가됨 — 반드시 재설치)
pip install -r requirements.txt

# 2. ECOS API 키 설정
export ECOS_API_KEY=your_key_here   # macOS/Linux
set ECOS_API_KEY=your_key_here      # Windows CMD

# 3. 앱 실행
streamlit run app.py

# 4. 문제 없으면 ECOS 업데이트 버튼 클릭 → macro.json 갱신 확인
```

---

## 📁 현재 파일 구조

```
60sec_econ_signal/
├── app.py                    ← 메인 Streamlit 앱 (1,805 lines)
├── requirements.txt          ← plotly, pandas 신규 추가
├── claude_code_next_sprint.md ← 이 파일
├── data/
│   ├── macro.json            ← 거시지표 7개 (ECOS 자동 갱신)
│   └── content_db.json       ← KDI 콘텐츠 DB
├── core/
│   ├── ecos.py               ← ECOS API 수집 + stat_code 정의
│   ├── fetcher.py            ← KDI 페이지 스크래핑
│   ├── content_manager.py    ← 콘텐츠 이력 관리
│   └── summarizer.py         ← 룰 기반 3줄 요약
└── outputs/                  ← HTML 리포트 저장
```

---

## 🔑 핵심 함수 맵 (app.py)

| 함수 | 역할 |
|------|------|
| `_render_dashboard_header()` | 다크 그라디언트 히어로 헤더 + 업데이트 시각 |
| `_render_status_pulse_strip()` | 4개 지표 색상 상태 스트립 (신규) |
| `_render_kpi_section()` | 46px KPI 카드 + Plotly 수평 bullet bar |
| `_render_trend_charts()` | Plotly go.Indicator 게이지 차트 (3개) |
| `_render_macro_overview_and_insights()` | 2열: 개요 + 다크 인사이트 카드 |
| `_render_secondary_indicators()` | 엔화·수출물가·수입물가 보조 지표 |
| `render_ui()` | 전체 레이아웃 조립 진입점 |
| `_get_threshold_status()` | 임계값 기반 신호등 색상 반환 |
| `_auto_business_impact()` | 지표별 수출 중소기업 영향 한 줄 해석 |
| `generate_report_html()` | HTML 리포트 생성 (다운로드용) |
