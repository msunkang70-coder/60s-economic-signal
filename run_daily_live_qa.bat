@echo off
:: ============================================================
:: run_daily_live_qa.bat
:: Daily QA Pipeline - 60sec Econ Signal
::
:: 용도: daily_live_qa.py --quiet 를 실행하고
::       stdout/stderr 를 logs\daily_live_qa.log 에 날짜별 추가 저장.
::       성공(exit 0) / 실패(exit 1) 종료코드 반환.
::
:: 수동 실행: run_daily_live_qa.bat
:: Task Scheduler: 이 파일을 절대경로로 지정
:: ============================================================

chcp 65001 > nul
cd /d "%~dp0"

:: ── 경로 설정 ──────────────────────────────────────────────
set "PROJ_DIR=%~dp0"
set "LOG_DIR=%PROJ_DIR%logs"
set "LOG_FILE=%LOG_DIR%\daily_live_qa.log"
set "PYTHON=python"

:: logs 디렉토리 없으면 생성
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

:: ── 실행 시각 기록 ─────────────────────────────────────────
set "RUN_TS=%DATE% %TIME%"
echo. >> "%LOG_FILE%"
echo ============================================================ >> "%LOG_FILE%"
echo [RUN] %RUN_TS% >> "%LOG_FILE%"
echo ============================================================ >> "%LOG_FILE%"

:: ── QA 실행 ───────────────────────────────────────────────
"%PYTHON%" "%PROJ_DIR%daily_live_qa.py" --quiet >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

:: ── 종료코드 기록 ──────────────────────────────────────────
if %EXIT_CODE% == 0 (
    echo [OK] exit_code=0  Health=Green/Yellow >> "%LOG_FILE%"
) else (
    echo [FAIL] exit_code=%EXIT_CODE%  QA critical detected or script error >> "%LOG_FILE%"
)

echo ============================================================ >> "%LOG_FILE%"

:: ── 로그 크기 제한: 5 MB 초과 시 rotation ─────────────────
for %%F in ("%LOG_FILE%") do set "LOG_SIZE=%%~zF"
if %LOG_SIZE% GTR 5242880 (
    move /y "%LOG_FILE%" "%LOG_DIR%\daily_live_qa.log.bak" > nul
    echo [LOG ROTATED at %RUN_TS%] >> "%LOG_FILE%"
)

exit /b %EXIT_CODE%
