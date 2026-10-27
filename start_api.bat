@echo off
if not exist .venv\Scripts\python.exe (
  echo Run setup_windows.bat first.
  exit /b 1
)
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
