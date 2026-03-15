# AIProxyHub 快速管理脚本
# 用法:
#   aiph start    - 启动 (后台静默)
#   aiph stop     - 停止
#   aiph status   - 查看状态
#   aiph restart  - 重启
#   aiph log      - 查看日志
#   aiph auto on  - 开机自启
#   aiph auto off - 取消自启
#   aiph failover - 检测本地代理，不可用时自动切到 InfiniteAI
#   aiph switch local|cloud - 手动切换 Codex provider

param(
    [Parameter(Position=0)]
    [ValidateSet('start','stop','status','restart','log','auto','failover','switch')]
    [string]$Action = 'status',

    [Parameter(Position=1)]
    [string]$SubAction = ''
)

$ErrorActionPreference = 'Stop'
$AIPH_DIR = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$AIPH_EXE = Join-Path $AIPH_DIR 'cli-proxy-api.exe'
$AIPH_PORT = 8317
$PROCESS_NAME = 'cli-proxy-api'
$TASK_NAME = 'AIProxyHub-AutoStart'
$RUNTIME_DIR = Join-Path $env:TEMP 'AIProxyHub'
$RUNTIME_YAML = Join-Path $RUNTIME_DIR 'cli-proxy-api.runtime.yaml'
$APP_DATA_DIR = Join-Path $env:LOCALAPPDATA 'AIProxyHub'
$CODEX_CONFIG = Join-Path $env:USERPROFILE '.codex\config.toml'

function Get-AiphProcess {
    Get-Process -Name $PROCESS_NAME -ErrorAction SilentlyContinue
}

function Test-PortListening {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect('127.0.0.1', $AIPH_PORT)
        $tcp.Close()
        return $true
    } catch {
        return $false
    }
}

function Test-ApiHealth {
    try {
        $key = [Environment]::GetEnvironmentVariable('AIPH_API_KEY', 'User')
        if (-not $key) { $key = $env:AIPH_API_KEY }
        if (-not $key) { return $false }
        $headers = @{ 'Authorization' = "Bearer $key" }
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$AIPH_PORT/v1/models" -Headers $headers -TimeoutSec 5 -ErrorAction SilentlyContinue
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Ensure-RuntimeConfig {
    if (Test-Path $RUNTIME_YAML) { return $RUNTIME_YAML }
    $alt = Join-Path $APP_DATA_DIR 'cli-proxy-api.runtime.yaml'
    if (Test-Path $alt) { return $alt }
    $venv_python = Join-Path $AIPH_DIR '.venv\Scripts\python.exe'
    $py = if (Test-Path $venv_python) { $venv_python } else { 'python' }
    & $py -c @"
import sys, os
sys.path.insert(0, r'$AIPH_DIR')
os.chdir(r'$AIPH_DIR')
from launcher import load_settings, generate_proxy_config, RUNTIME_PROXY_CONFIG, _ensure_runtime_dir
_ensure_runtime_dir()
s = load_settings()
generate_proxy_config(s, RUNTIME_PROXY_CONFIG)
print('OK: ' + RUNTIME_PROXY_CONFIG)
"@
    if (Test-Path $RUNTIME_YAML) { return $RUNTIME_YAML }
    if (Test-Path $alt) { return $alt }
    Write-Host "[ERROR] YAML config not generated" -ForegroundColor Red
    exit 1
}

function Start-Aiph {
    $existing = Get-AiphProcess
    if ($existing) {
        Write-Host "[OK] AIProxyHub already running (PID $($existing.Id))" -ForegroundColor Green
        return
    }

    $yaml = Ensure-RuntimeConfig
    Write-Host "[...] Starting AIProxyHub on :$AIPH_PORT ..." -ForegroundColor Yellow

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $AIPH_EXE
    $psi.Arguments = "-config `"$yaml`""
    $psi.WorkingDirectory = $AIPH_DIR
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $false
    $psi.RedirectStandardError = $false
    $psi.EnvironmentVariables['HOME'] = $env:USERPROFILE
    $psi.EnvironmentVariables['USERPROFILE'] = $env:USERPROFILE

    $proc = [System.Diagnostics.Process]::Start($psi)
    $null = $proc.Handle

    Start-Sleep -Seconds 3

    if ($proc.HasExited) {
        Write-Host "[FAIL] Process exited with code $($proc.ExitCode)" -ForegroundColor Red
        return
    }

    if (Test-PortListening) {
        Write-Host "[OK] AIProxyHub started (PID $($proc.Id), port $AIPH_PORT)" -ForegroundColor Green
    } else {
        Write-Host "[WARN] Process running (PID $($proc.Id)) but port $AIPH_PORT not yet ready" -ForegroundColor Yellow
    }
}

function Stop-Aiph {
    $procs = Get-AiphProcess
    if (-not $procs) {
        Write-Host "[OK] AIProxyHub not running" -ForegroundColor Yellow
        return
    }
    foreach ($p in $procs) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Write-Host "[OK] Stopped PID $($p.Id)" -ForegroundColor Green
    }
}

function Get-CurrentProvider {
    if (-not (Test-Path $CODEX_CONFIG)) { return 'unknown' }
    $line = Select-String -Path $CODEX_CONFIG -Pattern '^\s*model_provider\s*=' -List | Select-Object -First 1
    if ($line) {
        $val = ($line.Line -split '=', 2)[1].Trim().Trim('"').Trim("'")
        return $val
    }
    return 'unknown'
}

function Switch-Provider {
    param([string]$Target)

    if (-not (Test-Path $CODEX_CONFIG)) {
        Write-Host "[ERROR] Codex config not found: $CODEX_CONFIG" -ForegroundColor Red
        return
    }

    # 只替换文件顶部的 model_provider（前 10 行），不影响 profiles
    $lines = Get-Content $CODEX_CONFIG
    $replaced = $false
    if ($Target -eq 'local') {
        $newVal = '"OpenAI"'
        $label = 'OpenAI (local :8317)'
    } elseif ($Target -eq 'cloud') {
        $newVal = '"InfiniteAPI"'
        $label = 'InfiniteAPI (cloud)'
    } else {
        Write-Host "Usage: aiph switch local|cloud" -ForegroundColor Yellow
        return
    }

    for ($i = 0; $i -lt [Math]::Min(15, $lines.Count); $i++) {
        if ($lines[$i] -match '^\s*model_provider\s*=') {
            $lines[$i] = "model_provider = $newVal"
            $replaced = $true
            break
        }
    }

    if ($replaced) {
        Set-Content -Path $CODEX_CONFIG -Value $lines
        Write-Host "[OK] Codex provider -> $label" -ForegroundColor Green
        Write-Host "     Restart Codex to apply." -ForegroundColor DarkGray
    } else {
        Write-Host "[ERROR] model_provider not found in first 15 lines" -ForegroundColor Red
    }
}

function Invoke-Failover {
    $current = Get-CurrentProvider
    Write-Host "Current Codex provider: $current" -ForegroundColor Cyan

    $localOk = Test-ApiHealth
    if ($localOk) {
        Write-Host "[OK] Local proxy healthy" -ForegroundColor Green
        if ($current -ne 'OpenAI') {
            Write-Host "[FAILBACK] Switching back to local proxy..." -ForegroundColor Yellow
            Switch-Provider 'local'
        }
    } else {
        Write-Host "[WARN] Local proxy unreachable" -ForegroundColor Red
        # 尝试启动
        Start-Aiph
        Start-Sleep -Seconds 2
        $localOk = Test-ApiHealth
        if ($localOk) {
            Write-Host "[OK] Local proxy recovered after restart" -ForegroundColor Green
            if ($current -ne 'OpenAI') {
                Switch-Provider 'local'
            }
        } else {
            Write-Host "[FAILOVER] Switching to InfiniteAI..." -ForegroundColor Yellow
            Switch-Provider 'cloud'
        }
    }
}

function Show-Status {
    $proc = Get-AiphProcess
    if ($proc) {
        $port_ok = Test-PortListening
        $status = if ($port_ok) { "RUNNING (ready)" } else { "RUNNING (port not ready)" }
        $color = if ($port_ok) { 'Green' } else { 'Yellow' }
        Write-Host "AIProxyHub: $status" -ForegroundColor $color
        Write-Host "  PID:  $($proc.Id)"
        Write-Host "  Port: $AIPH_PORT"
        Write-Host "  Uptime: $(((Get-Date) - $proc.StartTime).ToString('hh\:mm\:ss'))"
    } else {
        Write-Host "AIProxyHub: STOPPED" -ForegroundColor Red
    }

    $current = Get-CurrentProvider
    Write-Host "  Codex Provider: $current" -ForegroundColor Cyan

    $task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    if ($task) {
        Write-Host "  AutoStart: ON ($($task.State))" -ForegroundColor Cyan
    } else {
        Write-Host "  AutoStart: OFF" -ForegroundColor DarkGray
    }
}

function Set-AutoStart {
    param([bool]$Enable)

    if ($Enable) {
        $scriptPath = $PSCommandPath
        $action = New-ScheduledTaskAction `
            -Execute 'powershell.exe' `
            -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`" start"
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Hours 0)

        Register-ScheduledTask `
            -TaskName $TASK_NAME `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description 'AIProxyHub auto-start on login' `
            -Force | Out-Null

        Write-Host "[OK] AutoStart enabled (Task: $TASK_NAME)" -ForegroundColor Green
    } else {
        Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "[OK] AutoStart disabled" -ForegroundColor Green
    }
}

# 主入口
switch ($Action) {
    'start'    { Start-Aiph }
    'stop'     { Stop-Aiph }
    'status'   { Show-Status }
    'restart'  { Stop-Aiph; Start-Sleep -Seconds 1; Start-Aiph }
    'failover' { Invoke-Failover }
    'switch'   { Switch-Provider $SubAction }
    'log'      {
        $logDir = Join-Path $AIPH_DIR 'logs'
        if (-not (Test-Path $logDir)) { $logDir = Join-Path $RUNTIME_DIR 'logs' }
        $latest = Get-ChildItem $logDir -Filter '*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latest) {
            Get-Content $latest.FullName -Tail 50
        } else {
            Write-Host "No log files found" -ForegroundColor Yellow
        }
    }
    'auto'     {
        switch ($SubAction) {
            'on'  { Set-AutoStart -Enable $true }
            'off' { Set-AutoStart -Enable $false }
            default { Write-Host "Usage: aiph auto on|off" -ForegroundColor Yellow }
        }
    }
}
