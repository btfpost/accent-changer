@echo off
if exist ".deps_installed" goto :run

echo First-time setup: installing dependencies (this happens only once)...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Installation failed. Check the messages above.
    pause
    exit /b 1
)
echo done > .deps_installed

:run
echo Starting Accent Changer website at http://127.0.0.1:8000
start http://127.0.0.1:8000
python server.py
pause
