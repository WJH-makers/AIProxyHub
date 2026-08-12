# AIProxyHub 快速管理脚本
# 用法：aiph start|stop|status|restart|log|auto on|off
#
# 这个脚本只管理 AIProxyHub 本身。它不会修改 Codex/其他客户端配置，
# 也不会在 provider 之间自动切换或为了规避上游限额更换项目/账号。

param(
    [Parameter(Position=0)]
    [ValidateSet('start','stop','status','restart','log','auto')]
    [string]$Action = 'status',

    [Parameter(Position=1)]
    [string]$SubAction = ''
)

$ErrorActionPreference = 'Stop'
$_scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$AIPH_DIR = (Resolve-Path (Join-Path $_scriptDir '..')).Path
$PROCESS_NAME = 'cli-proxy-api'
$TASK_NAME = 'AIProxyHub-AutoStart'

function Get-AiphRoot {
    if ($env:AIPROXYHUB_HOME) {
        return [Environment]::ExpandEnvironmentVariables($env:AIPROXYHUB_HOME)
    }
    $localRoot = Join-Path $env:LOCALAPPDATA 'AIProxyHub'
    if (Test-Path -LiteralPath (Join-Path $localRoot 'settings.json')) {
        return $localRoot
    }
    return $AIPH_DIR
}

function Get-AiphProxyPort {
    $port = 8317
    $settingsPath = Join-Path (Get-AiphRoot) 'settings.json'
    try {
        if (Test-Path -LiteralPath $settingsPath) {
            $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
            $candidate = [int]$settings.proxy_port
            if ($candidate -ge 1 -and $candidate -le 65535) { $port = $candidate }
        }
    } catch {}
    return $port
}

function Get-AiphProcess {
    $py = Get-Process -Name 'python','pythonw' -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -like '*AIProxyHub*' } catch { $false }
    }
    $go = Get-Process -Name $PROCESS_NAME -ErrorAction SilentlyContinue
    if ($py) { return $py }
    return $go
}

function Test-PortListening {
    param([int]$Port = (Get-AiphProxyPort))
    $tcp = New-Object System.Net.Sockets.TcpClient
    try {
        $tcp.Connect('127.0.0.1', $Port)
        return $true
    } catch {
        return $false
    } finally {
        $tcp.Dispose()
    }
}

function Start-Aiph {
    $existing = Get-AiphProcess
    if ($existing) {
        Write-Host "[OK] AIProxyHub already running (PID $($existing[0].Id))" -ForegroundColor Green
        return
    }

    $venvPython = Join-Path $AIPH_DIR '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $venvPython = Join-Path $AIPH_DIR '.venv\Scripts\python.exe'
    }
    if (-not (Test-Path -LiteralPath $venvPython)) { $venvPython = 'python' }
    $launcherPy = Join-Path $AIPH_DIR 'launcher.py'

    Write-Host '[...] Starting AIProxyHub (via launcher.py) ...' -ForegroundColor Yellow
    Start-Process -FilePath $venvPython `
        -ArgumentList @($launcherPy, '--no-browser', '--host', '127.0.0.1') `
        -WorkingDirectory $AIPH_DIR -WindowStyle Hidden | Out-Null

    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        $proxyPort = Get-AiphProxyPort
        if (Test-PortListening -Port $proxyPort) {
            Write-Host "[OK] AIProxyHub started (proxy port $proxyPort)" -ForegroundColor Green
            return
        }
    }
    $goProcs2 = Get-Process -Name $PROCESS_NAME -ErrorAction SilentlyContinue
    if ($goProcs2) {
        Write-Host "[OK] AIProxyHub started (PID $($goProcs2[0].Id), port still initializing)" -ForegroundColor Yellow
    } else {
        Write-Host '[WARN] launcher started but the proxy port is not ready' -ForegroundColor Yellow
    }
}

function Stop-Aiph {
    $stopped = 0
    foreach ($name in @($PROCESS_NAME, 'python', 'pythonw')) {
        $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
        foreach ($p in $procs) {
            $isAiph = $false
            try {
                if ($name -eq $PROCESS_NAME) {
                    $isAiph = $true
                } elseif ($p.Path -like '*AIProxyHub*') {
                    $isAiph = $true
                } else {
                    $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue).CommandLine
                    $isAiph = [bool]($cmdLine -and $cmdLine -like '*AIProxyHub*launcher.py*')
                }
            } catch {}
            if ($isAiph) {
                Stop-Process -Id $p.Id -ErrorAction SilentlyContinue
                Write-Host "[OK] Stopped $name PID $($p.Id)" -ForegroundColor Green
                $stopped++
            }
        }
    }
    if ($stopped -eq 0) { Write-Host '[OK] AIProxyHub not running' -ForegroundColor Yellow }
}

function Show-Status {
    $proc = Get-AiphProcess
    $proxyPort = Get-AiphProxyPort
    if ($proc) {
        $ready = Test-PortListening -Port $proxyPort
        $status = if ($ready) { 'RUNNING (ready)' } else { 'RUNNING (port not ready)' }
        $color = if ($ready) { 'Green' } else { 'Yellow' }
        Write-Host "AIProxyHub: $status" -ForegroundColor $color
        Write-Host "  PID:         $($proc.Id)"
        Write-Host "  Proxy port:  $proxyPort"
        try { Write-Host "  Uptime:      $(((Get-Date) - $proc.StartTime).ToString('hh\:mm\:ss'))" } catch {}
    } else {
        Write-Host 'AIProxyHub: STOPPED' -ForegroundColor Red
        Write-Host "  Proxy port:  $proxyPort"
    }

    $task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    if ($task) { Write-Host "  AutoStart:   ON ($($task.State))" -ForegroundColor Cyan }
    else { Write-Host '  AutoStart:   OFF' -ForegroundColor DarkGray }
}

function Set-AutoStart {
    param([bool]$Enable)
    if ($Enable) {
        $scriptPath = $PSCommandPath
        $windowsRoot = if ($env:SystemRoot) { $env:SystemRoot } else { 'C:\Windows' }
        $wscriptPath = Join-Path $windowsRoot 'System32\wscript.exe'
        $powerShellPath = Join-Path $windowsRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $startupDir = Join-Path $env:LOCALAPPDATA 'AIProxyHub\startup'
        New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
        $wrapperPath = Join-Path $startupDir 'aiph-autostart-hidden.vbs'
        $launcherCommand = ('"{0}" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{1}" start' -f $powerShellPath, $scriptPath)
        $wrapperContent = @(
            'Option Explicit',
            'Dim shell, exitCode',
            'Set shell = CreateObject("WScript.Shell")',
            ('exitCode = shell.Run("{0}", 0, True)' -f $launcherCommand.Replace('"', '""')),
            'WScript.Quit exitCode',
            ''
        ) -join "`r`n"
        Set-Content -LiteralPath $wrapperPath -Value $wrapperContent -Encoding Ascii
        $action = New-ScheduledTaskAction -Execute $wscriptPath -Argument "//B //NoLogo `"$wrapperPath`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0)
        Register-ScheduledTask -TaskName $TASK_NAME -Action $action -Trigger $trigger -Settings $settings -Description 'AIProxyHub auto-start on login' -Force | Out-Null
        Write-Host "[OK] AutoStart enabled (Task: $TASK_NAME)" -ForegroundColor Green
    } else {
        Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host '[OK] AutoStart disabled' -ForegroundColor Green
    }
}

switch ($Action) {
    'start'   { Start-Aiph }
    'stop'    { Stop-Aiph }
    'status'  { Show-Status }
    'restart' { Stop-Aiph; Start-Sleep -Seconds 1; Start-Aiph }
    'log' {
        $logDir = Join-Path (Get-AiphRoot) 'logs'
        if (-not (Test-Path -LiteralPath $logDir)) { $logDir = Join-Path $AIPH_DIR 'logs' }
        $latest = Get-ChildItem -LiteralPath $logDir -Filter '*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latest) { Get-Content -LiteralPath $latest.FullName -Tail 50 }
        else { Write-Host 'No log files found' -ForegroundColor Yellow }
    }
    'auto' {
        switch ($SubAction) {
            'on'  { Set-AutoStart -Enable $true }
            'off' { Set-AutoStart -Enable $false }
            default { Write-Host 'Usage: aiph auto on|off' -ForegroundColor Yellow }
        }
    }
}
