@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "ROOT=%~dp0"
set "BE=%ROOT%backend"
set "FE=%ROOT%frontend"

title Pricing App - start

where docker >nul 2>nul
if %ERRORLEVEL% equ 0 (
  echo [1/4] Docker: postgres + redis ...
  docker compose up -d postgres redis
  if !ERRORLEVEL! neq 0 (
    echo     Warning: docker compose failed. Start Docker Desktop or use local Postgres/Redis.
  ) else (
    echo     Waiting for databases ...
    timeout /t 6 /nobreak >nul
  )
) else (
  echo [1/4] Docker not in PATH - skip containers.
  timeout /t 2 /nobreak >nul
)

echo [2/4] Backend API http://127.0.0.1:8000 ...
start "Pricing-Backend" cmd /k "cd /d "!BE!" && py -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"

timeout /t 2 /nobreak >nul

echo [3/4] Celery worker ...
start "Pricing-Celery" cmd /k "cd /d "!BE!" && py -m celery -A app.celery_app worker --loglevel=info"

timeout /t 2 /nobreak >nul

echo [4/4] Frontend http://localhost:3000 ...
start "Pricing-Frontend" cmd /k "cd /d "!FE!" && npm run dev"

echo.
echo === Started ===
echo   UI:     http://localhost:3000
echo   API:    http://127.0.0.1:8000/docs
echo   Health: http://127.0.0.1:8000/health
echo.
echo Close each titled window to stop that service.
pause
