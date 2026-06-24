@echo off
cd /d "%~dp0"
python llmeshowyou_gui.py
if errorlevel 1 (
    echo.
    echo Python not found. Make sure Python is installed and on your PATH.
    pause
)
