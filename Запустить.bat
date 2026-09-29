@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo Не найден Python в виртуальном окружении:
  echo   "%PY%"
  echo.
  echo Похоже, окружение не создано. Соберите его так:
  echo   py -3.13 -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

if not exist "%~dp0main.py" (
  echo Не найден main.py рядом с этим файлом.
  echo Ярлык должен лежать в корне проекта.
  pause
  exit /b 1
)

echo Запускаю LLM Test Bench...
echo Python: "%PY%"
echo.

"%PY%" -u main.py
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
  echo.
  echo Приложение завершилось с кодом %CODE%.
  pause
  exit /b %CODE%
)

endlocal
