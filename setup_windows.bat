@echo off
setlocal
if not exist .venv\Scripts\python.exe (
  py -3.11 -m venv .venv
  if errorlevel 1 goto :error
)
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error
if not exist .env copy .env.example .env
 echo.
echo Setup complete. Run start_api.bat and start_dashboard.bat in separate terminals.
goto :end
:error
echo Setup failed. Review the error above; verify Python 3.11 is installed and internet access is available.
exit /b 1
:end
endlocal
