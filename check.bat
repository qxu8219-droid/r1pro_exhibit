@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 goto use_py
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 goto use_python
python3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 goto use_python3
echo Python 3.9+ is required. Install Python, then try again.
pause
exit /b 1
:use_py
py -3 "%~dp0check_package.py"
goto finished
:use_python
python "%~dp0check_package.py"
goto finished
:use_python3
python3 "%~dp0check_package.py"
:finished
set "CHECK_RESULT=%errorlevel%"
pause
exit /b %CHECK_RESULT%
