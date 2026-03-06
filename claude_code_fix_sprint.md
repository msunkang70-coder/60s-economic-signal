# 60s Economic Signal — Claude Code 수정 지시서
> 작성일: 2026-03-06 | 스크린샷 컨설팅 기반 우선순위 수정 목록
> **이 파일을 Claude Code에 그대로 붙여넣어 순서대로 수정하라.**

---

## 사전 준비

```bash
# 작업 전 현재 상태 백업
cp app.py app.py.bak
cp core/ecos.py core/ecos.py.bak

# 구문 검증 함수 정의 (각 수정 후 반드시 실행)
alias check="python -c \"import ast; ast.parse(open('app.py').read()); print('✅ OK')\""
```

---

## FIX-1 [5분] Key Insights 마크다운 버그 수정

**파일**: `app.py`
**함수**: `_render_macro_overview_and_insights()`
**문제**: `**환율 1476원**` 에서 `**` 기호가 그대로 화면에 노출됨

`ins_html` 생성 부분을 찾아서 아래와 같이 수정하라:

```python
# 수정 전 (현재 코드)
ins_html = "".join(
    f'<div style="padding:10px 0;border-bottom:1px solid #1e3a5f;'
    f'font-size:13px;color:#e2e8f0;line-height:1.6">{ins}</div>'
    for ins in insights
)

# 수정 후 — **text** → <strong>text</strong> 변환 + 📌 → 컬러 마커
import re as _re

def _md_to_html(text: str) -> str:
    """**bold** → <strong>bold</strong> 변환."""
    return _re.sub(r'\*\*(.+?)\*\*', r'<strong style="color:#93c5fd">\1</strong>', text)

ins_html = "".join(
    f'<div style="padding:10px 0;border-bottom:1px solid #1e3a5f;'
    f'font-size:13px;color:#e2e8f0;line-height:1.6">{_md_to_html(ins)}</div>'
    for ins in insights
)
```

**검증**: `check` 실행 → 앱 실행 후 Key Insights 카드에서 지표 이름이 하늘색 볼드로 표시되는지 확인

---

## FIX-2 [10분] as_of 날짜 표기 정리

**파일**: `core/ecos.py`
**문제**: `"2026-03-06 (최근 거래일 기준)"`, `"2025-12 (최근 발표월)"` 등 verbose 포맷 혼재

아래 3곳을 찾아서 괄호 설명 제거:

```python
# 일별 (환율) — _fetch_daily_fx() 함수 내부
# 수정 전
as_of = f"{t[:4]}-{t[4:6]}-{t[6:]} (최근 거래일 기준)"
# 수정 후
as_of = f"{t[:4]}-{t[4:6]}-{t[6:]}"

# 월별 — _fetch_monthly_yoy() 및 _fetch_monthly_abs() 함수 내부
# 수정 전
as_of = f"{t[:4]}-{t[4:]} (최근 발표월)"
# 수정 후
as_of = f"{t[:4]}-{t[4:]}"
```

**검증**: `python core/ecos.py` 단독 실행 or `data/macro.json` 열어서 `as_of` 필드 확인

---

## FIX-3 [30분] 콘텐츠 관련성 필터 — 수출·무역 기사만 표시

**파일**: `app.py`
**문제**: KDI 나라경제 목록에 하이퍼로컬·동네상권 기사가 포함되어 타겟 유저(수출 중소기업)와 무관한 콘텐츠 노출

`render_ui()` 내 정책 콘텐츠 브라우저 섹션에서 `fetch_list()` 호출 직후에 아래 필터를 추가하라:

```python
# ── 관련성 필터 상수 (파일 상단 _STOP_WORDS 근처에 추가) ──
_RELEVANCE_KW: list[str] = [
    "수출", "수입", "무역", "환율", "금리", "물가", "경기", "투자",
    "기업", "산업", "성장", "고용", "재정", "부채", "공급망", "원자재",
    "통상", "관세", "FTA", "글로벌", "달러", "금융", "시장", "경상수지",
    "제조", "중소기업", "스타트업", "벤처", "혁신", "디지털", "반도체",
]
_IRRELEVANT_KW: list[str] = [
    "동네", "로컬", "하이퍼로컬", "동네책방", "당근", "카카오",
    "지역상권", "골목", "소상공인 창업", "프랜차이즈",
]

def _filter_relevant_docs(docs: list) -> tuple[list, list]:
    """관련성 높은 문서와 낮은 문서를 분리 반환."""
    relevant, others = [], []
    for d in docs:
        title = d.get("title", "")
        has_relevant   = any(kw in title for kw in _RELEVANCE_KW)
        has_irrelevant = any(kw in title for kw in _IRRELEVANT_KW)
        if has_relevant and not has_irrelevant:
            relevant.append(d)
        else:
            others.append(d)
    return relevant, others
```

`render_ui()` 내 `fetch_list()` 호출 직후:

```python
st.session_state.docs = fetch_list(url.strip(), int(top_n))
# ← 이 줄 바로 아래에 추가
relevant, others = _filter_relevant_docs(st.session_state.docs)
if relevant:
    st.session_state.docs = relevant
    st.toast(f"✅ {len(relevant)}건 관련 기사 필터링 완료 (전체 {len(relevant)+len(others)}건 중)")
```

`docs` 목록 좌측 하단에 관련성 낮은 기사 접기 expander 추가:

```python
if others:
    with st.expander(f"기타 기사 {len(others)}건 (관련성 낮음)"):
        for d in others:
            st.caption(f"📄 {d['title'][:50]}")
```

**검증**: 앱 실행 → '목록 불러오기' 클릭 → 하이퍼로컬/동네 기사가 메인 목록에서 사라지는지 확인

---

## FIX-4 [30분] HTML 리포트 MSion 브랜딩 적용

**파일**: `app.py`
**함수**: `generate_report_html()`
**문제**: 다운로드 리포트에 MSion 로고/브랜드 없음

`generate_report_html()` 함수 맨 앞에 로고 로드 추가:

```python
def generate_report_html(docs, sel_doc=None, detail=None) -> str:
    # ── 로고 base64 로드 ─────────────────────────────
    logo_tag = ""
    try:
        import base64
        logo_path = pathlib.Path(_BASE) / "assets" / "logo.png"
        if not logo_path.exists():
            logo_path = pathlib.Path(_BASE) / "assets" / "logo.svg"
        if logo_path.exists():
            mime = "image/png" if logo_path.suffix == ".png" else "image/svg+xml"
            b64  = base64.b64encode(logo_path.read_bytes()).decode()
            logo_tag = f'<img src="data:{mime};base64,{b64}" alt="MSion" style="height:32px;width:auto;margin-bottom:12px;display:block">'
    except Exception:
        logo_tag = '<div style="font-size:18px;font-weight:900;color:#0f2240;margin-bottom:12px">MSion</div>'
    ...
```

`generate_report_html()` 의 CSS에서 헤더 스타일 교체:

```css
/* 수정 전 */
.header { border-bottom: 3px solid #111; ... }
.header h1 { font-size: 26px; font-weight: 900; ... }

/* 수정 후 */
.header {
    background: linear-gradient(135deg, #071123 0%, #0f2240 100%);
    padding: 28px 36px;
    border-radius: 10px;
    margin-bottom: 28px;
}
.header h1 {
    font-size: 22px;
    font-weight: 900;
    color: #ffffff;
    margin: 0 0 6px;
    letter-spacing: -0.3px;
}
.meta { font-size: 12px; color: #94a3b8; line-height: 2; }
.meta b { color: #60a5fa; }
```

HTML body 내 `<div class="header">` 블록 교체:

```html
<div class="header">
  {logo_tag}
  <h1>60s Economic Signal — 정책 브리핑 리포트</h1>
  <div class="meta">
    작성일: <b>{today}</b> &nbsp;|&nbsp;
    데이터 기준: <b>{macro_date_disp}</b> &nbsp;|&nbsp;
    출처: <b>KDI 경제정보센터 · 한국은행 ECOS</b>
  </div>
</div>
```

footer도 교체:

```html
<div class="footer">
  <span style="color:#0f2240;font-weight:700">MSion</span> &nbsp;|&nbsp;
  본 리포트는 참고 자료로만 활용하십시오. &nbsp;|&nbsp;
  <a href="https://msion.ai" style="color:#3b82f6">msion.ai</a>
</div>
```

---

## FIX-5 [30분] HTML 리포트 거시지표 카드 계층화

**파일**: `app.py`
**함수**: `generate_report_html()`
**문제**: 7개 지표가 동일 크기 3열 그리드 — 중요도 구분 없음

`macro_cards` 생성 블록을 아래와 같이 교체:

```python
PRIMARY_LABELS = ["환율(원/$)", "소비자물가(CPI)", "수출증가율", "기준금리"]
SECONDARY_LABELS = ["원/100엔 환율", "수출물가지수", "수입물가지수"]

_STATUS_COLOR = {
    "normal":  "#22c55e",
    "caution": "#f59e0b",
    "warning": "#f97316",
    "danger":  "#ef4444",
}

def _card_html(label, d, large=False) -> str:
    status, _, status_lbl = _get_threshold_status(label, str(d.get("value", "")))
    bar_color = _STATUS_COLOR.get(status, "#22c55e")
    val_size  = "28px" if large else "20px"
    try:
        val_f  = float(str(d.get("value","0")).replace(",","").replace("+",""))
        impact = _auto_business_impact(label, val_f)
    except Exception:
        impact = ""
    impact_html = (
        f'<div style="font-size:10px;color:#1e40af;background:#eff6ff;'
        f'border-left:2px solid #3b82f6;padding:4px 8px;margin-top:8px;'
        f'border-radius:0 4px 4px 0;line-height:1.5">💡 {impact}</div>'
    ) if impact and large else ""
    badge = (
        f'<span style="background:{bar_color};color:#fff;padding:1px 7px;'
        f'border-radius:8px;font-size:9px;font-weight:700;margin-left:6px">'
        f'{status_lbl}</span>'
    ) if status_lbl else ""
    return (
        f'<div style="padding:{("18px" if large else "12px")};'
        f'border:1px solid #e2e8f0;border-top:3px solid {bar_color};'
        f'border-radius:8px;background:#fff">'
        f'<div style="font-size:10px;color:#94a3b8;margin-bottom:4px">'
        f'{label}{badge}</div>'
        f'<div style="font-size:{val_size};font-weight:900;color:#0f172a">'
        f'{d.get("value","")}'
        f'<span style="font-size:12px;color:#64748b;margin-left:2px">{d.get("unit","")}</span>'
        f'<span style="font-size:14px;color:{"#16a34a" if d.get("trend")=="▲" else "#dc2626" if d.get("trend")=="▼" else "#94a3b8"};margin-left:4px">'
        f'{"↑" if d.get("trend")=="▲" else "↓" if d.get("trend")=="▼" else "→"}'
        f'</span></div>'
        f'<div style="font-size:10px;color:#94a3b8;margin-top:4px">'
        f'{d.get("note","")} | {d.get("as_of","")}</div>'
        f'{impact_html}'
        f'</div>'
    )

primary_cards   = "".join(_card_html(l, _MACRO[l], large=True)  for l in PRIMARY_LABELS   if l in _MACRO)
secondary_cards = "".join(_card_html(l, _MACRO[l], large=False) for l in SECONDARY_LABELS if l in _MACRO)

macro_section = f"""
<div class="section">
  <h2>📈 핵심 거시지표</h2>
  <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:16px">
    {primary_cards}
  </div>
  <div style="font-size:10px;color:#94a3b8;font-weight:700;
              text-transform:uppercase;letter-spacing:1px;
              margin-bottom:8px;padding-top:8px;border-top:1px solid #f1f5f9">
    보조 지표 — 무역 심층
  </div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px">
    {secondary_cards}
  </div>
</div>
"""
```

HTML template에서 기존 `macro_cards` 사용 부분 교체:

```python
# 수정 전
  <div class="section">
    <h2>📈 거시지표 현황</h2>
    <div class="grid3">{macro_cards}</div>
  </div>

# 수정 후
  {macro_section}
```

---

## FIX-6 [20분] 리스크·기회 기본값 의미있게 개선

**파일**: `app.py`
**함수**: `_risk_opportunity()`
**문제**: 키워드 없으면 "현재 명확한 위험 신호 없음", "추가 데이터 확인 후 판단 권장" 반환 — 무의미

```python
def _risk_opportunity(text: str) -> tuple:
    risk_found = [w for w in _RISK_KW if w in text]
    opp_found  = [w for w in _OPP_KW  if w in text]

    if risk_found:
        risk = f"{'·'.join(risk_found[:2])} 관련 부정적 흐름 감지 — 선제적 리스크 점검 권장"
    else:
        # 수정: 거시지표 기반 기본 리스크 메시지
        fx = _MACRO.get("환율(원/$)", {})
        try:
            fx_val = float(str(fx.get("value","0")).replace(",",""))
        except Exception:
            fx_val = 0
        if fx_val >= 1450:
            risk = "고환율 지속 — 원자재 수입 원가 상승 압박 점검 필요"
        elif fx_val <= 1300:
            risk = "저환율 — 수출 가격경쟁력 약화 모니터링 필요"
        else:
            risk = "현재 단기 리스크 신호 낮음 — 글로벌 공급망 변동 지속 주시"

    if opp_found:
        opp = f"{'·'.join(opp_found[:2])} 관련 긍정적 신호 — 시장 확대 기회 검토"
    else:
        # 수정: 거시지표 기반 기본 기회 메시지
        export = _MACRO.get("수출증가율", {})
        try:
            ex_val = float(str(export.get("value","0")).replace("+",""))
        except Exception:
            ex_val = 0
        if ex_val > 5:
            opp = f"수출 +{ex_val}% 증가세 — 주요 수출 시장 확대 전략 검토 적기"
        else:
            opp = "거시 안정 구간 — 중장기 시장 다변화 및 신규 바이어 발굴 검토"

    return risk, opp
```

---

## FIX-7 [15분] 정책 강도 게이지 제거 → 정책 분류 배지로 교체

**파일**: `app.py`
**함수**: `_render_policy_summary()` + `generate_report_html()`
**문제**: 정책 강도 숫자(2/5)가 임의적이고 신뢰 불가

`_render_policy_summary()` 에서 아래 블록 제거:
```python
# 제거 대상
intens = _policy_intensity(docs)
bar = "●" * intens + "○" * (5 - intens)
st.markdown(f"**정책 강도** &nbsp; `{bar}` &nbsp; {intens}/5")
```

교체 (정책 분류 배지로):
```python
# 교체 내용 — 문서별 정책 성격 배지
ptype_counter = {}
for d in docs[:10]:
    pt = _classify_policy_type(d.get("title",""))
    ptype_counter[pt] = ptype_counter.get(pt, 0) + 1

if ptype_counter:
    dominant = max(ptype_counter, key=ptype_counter.get)
    bg, fg = _POLICY_TYPE_COLOR.get(dominant, ("#e8f4fd","#1a6fa8"))
    st.html(
        f'이번 달 정책 기조 &nbsp;'
        f'<span style="background:{bg};color:{fg};padding:3px 12px;'
        f'border-radius:12px;font-size:12px;font-weight:700">{dominant}</span>'
        f'<span style="font-size:11px;color:#94a3b8;margin-left:8px">'
        f'({ptype_counter[dominant]}건 / {min(len(docs),10)}건 분석)</span>'
    )
```

`generate_report_html()` 에서도 동일하게 강도 바 → 정책 기조 배지로 교체

---

## FIX-8 [10분] Gauge 차트 캡션 위치 버그 수정

**파일**: `app.py`
**함수**: `_render_trend_charts()`
**문제**: `st.caption()` 이 Gauge 차트 위에 표시됨

각 컬럼의 `st.caption(...)` 을 `st.plotly_chart()` 호출 직후(아래)에 오도록 순서 확인:

```python
# 반드시 이 순서를 유지할 것
st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
st.caption(f"기준: {as_of}  |  이전: {prev_fmt} {unit_label}")  # ← plotly_chart 다음에
```

---

## 최종 검증

```bash
# 1. 구문 오류 없는지 확인
python -c "import ast; ast.parse(open('app.py').read()); print('✅ app.py OK')"
python -c "import ast; ast.parse(open('core/ecos.py').read()); print('✅ ecos.py OK')"

# 2. 앱 실행 (별도 터미널)
streamlit run app.py

# 3. 체크리스트
# [ ] Key Insights 카드에서 지표값이 하늘색 볼드로 표시되는지
# [ ] 목록 불러오기 후 하이퍼로컬/동네 기사가 메인 목록에서 제거되는지
# [ ] HTML 리포트 다운로드 후 MSion 로고 + 다크 헤더 보이는지
# [ ] 리포트 거시지표가 4대 지표(대형) + 보조 3개(소형) 2단계로 분리되는지
# [ ] 정책 강도 ●●○○○ 가 사라지고 정책 기조 배지로 바뀌었는지
# [ ] as_of 날짜에 "(최근 발표월)" 괄호 설명이 없는지
```

---

## 수정 완료 후 다음 단계 (Sprint 2)

- `st.tabs()` 3탭 구조 전환 (📊 경제신호 / 📰 정책브리핑 / 📥 리포트)
- URL 입력창 숨기기 + 앱 시작 시 자동 목록 로드
- 기준금리 Gauge 차트 4번째 추가
- 환율 임계값 알림 배너
