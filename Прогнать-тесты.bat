@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo Не найден Python в виртуальном окружении: "%PY%"
  pause
  exit /b 1
)

rem Без аргументов показываем подсказку, а не гадаем за пользователя.
if "%~1"=="" (
  echo Прогон набора тестов.
  echo.
  echo Использование:
  echo   %~nx0 chat_single
  echo   %~nx0 chat_single --runs 2 --report
  echo   %~nx0 speed,chat_multi --report
  echo.
  echo Доступные наборы:
  for /d %%D in ("%~dp0tests\*") do echo   %%~nxD
  echo.
  echo Всё остальное передаётся run_tests.py как есть: --runs, --limit,
  echo --tags, --reasoning-allowance, --report, --dry-run.
  echo.
  pause
  exit /b 0
)

echo Прогон: %*
echo.
"%PY%" -u run_tests.py %*
set "CODE=%ERRORLEVEL%"

echo.
echo Код выхода: %CODE%
pause
exit /b %CODE%
