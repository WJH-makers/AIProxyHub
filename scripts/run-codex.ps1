$ErrorActionPreference = "Stop"

# 透传给 codex 的参数（例如：/model、-p profile、--help 等）
# 说明：
# - 不使用 param + ValueFromRemainingArguments，是为了避免 PowerShell 的“通用参数缩写”
#   把 `-p` 误判为 `-ProgressAction/-PipelineVariable` 等，导致无法把参数透传给 codex。
$CodexArgs = @($args)

function _FirstExistingFile([string[]]$candidates) {
  foreach ($p in $candidates) {
    if ($p -and (Test-Path $p)) { return $p }
  }
  return ""
}

# LOCALAPPDATA 在某些非交互/受限 shell 中可能不存在；兜底拼出默认路径
$localAppData = [string]$env:LOCALAPPDATA
if ([string]::IsNullOrWhiteSpace($localAppData)) {
  $localAppData = Join-Path $env:USERPROFILE "AppData\\Local"
}

# 已显式设置则直接使用（避免覆盖用户会话）
if (-not ([string]$env:AIPH_API_KEY).Trim()) {
  $root = Resolve-Path (Join-Path $PSScriptRoot "..")
  $py = Join-Path $root ".venv\\Scripts\\python.exe"
  if (-not (Test-Path $py)) {
    # 兜底：允许用户用系统 python（但更建议先创建 .venv）
    $py = (Get-Command python -ErrorAction Stop).Source
  }

  $settingsFile = _FirstExistingFile @(
    (Join-Path $localAppData "AIProxyHub\\settings.json"),
    (Join-Path $root "settings.json")
  )
  if (-not $settingsFile) {
    throw "未找到 AIProxyHub settings.json：请先启动一次 AIProxyHub 并在配置页设置 API Key。"
  }

  # 通过 launcher.load_settings() 读取并解密（DPAPI）本地保存的 api_key（/v1/* 代理客户端密钥）
  # 重要：此处不会回显任何 key 明文；仅写入当前 PowerShell 进程环境变量。
  $token = & $py -c @"
import sys
root = r'''$root'''
# 确保从脚本所在 repo 根目录导入 launcher.py（允许从任意 cwd 运行本脚本）
if root and root not in sys.path:
    sys.path.insert(0, root)
import launcher
launcher.SETTINGS_FILE = r'''$settingsFile'''
s = launcher.load_settings()
print((s.get('api_key') or s.get('admin_api_key') or '').strip())
"@
  $token = ([string]$token).Trim()
  if (-not $token) {
    throw "settings.json 中未找到可用的 API Key（api_key/admin_api_key 为空）。请先在 AIProxyHub 配置页设置。"
  }
  $env:AIPH_API_KEY = $token
}

& codex @CodexArgs
