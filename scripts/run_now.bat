@echo off
REM Convenience: run today's audit pass without waiting for the schedule.
cd /d "%~dp0\.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scripts\daily_run.py %*
) else (
    python scripts\daily_run.py %*
)
