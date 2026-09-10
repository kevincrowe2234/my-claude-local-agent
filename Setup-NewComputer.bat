@echo off
setlocal enabledelayedexpansion
title My Claude Local Agent - First-Time Setup
color 0A

echo =========================================================================
echo  My Claude Local Agent - Windows 11 Setup
echo =========================================================================
echo.
echo This will:
echo   1. Install Python 3.12 (only if a Python install isn't already found)
echo   2. Install required Python packages (customtkinter, anthropic, etc.)
echo   3. Create a taskbar shortcut for the app
echo.
echo Requirements: an internet connection and about 200 MB of free disk space.
echo.
pause
echo.

cd /d "%~dp0"

REM -------------------------------------------------------------------
REM Step 1: Check for / install Python
REM -------------------------------------------------------------------
echo [1/4] Checking for Python...
where python >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
    echo   Found Python !PYVER! already installed. Skipping install.
    goto :install_packages
)

echo   Python not found on PATH. Downloading the Python 3.12.7 installer...
set "PY_INSTALLER=%TEMP%\python-3.12.7-amd64.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile '%PY_INSTALLER%' -UseBasicParsing; exit 0 } catch { Write-Host $_.Exception.Message; exit 1 }"
if not %errorlevel% equ 0 (
    echo.
    echo ERROR: Failed to download the Python installer. Check your internet
    echo connection, or manually install Python 3.9+ from
    echo https://www.python.org/downloads/ ^(check "Add python.exe to PATH"^)
    echo and then re-run this script.
    pause
    exit /b 1
)

echo   Installing Python silently - this can take a minute or two...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1 Include_tcltk=1 Include_test=0
set "INSTALL_RC=%errorlevel%"
del "%PY_INSTALLER%" >nul 2>&1
if not %INSTALL_RC% equ 0 (
    echo.
    echo ERROR: Python installation failed ^(exit code %INSTALL_RC%^).
    pause
    exit /b 1
)

echo   Refreshing PATH for this window so 'python' resolves immediately...
for /f "skip=2 tokens=3*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USERPATH=%%a %%b"
for /f "skip=2 tokens=3*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYSPATH=%%a %%b"
set "PATH=%SYSPATH%;%USERPATH%"

where python >nul 2>&1
if not %errorlevel% equ 0 (
    echo.
    echo Python was installed, but this window still can't see it on PATH.
    echo Please CLOSE this window, open a NEW Command Prompt, and run
    echo Setup-NewComputer.bat again - it will detect Python and continue
    echo from the package-install step on the second run.
    pause
    exit /b 1
)
echo   Python installed successfully.

:install_packages
echo.
echo [2/4] Upgrading pip...
python -m pip install --upgrade pip
if not %errorlevel% equ 0 (
    echo   Warning: pip self-upgrade failed - continuing anyway.
)

echo.
echo [3/4] Installing required packages from requirements.txt...
if not exist "requirements.txt" (
    echo ERROR: requirements.txt not found in this folder.
    echo Make sure you copied ALL the files listed in README.md into this
    echo same folder before running this script.
    pause
    exit /b 1
)
python -m pip install -r requirements.txt
if not %errorlevel% equ 0 (
    echo.
    echo ERROR: Failed to install one or more required packages.
    echo Scroll up for details, or try running this script again.
    pause
    exit /b 1
)
echo   Packages installed successfully.

echo.
echo [4/4] Creating taskbar shortcut...
if exist "Create-TaskbarShortcut.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "Create-TaskbarShortcut.ps1"
) else (
    echo   Create-TaskbarShortcut.ps1 not found - skipping shortcut creation.
    echo   You can still launch the app via run_my_claude_agent.bat.
)

echo.
echo =========================================================================
echo  Setup complete!
echo =========================================================================
echo.
echo Next steps:
echo   1. If a shortcut was created above, right-click
echo      "My Claude Local Agent.lnk" in this folder and choose
echo      "Pin to taskbar".
echo   2. Launch the app: double-click run_my_claude_agent.bat
echo      (or the pinned taskbar icon).
echo   3. In the Setup tab, enter your Anthropic API key and click
echo      "Connect / Test Key".
echo   4. In the Workspaces tab, click "+ Add Workspace" to give Claude a
echo      folder to work in.
echo.
pause
