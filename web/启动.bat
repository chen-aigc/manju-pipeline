@echo off
chcp 65001 >nul
title 漫剧工厂
cd /d "%~dp0"

echo ========================================
echo   漫剧工厂 启动中...
echo ========================================
echo.

set PYTHONPATH=%~dp0..\src
set PY=%~dp0..\.venv\Scripts\python.exe

if not exist "%PY%" (
  echo [错误] 找不到 Python：%PY%
  echo 请确认虚拟环境 .venv 存在
  pause
  exit /b 1
)

echo [1/3] 检查环境...
"%PY%" -c "import fastapi,uvicorn" 2>nul
if errorlevel 1 (
  echo [错误] 缺少依赖，请先运行：
  echo   .venv\Scripts\python.exe -m pip install fastapi uvicorn openpyxl
  pause
  exit /b 1
)
echo       OK
echo.

echo [2/3] 启动服务（端口 8501）...
start "" /b "%PY%" -m manju.cli web --no-open

echo [3/3] 等待服务就绪...
set /a n=0
:wait
timeout /t 1 >nul
set /a n+=1
curl -s -o nul http://127.0.0.1:8501/api/health 2>nul
if not errorlevel 1 goto ready
if %n% lss 20 goto wait
echo      超时，请手动打开 http://127.0.0.1:8501
goto opened

:ready
echo      就绪

:opened
start "" http://127.0.0.1:8501
echo.
echo ========================================
echo   浏览器已打开：http://127.0.0.1:8501
echo   关闭此窗口 = 停止服务
echo ========================================
echo.
pause
