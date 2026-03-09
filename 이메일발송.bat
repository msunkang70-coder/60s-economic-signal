@echo off
chcp 65001 > nul
cd /d "%~dp0"

:: ── 이메일 설정 (secrets.toml 대신 환경변수로 주입) ──
set EMAIL_SENDER=msunkang70@gmail.com
set EMAIL_PASSWORD=Rbkgkhztkyqeswjf
set EMAIL_RECIPIENTS=msunkang@naver.com
set EMAIL_SMTP_HOST=smtp.gmail.com
set EMAIL_SMTP_PORT=587
echo.
echo ========================================
echo   60s 경제신호 — 이메일 수동 발송
echo   (개선 버전: TASK-01~07 적용)
echo ========================================
echo.
echo  [1] 월간 스크립트 이메일 발송
echo      (60초 쇼츠 스크립트 + 거시지표)
echo.
echo  [2] 임계값 알림 이메일 발송
echo      (환율/CPI/수출 이상 감지 시)
echo.
echo  [Q] 종료
echo.
set /p choice="선택 (1/2/Q): "

if /i "%choice%"=="1" goto script_mail
if /i "%choice%"=="2" goto alert_mail
if /i "%choice%"=="q" goto end
goto end

:script_mail
echo.
echo [발송 중] 월간 스크립트 이메일...
python -m core.emailer
echo.
pause
goto end

:alert_mail
echo.
echo [발송 중] 임계값 알림 이메일...
python -m core.emailer --alert
echo.
pause
goto end

:end
