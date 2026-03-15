@echo off
chcp 65001 >nul 2>&1
title AIProxyHub
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
echo.
echo   ╔══════════════════════════════════╗
echo   ║       AIProxyHub v1.2.14         ║
echo   ║   ChatGPT 注册 + 代理 一体化    ║
echo   ╚══════════════════════════════════╝
echo.
echo   正在启动管理面板...
echo   浏览器将自动打开（如未打开，请以控制台输出的 URL 为准）
echo.

set "ROOT=%~dp0"

rem 优先使用 py 启动，避免 python 指向错误版本/未安装的情况
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    set "PY=python"
)

rem 检查 Python 版本（需要 3.10+）
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo   [错误] 需要 Python 3.10+ 才能运行 AIProxyHub
    echo   请安装官方 Python 3.10+ 后重试（建议勾选 Add Python to PATH）
    echo.
    if not "%AIPROXYHUB_NO_PAUSE%"=="1" pause
    exit /b 1
)

rem 创建/复用虚拟环境，保证依赖一致
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo   [INFO] 正在创建虚拟环境: .venv
    %PY% -m venv "%ROOT%.venv"
    if %errorlevel% neq 0 (
        echo.
        echo   [错误] 无法创建虚拟环境
        echo   请确保已安装 Python 3.10+（建议安装官方 Python）
        echo.
        if not "%AIPROXYHUB_NO_PAUSE%"=="1" pause
        exit /b 1
    )
)

call "%ROOT%.venv\Scripts\activate.bat"
if %errorlevel% neq 0 (
    echo.
    echo   [错误] 无法激活虚拟环境
    echo.
    if not "%AIPROXYHUB_NO_PAUSE%"=="1" pause
    exit /b 1
)

echo   [INFO] 正在安装/更新依赖（requirements.txt）...
python -m pip install -U pip >nul 2>&1
if %errorlevel% neq 0 (
    echo   [WARN] pip 更新失败，将继续尝试安装依赖...
)
python -m pip install -r "%ROOT%requirements.txt"
if %errorlevel% neq 0 (
    echo.
    echo   [错误] 依赖安装失败
    echo   可能原因：网络/代理不可用、权限不足、pip 源不可达
    echo.
    if not "%AIPROXYHUB_NO_PAUSE%"=="1" pause
    exit /b 1
)

python "%ROOT%launcher.py" %*
if %errorlevel% neq 0 (
    echo.
    echo   [错误] launcher.py 运行失败
    echo   请检查上方错误输出。
    echo.
)
if not "%AIPROXYHUB_NO_PAUSE%"=="1" pause
endlocal
