@echo off
chcp 65001 >nul 2>&1
title AIProxyHub
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo.
echo   AIProxyHub v2.0.0-clean
echo   本地 AI 网关 + 管理面板
echo.
echo   正在启动管理面板...
echo   浏览器将自动打开（如未打开，请以控制台输出的 URL 为准）
echo.

rem 优先使用官方 Python Launcher；没有 py.exe 时使用 PATH 中的 python。
where py.exe >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
) else (
    set "PYTHON_CMD=python"
)

rem 先验证宿主 Python，避免后面只显示模糊的依赖错误。
%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [错误] 需要 Python 3.10+ 才能运行 AIProxyHub。
    echo   请安装官方 Python 3.10+ 后重试（建议勾选 Add Python to PATH）。
    goto :failed
)

set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo   [INFO] 正在创建虚拟环境：.venv
    %PYTHON_CMD% -m venv "%ROOT%\.venv"
    if errorlevel 1 (
        echo.
        echo   [错误] 无法创建虚拟环境，请检查 Python 安装和目录权限。
        goto :failed
    )
)

rem 依赖已满足时不访问网络；只有首次启动或版本不匹配才补齐 pip 并安装。
"%VENV_PY%" -c "from importlib.metadata import version; raise SystemExit(0 if version('zstandard') == '0.25.0' else 1)" >nul 2>&1
if errorlevel 1 (
    rem 某些精简版 Python 会创建 venv 但不带 pip；先补齐 pip。
    "%VENV_PY%" -m pip --version >nul 2>&1
    if errorlevel 1 (
        echo   [INFO] 正在为虚拟环境安装 pip...
        "%VENV_PY%" -m ensurepip --upgrade
        if errorlevel 1 (
            echo.
            echo   [错误] 无法为虚拟环境安装 pip。
            goto :failed
        )
    )
    echo   [INFO] 正在安装运行依赖...
    "%VENV_PY%" -m pip install --disable-pip-version-check -r "%ROOT%\requirements.txt"
    if errorlevel 1 (
        echo.
        echo   [错误] 运行依赖安装失败。上方保留了实际错误输出，请先修复网络或 Python 环境后重试。
        goto :failed
    )
)

"%VENV_PY%" "%ROOT%\launcher.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo   [错误] launcher.py 运行失败（退出码：%RC%）。
)
if not "%AIPROXYHUB_NO_PAUSE%"=="1" pause
endlocal & exit /b %RC%

:failed
set "RC=1"
if not "%AIPROXYHUB_NO_PAUSE%"=="1" pause
endlocal & exit /b %RC%
