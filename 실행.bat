@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo Starting 60sec Econ Signal Dashboard...
echo.
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8501 "') do taskkill /f /pid %%a > nul 2>&1
timeout /t 1 > nul
python -m streamlit run app.py --server.port 8501
pause
