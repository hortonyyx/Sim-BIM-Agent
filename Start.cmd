@echo off
setlocal
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
set PYTHONPATH=
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.ps1 first to install the Python environment.
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -B "%~dp0sim_bim.py" %*
set "SIM_BIM_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %SIM_BIM_EXIT%
