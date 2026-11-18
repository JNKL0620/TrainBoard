@echo off
setlocal
set PYTHONUTF8=1
chcp 65001 >nul
cd /d "%~dp0"
if not exist panel.json copy /y panel.example.json panel.json >nul
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -m trainboard %*
) else (
  python -m trainboard %*
)
if errorlevel 1 pause
