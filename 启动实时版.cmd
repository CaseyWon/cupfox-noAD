@echo off
chcp 65001 >nul
title 茶杯狐纯净实时版

set "BUNDLED_PYTHON=C:\Users\wangjie\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

echo.
echo 茶杯狐纯净实时版正在启动...
echo 电脑访问: http://127.0.0.1:8765/
echo 手机访问: http://192.168.126.43:8765/
echo.
echo 请保持此窗口开启。按 Ctrl+C 可停止服务。
echo.

if exist "%BUNDLED_PYTHON%" (
  "%BUNDLED_PYTHON%" "%~dp0server.py" --host 0.0.0.0 --port 8765 --proxy http://127.0.0.1:7890
) else (
  py -3 "%~dp0server.py" --host 0.0.0.0 --port 8765 --proxy http://127.0.0.1:7890
)

pause
