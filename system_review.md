# 60초 경제신호 — 전체 시스템 리뷰
생성일: 2026-03-06 | 대상 브랜치: main

---

## 총평

파이프라인 구조(ECOS → macro.json → main.py → SRT → Email → Streamlit)는 잘 설계되어 있고 backward-compatible 원칙이 일관되게 지켜지고 있다.
다만 **데이터 신선도 추적 부재**, **이메일 trend 필드 미반영**, **거시지표 개수 부족** 세 가지가 실사용 품질에 직접 영향을 미치는 우선순위 이슈다.

---

## 1. 데이터 가치 (Data Value)

### 현재 상태
| 지표 | 값 | 비교 기준 | 코멘트 |
|---|---|---|---|
| 환율(원/$) | 1,458 | 전일 대비 | "달러 강세·관세 불확실성으로 원화 약세 지속" |
| 수출증가율 | +3.2% | 전년동월(YoY) | "반도체 중심 회복, 대중 수출 부진" |
| 소비자물가(CPI) | +2.1% | 전년동월(YoY) | "목표 수렴 중, 서비스 물가 여전히 높음" |

### 문제점

**① 지표 3개는 너무 적다**
현재 환율·수출·물가만으로는 "경기 방향"을 판단하기 부족하다.
아래 2개를 추가하면 스크립트 품질이 크게 올라간다.

```python
# core/ecos.py _SPEC에 추가 권장
"기준금리": {
    "stat_code": "722Y001",
    "item_code": "0101000",
    "period":    "M",
    "unit":      "%",
    "frequency": "비정기",
},
"GDP성장률(실질)": {
    "stat_code": "200Y001",
    "item_code": "10111",
    "period":    "Q",
    "unit":      "%",
    "frequency": "분기",
},
```

**② 지표 간 서사 연결 없음**
3개 지표가 각각 독립 카드로 표시되지만, 실제로는 서로 연결된다.
예: "환율 ▲(1,458) + 수출증가율 ▲(3.2%) → 수출 채산성 개선 가능성" 같은
복합 해석 문장이 없다.

```python
# 추가 권장: core/ecos.py 또는 app.py
def _macro_narrative(macro: dict) -> str:
    """지표 조합 기반 1줄 해석 자동 생성"""
    fx   = float(macro.get("환율(원/$)", {}).get("value", 0) or 0)
    exp  = float(macro.get("수출증가율", {}).get("value", 0) or 0)
    cpi  = float(macro.get("소비자물가(CPI)", {}).get("value", 0) or 0)

    if fx > 1400 and exp > 0:
        return "원화 약세가 수출 채산성을 지지하나 수입 물가 상승 압력도 병존"
    if cpi > 2.5:
        return "물가 압력이 높아 한은의 금리 인하 시기가 지연될 가능성"
    if exp < 0:
        return "수출 부진이 경기 회복 속도를 제약하는 요인으로 작용 중"
    return "거시지표는 완만한 회복 흐름을 시사하나 불확실성은 잔존"
```

**③ 경보 임계값 없음**
환율 1,500 돌파, CPI 3% 초과 같은 이벤트를 별도 경보로 처리하지 않는다.
app.py의 `_validate_macro_item()`은 환율 정상범위(1,200~1,700) 체크만 한다.

```python
# 추가 권장: app.py _validate_macro_item() 확장
_ALERT_THRESHOLDS = {
    "환율(원/$)":     {"warn": 1450, "alert": 1500},
    "소비자물가(CPI)": {"warn": 2.5,  "alert": 3.0},
    "수출증가율":      {"warn": -3.0, "alert": -8.0},  # 하락 기준
}
```

---

## 2. 데이터 정확성 및 신선도 (Data Accuracy & Freshness)

### 문제점

**① macro.json에 갱신 타임스탬프가 없다** ← 가장 중요한 이슈

현재 macro.json 구조:
```json
{
  "환율(원/$)": { "value": "1458", "as_of": "2026-02-28", ... }
}
```

`as_of`는 각 지표의 데이터 기준일이지, **파일이 언제 갱신됐는지**를 나타내지 않는다.
"2026-02-28 기준 환율"을 언제 수집했는지 알 수 없다.

```python
# core/ecos.py refresh_macro() 저장 직전에 추가
updated["_meta"] = {
    "refreshed_at": datetime.now().isoformat(),
    "api_version":  "ECOS StatisticSearch v1",
}
# app.py _load_macro()에서 _meta 분리 처리
```

**② 이메일의 trend 화살표가 항상 공백이다** ← 버그

`emailer.py` `_build_html()`에서:
```python
trend = d.get("trend", "")   # ← macro.json에 "trend" 키가 없음
```

`trend`는 `app.py`의 `_calc_trend()`가 런타임에 계산하지만,
`macro.json`에는 저장되지 않는다. 이메일의 trend 칸은 항상 빈 값이다.

**수정 방법 (2가지 중 택1):**
```python
# 방법 A: ecos.py refresh_macro() 저장 시 trend 미리 계산해서 저장
def _calc_trend(v: str, p: str) -> str:
    try:
        return "▲" if float(v) > float(p) else ("▼" if float(v) < float(p) else "→")
    except Exception:
        return "→"
# entry["trend"] = _calc_trend(result["value"], result["prev_value"])

# 방법 B: emailer.py _build_html()에서 직접 계산
def _calc_trend_email(d: dict) -> str:
    try:
        v, p = float(d.get("value","0")), float(d.get("prev_value","0"))
        return "▲" if v > p else ("▼" if v < p else "→")
    except Exception:
        return ""
```

방법 A(ecos.py에서 저장)가 단일 진실 원천(Single Source of Truth) 원칙에 맞다.

**③ macro.json의 출처 표기가 _SPEC과 불일치**

`_SPEC`에는 모두 `source_name: "한국은행 ECOS"`로 정의되어 있으나,
현재 macro.json 파일에는:
- 수출증가율 → "관세청 무역통계"
- 소비자물가 → "통계청 KOSIS"

파일이 수동으로 편집된 것으로 보인다. ECOS API가 갱신되면 덮어쓰여져 자동 수정되나,
`refresh_macro()` 실패 시 잘못된 출처 표기가 그대로 남는다.

**④ `_fetch_rows()` 재시도 없음**

```python
# 현재: 단일 요청, 실패 시 바로 [] 반환
resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)

# 권장: 최소 2회 재시도
for attempt in range(2):
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        break
    except Exception as e:
        if attempt == 1:
            print(f"[ECOS] 최종 실패: {e}")
            return []
        time.sleep(2)
```

**⑤ macro.json 쓰기가 비원자적**

`content_manager.py`는 `tmp → fsync → os.replace` 패턴으로 원자적 쓰기를 하지만,
`ecos.py`는 `open("w")` 직접 쓰기다. GitHub Actions 중단 시 파일이 손상될 수 있다.

```python
# ecos.py refresh_macro() 마지막 부분
import tempfile, os as _os
tmp_fd, tmp_path = tempfile.mkstemp(dir=MACRO_PATH.parent, suffix=".tmp")
with _os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
    json.dump(updated, f, ensure_ascii=False, indent=2)
_os.replace(tmp_path, MACRO_PATH)
```

---

## 3. 대시보드 UI/UX

### 현재 레이아웃 구조
```
[좌 2/5] 설정 + 문서 목록 + 정책 요약
[우 3/5] 문서 뷰어 (제목·요약·키워드·본문·정책분석·전략질문)
─────────────────────────────────────────
[하단 전폭] 거시지표 카드
[하단 전폭] 다운로드
[하단 전폭] 콘텐츠 이력
```

### 문제점

**① 거시지표가 화면 하단에 위치**
거시지표는 매달 업데이트되는 핵심 컨텍스트인데, 스크롤 없이는 보이지 않는다.
기사 목록보다 먼저 눈에 띄는 위치(상단 or 사이드바)가 적합하다.

**② 카드에 시각적 위험도 색상이 없다**
환율 1,458은 높은 수준이지만 다른 지표와 동일한 흰색 카드로 표시된다.
값 범위에 따라 카드 border/배경에 색을 입히면 직관성이 크게 개선된다.

**③ "마지막 갱신 시각" 표시 없음**
거시지표 섹션에 `refreshed_at` 타임스탬프가 없어서 데이터가 오래됐는지 알 수 없다.

### 권장 UI 레이아웃

```
┌─────────────────────────────────────────────────┐
│  📊 나라경제 브라우저            [🔄 거시지표 업데이트] │
│  ── 거시지표 상단 배너 (3개 카드, 색상 위험도 표시) ──  │
│  환율 1,458원 ▲ [주의]  수출 +3.2% ▲  CPI 2.1% ▲  │
│  갱신: 2026-03-01 09:00 KST                      │
├──────────────┬──────────────────────────────────┤
│  ⚙️ 설정      │  ### 기사 제목                    │
│  URL 입력    │  발행: 2026-01 | 본문: 2,341자     │
│  [불러오기]   │  [🔗 원문 링크]                   │
│              │                                  │
│  ── 필터 ──  │  📝 3줄 요약                       │
│  월 선택     │  ...                              │
│  키워드 검색  │                                  │
│  정렬        │  🏷️ 정책 분석 | 🤔 전략 질문        │
│              │                                  │
│  ── 목록 ──  ├──────────────────────────────────┤
│  📄 기사1    │  💡 거시 인사이트                  │
│  📄 기사2    │  "원화 약세가 수출 채산성을          │
│  📄 기사3    │   지지하나 수입물가 압박도 병존"     │
│              │                                  │
│  📋 정책요약  │  ⬇️ 다운로드                       │
└──────────────┴──────────────────────────────────┘
[하단] 📂 최근 생성된 콘텐츠 이력
```

### 카드 색상 기준 (권장)

```python
def _macro_card_style(label: str, value: str) -> tuple:
    """(border_color, bg_color, badge_text) 반환"""
    try:
        v = float(str(value).replace(",", ""))
    except Exception:
        return ("#e2e8f0", "#fff", "")

    if "환율" in label:
        if v >= 1500: return ("#e53e3e", "#fff5f5", "🔴 주의")
        if v >= 1450: return ("#dd6b20", "#fffaf0", "🟠 경계")
        return ("#38a169", "#f0fff4", "🟢 안정")

    if "CPI" in label or "물가" in label:
        if v >= 3.0: return ("#e53e3e", "#fff5f5", "🔴 고물가")
        if v >= 2.5: return ("#dd6b20", "#fffaf0", "🟠 주의")
        return ("#38a169", "#f0fff4", "🟢 목표")

    if "수출" in label:
        if v <= -5: return ("#e53e3e", "#fff5f5", "🔴 부진")
        if v < 0:   return ("#dd6b20", "#fffaf0", "🟠 감소")
        return ("#38a169", "#f0fff4", "🟢 증가")

    return ("#e2e8f0", "#fff", "")
```

---

## 4. 코드 구조 (Pipeline Stability)

### 파이프라인 현황
```
ECOS API ──► macro.json ──► main.py (Step1~9)
                                │
                    ┌───────────┼───────────┐
                 Step7       Step8       Step9        Step10
            output_script  output_script content_db  email
                .txt           .srt        .json    (선택적)
```

### 강점
- 각 Step이 독립적이고 실패해도 다음 Step이 진행됨 (try/except 일관 적용)
- backward-compatible 원칙 (API 키 없으면 기존 파일 유지)
- content_manager.py의 원자적 쓰기 패턴 우수

### 문제점 및 개선 제안

**① 파이프라인에 공식 상태 리포트가 없다**
각 Step의 성공/실패가 콘솔 print로만 남는다.
GitHub Actions 로그에서 Step별 결과를 한눈에 보기 어렵다.

```python
# main.py 마지막에 추가 권장
def _print_pipeline_summary(steps: dict) -> None:
    print("\n" + "=" * 50)
    print("파이프라인 실행 요약")
    print("=" * 50)
    for step, result in steps.items():
        icon = "✅" if result["ok"] else "❌"
        print(f"  {icon}  {step}: {result.get('msg', '')}")
    print("=" * 50)
```

**② ecos.py의 _fetch_rows() 재시도 없음** (2번 항목과 동일, 반복 강조)

**③ ecos.py macro.json 쓰기 비원자적** (2번 항목과 동일)

**④ summarizer.py와 main.py에 summarize_rule_based()가 중복 존재**

`main.py` 4번 함수(`summarize_rule_based`)와 `core/summarizer.py`의 동일 함수가
별도로 관리되고 있다. 두 버전이 서로 다를 경우 결과가 달라질 수 있다.

```python
# main.py에서 직접 구현 대신 import로 대체 권장
from core.summarizer import summarize_rule_based
```

**⑤ 월별 실행 GitHub Actions에서 ECOS 갱신 타이밍 주의**

현재 `.github/workflows/monthly_run.yml`의 실행 순서를 확인해야 한다.
`main.py`가 ECOS 갱신보다 먼저 실행되면, 직전 달 macro.json로 스크립트가 생성된다.

권장 실행 순서:
```yaml
steps:
  - name: "Step 0: ECOS 거시지표 갱신"
    run: python -m core.ecos
    env:
      ECOS_API_KEY: ${{ secrets.ECOS_API_KEY }}
  - name: "Step 1~: 스크립트 생성"
    run: python main.py
```

---

## 우선순위별 실행 계획

| 우선순위 | 항목 | 파일 | 난이도 |
|:---:|---|---|:---:|
| 🔴 즉시 | 이메일 trend 버그 수정 | emailer.py 또는 ecos.py | 낮음 |
| 🔴 즉시 | macro.json에 refreshed_at 추가 | ecos.py | 낮음 |
| 🟠 단기 | ecos.py 원자적 쓰기 | ecos.py | 낮음 |
| 🟠 단기 | _fetch_rows() 재시도 추가 | ecos.py | 낮음 |
| 🟠 단기 | 거시지표 카드 색상 위험도 | app.py | 중간 |
| 🟡 중기 | 기준금리 지표 추가 | ecos.py _SPEC | 낮음 |
| 🟡 중기 | 지표 간 narrative 문장 | ecos.py or app.py | 중간 |
| 🟡 중기 | main.py summarize 중복 제거 | main.py | 낮음 |
| 🟢 장기 | 거시지표 대시보드 상단 이동 | app.py | 중간 |
| 🟢 장기 | 파이프라인 Summary 출력 | main.py | 낮음 |
