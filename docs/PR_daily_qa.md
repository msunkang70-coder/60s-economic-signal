# PR: Daily QA 자동화 파이프라인 + 운영 배포

## PR 제목

```
feat: Daily QA 파이프라인 + 운영 자동화 — 6-check 헬스모니터 + Task Scheduler 등록
```

---

## 변경 파일 목록 및 커밋 대상/제외 분류

### 커밋 대상 (10개 파일)

| # | 파일 | 구분 | 변경 요약 |
|---|------|------|-----------|
| 1 | `.gitignore` | M | `logs/*.log*` 제외 규칙 추가 |
| 2 | `core/utils.py` | M | `safe_execute`, `safe_float`, `safe_json_load` 추가 |
| 3 | `core/checklist_rules.py` | NEW | `MACRO_CHECKLIST_MAP`, `ACTION_CHECKLIST_TEMPLATES` 상수 |
| 4 | `core/extra_sources.py` | M | 한국경제 RSS URL 수정 (feeds→www) |
| 5 | `daily_live_qa.py` | NEW | QA 파이프라인 본체 (774줄, 6개 체크) |
| 6 | `app.py` | M | 사이드바 Debug Mode / QA Panel 추가 |
| 7 | `run_daily_live_qa.bat` | NEW | Windows 배치 래퍼 |
| 8 | `run_daily_live_qa.ps1` | NEW | PowerShell 래퍼 (Task Scheduler 권장) |
| 9 | `docs/daily_qa_ops_guide.md` | NEW | 운영 가이드 (등록 절차, 장애 대응표) |
| 10 | `logs/.gitkeep` | NEW | `logs/` 디렉토리 추적용 placeholder |

### 커밋 제외 (런타임 생성 파일)

| 파일 | 제외 이유 |
|------|-----------|
| `logs/daily_live_qa.log` | `.gitignore` 신규 규칙으로 제외 |
| `data/daily_qa_report.json` | 기존 `*.json` 규칙으로 제외 |
| `data/daily_qa_report_prev.json` | 기존 `*.json` 규칙으로 제외 |

---

## 권장 커밋 구성 (3개 커밋)

### Commit 1 — core 의존성 복구 및 버그픽스

```
fix: core 모듈 복구 — utils 함수 추가, checklist_rules 신규, hankyung URL 수정

- core/utils.py: safe_execute/safe_float/safe_json_load 추가
  (today_signal.py 의존성 충족, 구 버전에 누락된 함수)
- core/checklist_rules.py: MACRO_CHECKLIST_MAP / ACTION_CHECKLIST_TEMPLATES 신규
  (today_signal.py import 경로 복구 — git 미추적 상태였음)
- core/extra_sources.py: 한국경제 RSS URL 교체
  feeds.hankyung.com → www.hankyung.com/feed/economy
  (2026-03-17 DNS 소멸 확인, 대체 URL 50건 안정 검증)
```

대상: `core/utils.py`, `core/checklist_rules.py`, `core/extra_sources.py`

---

### Commit 2 — Daily QA 파이프라인 신규 추가

```
feat: Daily QA 파이프라인 추가 — daily_live_qa.py 6개 헬스체크

6개 체크 항목:
  1. source_ingestion_count  — RSS 소스별 수집 건수
  2. junk_filtering_ratio    — 정크 필터 비율 (warn 15%, critical 25%)
  3. zero_relevance_ratio    — 경제 키워드 미매칭 비율 (warn 75%)
  4. ranking_stability       — 산업별 top1 지표 안정성 (3회 이상 변동 warn)
  5. cache_ttl_status        — summary_cache 파일 갱신 여부 (warn 6h, critical 24h)
  6. source_availability     — RSS 소스 HTTP 응답 (PRIMARY/SECONDARY: critical)

Threshold v2 — 2026-03-17 실측 데이터 기반 false positive 제거
결과: data/daily_qa_report.json (*.json ignore로 미추적)
Public API: run_daily_qa(), load_latest_qa_report(), get_system_health()
```

대상: `daily_live_qa.py`

---

### Commit 3 — 운영화 (UI + 래퍼 + 가이드 + gitignore)

```
chore: Daily QA 운영화 — Debug Panel, 실행 래퍼, 운영 가이드

app.py:
  - 사이드바 Debug Mode 토글 추가
  - Green/Yellow/Red 헬스 배지 + CRITICAL/WARNING 지표 + 체크별 expander

운영 래퍼:
  - run_daily_live_qa.bat — Windows 더블클릭용, 5MB 로그 rotation
  - run_daily_live_qa.ps1 — Task Scheduler 권장, UTF-8 타임스탬프 로그

Task Scheduler:
  - 작업명: 60sec_EconSignal_DailyQA
  - 매일 09:00, StartWhenAvailable, RunOnlyIfNetworkAvailable
  - 검증: LastTaskResult=0, logs/daily_live_qa.log 정상 기록 확인

문서:
  - docs/daily_qa_ops_guide.md — 등록 절차, 로그 확인법, 장애 대응표

.gitignore:
  - logs/daily_live_qa.log* 제외 규칙 추가
  - logs/.gitkeep 추적
```

대상: `app.py`, `run_daily_live_qa.bat`, `run_daily_live_qa.ps1`,
      `docs/daily_qa_ops_guide.md`, `logs/.gitkeep`, `.gitignore`

---

## PR 설명 초안

### Summary

매일 오전 09:00 자동 실행되는 **Daily QA 파이프라인**을 추가하고, Streamlit 사이드바에 시스템 헬스 패널을 통합한다.

### 변경 배경

| 문제 | 영향 |
|------|------|
| `feeds.hankyung.com` DNS 소멸 | 한국경제 RSS 0건 수집, source_ingestion WARNING |
| `core/checklist_rules.py` git 미추적 | `ranking_stability` 항상 fallback("unknown") |
| `core/utils.py` 구버전 (`safe_execute` 누락) | `today_signal.py` import 실패 |

수동 확인 없이는 위 장애가 며칠간 방치될 수 있었음. 자동 헬스체크 필요성 확인.

### 변경 내용

**1. core 의존성 복구 (fix)**

- `core/utils.py`: `safe_execute` 데코레이터 외 2개 함수 추가 — `today_signal.py` import 복구
- `core/checklist_rules.py`: 체크리스트 상수 파일 신규 추가 (git 미추적 → 추적)
- `core/extra_sources.py`: 한국경제 RSS URL 교체 (`feeds` → `www.hankyung.com/feed/economy`)

**2. Daily QA 파이프라인 (`daily_live_qa.py`)**

6개 체크로 시스템 상태를 매일 진단:

```
source_ingestion_count  — 수집 건수 정상 여부
junk_filtering_ratio    — 정크 비율 (v2 threshold: warn 15%, critical 25%)
zero_relevance_ratio    — 경제 키워드 매칭률 (v2 threshold: warn 75%)
ranking_stability       — 산업별 top1 지표 안정성
cache_ttl_status        — summary_cache 파일 갱신 여부
source_availability     — 각 RSS 소스 HTTP 응답
```

- 전체 결과: `Green / Yellow / Red`
- JSON 보고서: `data/daily_qa_report.json` (런타임, git ignore)

**3. Streamlit Debug Panel (`app.py`)**

사이드바 하단 `🔧 Debug Mode` 토글 ON 시:
- Green/Yellow/Red 헬스 배지 + 최근 실행 시각
- CRITICAL / WARNING 카운트 지표
- 체크별 상세 expander

**4. 운영 래퍼 + Task Scheduler**

- `run_daily_live_qa.ps1` / `.bat` — 로그 리디렉션, exit code 반환, 5MB rotation
- Task Scheduler `60sec_EconSignal_DailyQA` 등록 (매일 09:00, StartWhenAvailable)
- `docs/daily_qa_ops_guide.md` — 등록 절차 + 장애 대응표

### 검증 결과

| 검증 항목 | 결과 |
|-----------|------|
| `python daily_live_qa.py` 직접 실행 | 🟢 Green, CRITICAL 0, WARNING 0 |
| `run_daily_live_qa.ps1` 수동 실행 | exit 0, 로그 정상 기록 |
| Task Scheduler `Start-ScheduledTask` | LastTaskResult=0, 2초 내 완료 |
| 로그 파일 append | 7라인 신규 기록 (타임스탬프 포함) |
| Hankyung URL 안정성 | 50건/회, 59ms, 3회 연속 확인 |
| ranking_stability | 8개 산업 모두 "수입물가지수" (rules-based) |

### 기대 효과

- RSS 소스 장애 당일 자동 감지 (수동 확인 → 09:00 자동 알림)
- ranking 지표 fallback 발생 시 즉시 경고
- 캐시 미갱신(24h 이상) CRITICAL로 조기 감지
- Streamlit 사이드바에서 현재 시스템 상태 즉시 확인 가능

---

## Merge 전 체크리스트

### 코드 리뷰

- [ ] `core/checklist_rules.py` — MACRO_CHECKLIST_MAP 모든 산업 키 확인 (반도체/자동차/화학/소비재/배터리/조선/철강/일반)
- [ ] `daily_live_qa.py` — `_THRESHOLDS` 값이 v2 기준인지 확인
- [ ] `daily_live_qa.py` — `_RSS_SOURCES` 내 한국경제 URL이 `www.hankyung.com/feed/economy` 인지 확인
- [ ] `app.py` — Debug Panel 토글이 사이드바에 정상 렌더링되는지 확인

### 기능 검증

- [ ] `python daily_live_qa.py` 실행 → exit 0, 🟢 Green 확인
- [ ] `powershell -File run_daily_live_qa.ps1` 실행 → exit 0, logs/daily_live_qa.log append 확인
- [ ] Task Scheduler `60sec_EconSignal_DailyQA` 등록 상태 확인 → `Get-ScheduledTask -TaskName "60sec_EconSignal_DailyQA"`
- [ ] Streamlit 앱 실행 후 사이드바 🔧 Debug Mode 토글 → 헬스 배지 정상 표시 확인

### 파일 제외 확인

- [ ] `data/daily_qa_report.json` git add 안 됨 (`*.json` ignore)
- [ ] `logs/daily_live_qa.log` git add 안 됨 (신규 ignore 규칙)
- [ ] `data/daily_qa_report_prev.json` git add 안 됨

### 운영 확인

- [ ] Task Scheduler `60sec_EconSignal_DailyQA` 가 로컬에 등록되어 있는지 확인
- [ ] 다음날 09:00 이후 `LastTaskResult = 0` 재확인
- [ ] `docs/daily_qa_ops_guide.md` 내 절대 경로가 실제 환경과 일치하는지 확인

---

## 실행 커맨드 요약 (merge 후)

```powershell
# 수동 실행
python daily_live_qa.py

# 로그 포함 수동 실행
powershell -ExecutionPolicy Bypass -File run_daily_live_qa.ps1

# Task Scheduler 즉시 테스트
Start-ScheduledTask -TaskName "60sec_EconSignal_DailyQA"
(Get-ScheduledTaskInfo -TaskName "60sec_EconSignal_DailyQA").LastTaskResult

# 로그 확인
Get-Content logs\daily_live_qa.log -Tail 20
```
