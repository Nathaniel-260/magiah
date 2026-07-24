@echo off
rem =====================================================================
rem  magiah launcher (ASCII alias) - identical to "הפעלת מגיה.bat".
rem  Starts the Hebrew review web UI and opens it in the browser.
rem =====================================================================

rem --- (1) console + Python UTF-8 so Hebrew text and Hebrew paths work ---
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem --- (2) always run from the folder this .bat lives in ---------------
cd /d "%~dp0"

rem --- (4) data / output folder next to this .bat ----------------------
rem  EDIT THIS LINE to point the review UI at another folder
rem  (for example one that already contains report.db / ui_review.db):
set "MAGIAH_OUT=%~dp0magiah_data"
if not exist "%MAGIAH_OUT%" mkdir "%MAGIAH_OUT%"

echo.
echo ==========================================
echo    מגיה - מנוע הגהה
echo ==========================================
echo.

rem --- (3) find a working Python: py -3, then python, then python3 -----
set "PYEXE="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    python3 --version >nul 2>&1
    if not errorlevel 1 set "PYEXE=python3"
)

if not defined PYEXE (
    echo לא נמצאה התקנת Python במחשב.
    echo יש להתקין Python בגרסה 3.9 ומעלה מהכתובת:
    echo    https://www.python.org/downloads/
    echo בזמן ההתקנה סמנו את האפשרות "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo פותח את הממשק בדפדפן...
echo תיקיית הנתונים: %MAGIAH_OUT%
echo לסגירה: סגרו חלון זה.
echo.

rem --- (5)+(6) launch. Prefer launcher.py (free-port + Hebrew errors);
rem  fall back to the module entry point. --out sets the data folder. ---
if exist "%~dp0launcher.py" (
    %PYEXE% -X utf8 "%~dp0launcher.py" --out "%MAGIAH_OUT%"
) else (
    %PYEXE% -X utf8 -m magiah ui --out "%MAGIAH_OUT%"
)

if errorlevel 1 (
    echo.
    echo אירעה שגיאה בהפעלת מגיה. פרטי השגיאה מופיעים למעלה.
    echo אם הבעיה חוזרת, ודאו ש-Python מותקן ושהתיקייה שלמה.
)

echo.
pause
