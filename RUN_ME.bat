@echo off
echo.
echo ============================================================
echo   Nifty Options Live Algo
echo   Start this before 9:15 AM on any trading day
echo ============================================================
echo.
echo [1/2] Installing packages...
pip install -r requirements.txt
echo.
echo [2/2] Starting algo...
echo.
python main.py
pause
