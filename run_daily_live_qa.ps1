#Requires -Version 5.1
<#
.SYNOPSIS
    Daily QA Pipeline - 60sec Econ Signal

.DESCRIPTION
    daily_live_qa.py --quiet 를 실행하고 logs\daily_live_qa.log 에 기록한다.
    Task Scheduler 에서 호출하거나 수동으로 실행 가능.

    성공: exit 0
    QA CRITICAL 감지 또는 스크립트 오류: exit 1

.EXAMPLE
    .\run_daily_live_qa.ps1
    PowerShell -ExecutionPolicy Bypass -File "C:\...\run_daily_live_qa.ps1"
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── 경로 설정 ─────────────────────────────────────────────────
$ProjectDir = $PSScriptRoot
$LogDir     = Join-Path $ProjectDir "logs"
$LogFile    = Join-Path $LogDir     "daily_live_qa.log"
$Script     = Join-Path $ProjectDir "daily_live_qa.py"

# logs 디렉토리 없으면 생성
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

# ── 로그 헬퍼 ────────────────────────────────────────────────
function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts  $Message" | Add-Content -Path $LogFile -Encoding UTF8
}

# ── 로그 rotation (5 MB 초과 시) ─────────────────────────────
if (Test-Path $LogFile) {
    $size = (Get-Item $LogFile).Length
    if ($size -gt 5MB) {
        $bak = $LogFile + ".bak"
        Move-Item -Path $LogFile -Destination $bak -Force
        Write-Log "[LOG ROTATED] Previous log moved to $bak"
    }
}

# ── 실행 시각 헤더 ────────────────────────────────────────────
Write-Log ("=" * 60)
Write-Log "[RUN START] daily_live_qa.py --quiet"
Write-Log ("=" * 60)

# ── QA 실행 ──────────────────────────────────────────────────
$exitCode = 0
try {
    # stdout+stderr 모두 로그로 리디렉션 (UTF-8)
    $output = & python $Script --quiet 2>&1
    $exitCode = $LASTEXITCODE

    # 출력 내용을 로그에 추가
    if ($output) {
        $output | ForEach-Object { Write-Log "  $_" }
    }
}
catch {
    Write-Log "[ERROR] Script threw exception: $_"
    $exitCode = 1
}

# ── 종료코드 기록 ─────────────────────────────────────────────
if ($exitCode -eq 0) {
    Write-Log "[OK] exit_code=0  QA pipeline completed successfully"
} else {
    Write-Log "[FAIL] exit_code=$exitCode  QA CRITICAL or script error detected"
}
Write-Log ("=" * 60)

exit $exitCode
