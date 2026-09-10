@echo off
REM Launches My Claude Local Agent without a console window staying open.
REM Requires Python to be installed with "Add python.exe to PATH" checked
REM during installation (standard option in the official Windows installer).

cd /d "%~dp0"
start "" pythonw "my_claude_agent_app.py"
