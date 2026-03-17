# Daily QA 자동 실행 운영 가이드

> 60sec Econ Signal — MSion AI Macro Intelligence Dashboard
> 최종 업데이트: 2026-03-17

---

## 1. 개요

`daily_live_qa.py --quiet` 를 매일 오전 09:00 자동 실행하여
시스템 건강 상태(Green / Yellow / Red)를 `logs/daily_live_qa.log` 에 기록한다.

```
프로젝트 루트
├── daily_live_qa.py          # QA 파이프라인 본체
├── run_daily_live_qa.bat     # Windows 배치 래퍼 (더블클릭 / Task Scheduler)
├── run_daily_live_qa.ps1     # PowerShell 래퍼 (권장 — UTF-8 로그)
├── logs/
│   ├── daily_live_qa.log     # 자동 생성, 5 MB 초과 시 rotation
│   └── daily_live_qa.log.bak # rotation 백업
└── data/
    ├── daily_qa_report.json      # 최신 QA 결과
    └── daily_qa_report_prev.json # 직전 실행 결과 (ranking 비교용)
```

---

## 2. 실행 방법

### 2-1. 수동 실행 (PowerShell, 권장)

```powershell
cd "C:\Users\MS\OneDrive\AI Study\60sec_econ_signal\.claude\worktrees\clever-snyder"
powershell -ExecutionPolicy Bypass -File "run_daily_live_qa.ps1"
```

### 2-2. 수동 실행 (배치 파일, 더블클릭)

탐색기에서 `run_daily_live_qa.bat` 더블클릭

### 2-3. 직접 실행 (디버그용)

```powershell
python daily_live_qa.py          # 전체 출력 (verbose)
python daily_live_qa.py --quiet  # 결과 한 줄만 출력
```

---

## 3. Windows Task Scheduler 등록

### 3-1. PowerShell로 자동 등록 (권장)

관리자 권한 PowerShell에서 아래 명령 한 번만 실행:

```powershell
$ProjectDir = "C:\Users\MS\OneDrive\AI Study\60sec_econ_signal\.claude\worktrees\clever-snyder"
$ScriptPath = "$ProjectDir\run_daily_live_qa.ps1"

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File `"$ScriptPath`"" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger -Daily -At "09:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -DontStopOnIdleEnd

Register-ScheduledTask `
    -TaskName   "60sec_EconSignal_DailyQA" `
    -Action     $Action `
    -Trigger    $Trigger `
    -Settings   $Settings `
    -RunLevel   Limited `
    -Description "60sec Econ Signal — Daily QA Pipeline (09:00)" `
    -Force
```

> `StartWhenAvailable`: 09:00에 PC가 꺼져 있었으면 다음 부팅 시 즉시 실행
> `RunOnlyIfNetworkAvailable`: 네트워크 없으면 건너뜀
> `ExecutionTimeLimit 10분`: 스크립트가 멈춰도 강제 종료

### 3-2. GUI로 등록 (수동)

1. `Win + R` → `taskschd.msc` → 작업 스케줄러 열기
2. [작업 만들기] 클릭
3. **일반** 탭
   - 이름: `60sec_EconSignal_DailyQA`
   - 설명: `60sec Econ Signal Daily QA Pipeline`
   - 보안 옵션: `사용자가 로그온할 때만 실행`
4. **트리거** 탭 → [새로 만들기]
   - 매일 / 오전 09:00
5. **동작** 탭 → [새로 만들기]
   - 프로그램/스크립트: `powershell.exe`
   - 인수 추가:
     ```
     -ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File "C:\Users\MS\OneDrive\AI Study\60sec_econ_signal\.claude\worktrees\clever-snyder\run_daily_live_qa.ps1"
     ```
   - 시작 위치:
     ```
     C:\Users\MS\OneDrive\AI Study\60sec_econ_signal\.claude\worktrees\clever-snyder
     ```
6. **조건** 탭
   - "네트워크 연결이 가능한 경우에만 작업 시작" 체크
7. **설정** 탭
   - "예약된 시작 시간을 놓친 경우 가능하면 빨리 작업 시작" 체크
   - 실행 제한 시간: 10분

### 3-3. 등록 확인 및 관리

```powershell
# 등록 확인
Get-ScheduledTask -TaskName "60sec_EconSignal_DailyQA" | Select TaskName, State, LastRunTime, NextRunTime

# 지금 즉시 테스트 실행
Start-ScheduledTask -TaskName "60sec_EconSignal_DailyQA"

# 마지막 실행 결과 확인 (0 = 성공, 1 = 실패)
(Get-ScheduledTaskInfo -TaskName "60sec_EconSignal_DailyQA").LastTaskResult

# 작업 비활성화
Disable-ScheduledTask -TaskName "60sec_EconSignal_DailyQA"

# 작업 삭제
Unregister-ScheduledTask -TaskName "60sec_EconSignal_DailyQA" -Confirm:$false
```

---

## 4. 로그 확인

```powershell
# 최신 로그 확인
Get-Content logs\daily_live_qa.log -Tail 30

# 실패 항목만 필터
Select-String -Path logs\daily_live_qa.log -Pattern "\[FAIL\]"

# 오늘 실행 내역
Select-String -Path logs\daily_live_qa.log -Pattern (Get-Date -Format "yyyy-MM-dd")
```

### 로그 샘플

```
2026-03-17 09:00:03  ============================================================
2026-03-17 09:00:03  [RUN START] daily_live_qa.py --quiet
2026-03-17 09:00:03  ============================================================
2026-03-17 09:00:05    [결과] Green | CRITICAL 0 | WARNING 0
2026-03-17 09:00:05  [OK] exit_code=0  QA pipeline completed successfully
2026-03-17 09:00:05  ============================================================
```

---

## 5. 종료코드 정의

| exit code | 의미 |
|-----------|------|
| 0 | 정상 — Green 또는 Yellow (WARNING은 있어도 CRITICAL 없음) |
| 1 | 비정상 — CRITICAL 감지 또는 스크립트 예외 |

---

## 6. QA 결과 확인

### JSON 보고서 직접 확인

```powershell
Get-Content data\daily_qa_report.json | ConvertFrom-Json | Select overall_health, run_at
```

### Streamlit Debug Panel

`app.py` 실행 후 사이드바 하단 **"🔧 Debug Mode"** 토글 ON →
`▶ QA 실행` 버튼 클릭 또는 기존 `daily_qa_report.json` 자동 로드

---

## 7. 운영 체크리스트

### 초기 설정

- [ ] `run_daily_live_qa.ps1` 이 프로젝트 루트에 존재하는지 확인
- [ ] `logs/` 디렉토리 존재 확인 (없으면 자동 생성됨)
- [ ] PowerShell 수동 실행 1회 → `logs/daily_live_qa.log` 생성 확인
- [ ] exit code 0 확인
- [ ] Task Scheduler 등록 완료
- [ ] `Start-ScheduledTask` 로 즉시 실행 테스트
- [ ] `LastTaskResult = 0` 확인

### 주간 점검 (매주 월요일)

- [ ] `logs/daily_live_qa.log` 에서 `[FAIL]` 항목 없는지 확인
- [ ] `data/daily_qa_report.json` 의 `overall_health` 확인
- [ ] 로그 파일 크기 확인 (5 MB 초과 시 자동 rotation)

### 장애 대응

| 증상 | 원인 | 조치 |
|------|------|------|
| `[FAIL] exit_code=1` in log | QA CRITICAL 감지 | `daily_qa_report.json` 열어 critical 항목 확인 |
| 로그 파일 없음 | 스크립트 미실행 | Task Scheduler 상태 확인, 즉시 실행 테스트 |
| source_availability CRITICAL | RSS 소스 다운 | `core/extra_sources.py` URL 점검, 대체 URL 교체 |
| ranking_stability WARNING | 지표 급변 | 정상 시장 변동 여부 확인 — 수동 OK 처리 가능 |
| cache_ttl CRITICAL (24h+) | 캐시 미갱신 | `data/summary_cache.json` 삭제 후 앱 재시작 |

---

## 8. 파일 목록 요약

| 파일 | 역할 |
|------|------|
| `daily_live_qa.py` | QA 파이프라인 본체 (6개 체크) |
| `run_daily_live_qa.bat` | Windows 배치 래퍼 (더블클릭용) |
| `run_daily_live_qa.ps1` | PowerShell 래퍼 (Task Scheduler 권장) |
| `logs/daily_live_qa.log` | 실행 이력 로그 (UTF-8, 5 MB rotation) |
| `data/daily_qa_report.json` | 최신 QA 결과 JSON |
| `data/daily_qa_report_prev.json` | 직전 결과 (ranking 비교용) |
