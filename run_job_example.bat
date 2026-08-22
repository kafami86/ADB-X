@echo off
REM Example: back up just the "Camera" job with no prompts.
REM Copy this file and change the job name to make one .bat per job if you like.
cd /d "%~dp0"
python main.py --job Camera --auto
set EXITCODE=%ERRORLEVEL%
echo.
if %EXITCODE%==0 (
    echo Camera backup finished successfully.
) else if %EXITCODE%==1 (
    echo Camera backup finished, but one or more files FAILED. Check logs\backup.log
) else (
    echo Camera backup could not run. Check logs\backup.log
)
pause
