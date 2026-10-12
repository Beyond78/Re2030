@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title 2030 仿真演示平面

rem ── 端口：可传第一个参数，默认 8000 ──────────────────
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

rem ── 查找可用的 Python 3 ─────────────────────────────
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY if exist "D:\python\python.exe" set "PY=D:\python\python.exe"

if not defined PY (
  echo.
  echo [错误] 没有找到可用的 Python 3。
  echo         请安装 Python 3.10+ 并勾选 "Add Python to PATH"，
  echo         或把 python.exe 放在 D:\python\ 下。
  echo.
  pause
  exit /b 1
)

rem ── 报文数据检查 ────────────────────────────────────
if not exist "data\message_core\import-report.json" (
  echo.
  echo [提示] 还没有导入 message_core 报文数据。
  echo         "变电站网络攻击场景" 会显示为不可用，其余场景不受影响。
  echo         导入方法（把 message_core.zip 路径填在后面）：
  echo             %PY% tools\import_message_core.py "路径\message_core.zip"
  echo.
)

echo ==================================================
echo   2030 仿真系统 · 演示平面
echo ==================================================
echo   解释器 ：%PY%
echo   地址   ：http://127.0.0.1:%PORT%/
echo   停止   ：Ctrl+C
echo ==================================================
echo.

rem ── 等 2 秒后自动打开浏览器（服务启动需要一点时间）──
start "" /min cmd /c "ping -n 3 127.0.0.1 >nul & start http://127.0.0.1:%PORT%/"

%PY% main.py --ui --scenario scenarios\substation-live.json --output runs --port %PORT%

echo.
echo 服务已停止。
pause
