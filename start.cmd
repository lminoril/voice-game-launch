@echo off
rem Installs dependencies if needed and starts the app without a console window.
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 goto :nopython

python -c "import vosk, sounddevice, rapidfuzz" >nul 2>&1
if not errorlevel 1 goto :run

echo First run: installing dependencies...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pipfail

:run
where pythonw >nul 2>&1
if errorlevel 1 goto :console
start "" pythonw "%~dp0app.py"
exit /b 0

:console
start "" python "%~dp0app.py"
exit /b 0

:nopython
echo.
echo Python not found. Install it from https://python.org and run this file again.
echo.
pause
exit /b 1

:pipfail
echo.
echo Could not install dependencies. Check your internet connection.
echo.
pause
exit /b 1
