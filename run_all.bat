@echo off
REM Runs every enabled backup job with no prompts.
REM Double-click this file, or schedule it in Windows Task Scheduler.
cd /d "%~dp0"
python main.py --run-all --auto
set EXITCODE=%ERRORLEVEL%
echo.
if %EXITCODE%==0 (
    echo Backup finished successfully.
) else if %EXITCODE%==1 (
    echo Backup finished, but one or more files FAILED. Check logs\backup.log
) else (
    echo Backup could not run. Check logs\backup.log
)
pause
