@echo off
setlocal EnableDelayedExpansion

echo.
echo === Rite Water Voucher Audit — Push to Google Sheets ===
echo.

REM ── Find Python ────────────────────────────────────────────
set PYTHON=
for %%P in (python python3) do (
    if "!PYTHON!"=="" (
        where %%P >nul 2>&1
        if not errorlevel 1 set PYTHON=%%P
    )
)
if "!PYTHON!"=="" (
    echo ERROR: Python is not installed or not on PATH.
    echo Install Python from https://python.org and try again.
    pause & exit /b 1
)
echo Using Python: !PYTHON!
echo.

REM ── Install required packages if missing ───────────────────
echo Checking required packages...
!PYTHON! -c "import gspread" >nul 2>&1
if errorlevel 1 (
    echo Installing gspread...
    !PYTHON! -m pip install gspread --quiet
)
!PYTHON! -c "from google.oauth2.service_account import Credentials" >nul 2>&1
if errorlevel 1 (
    echo Installing google-auth...
    !PYTHON! -m pip install google-auth --quiet
)
echo Packages ready.
echo.

REM ── Paths ──────────────────────────────────────────────────
set SCRIPT_DIR=%~dp0
set CREDS=%SCRIPT_DIR%google_creds.json
set OUT_DIR=%SCRIPT_DIR%output
set SHEET_ID=1aBIdbOrtYRaIOhskeOk3Rb59MLce8ixjnHr9UDkZl_k

REM ── Check credentials ──────────────────────────────────────
if not exist "%CREDS%" (
    echo ERROR: google_creds.json not found at:
    echo   %CREDS%
    echo.
    echo Please save your service account key file there.
    pause & exit /b 1
)

REM ── Run ────────────────────────────────────────────────────
echo Pushing audit results to Google Sheets...
echo.
!PYTHON! "%SCRIPT_DIR%scripts\log_all_to_sheets.py" ^
  --sheet-id "%SHEET_ID%" ^
  --creds    "%CREDS%" ^
  --out-dir  "%OUT_DIR%"

echo.
if errorlevel 1 (
    echo Something went wrong — see error above.
) else (
    echo Done!
)
pause
