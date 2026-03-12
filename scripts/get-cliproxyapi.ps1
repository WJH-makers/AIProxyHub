param(
  # 默认下载 router-for-me/CLIProxyAPI 的 latest release（Windows amd64）。
  [switch]$Force
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$outExe = Join-Path $root "cli-proxy-api.exe"

if ((Test-Path $outExe) -and (-not $Force)) {
  Write-Host "[CLIProxyAPI] 已存在：$outExe"
  Write-Host "如需覆盖，请加 -Force"
  exit 0
}

$api = "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest"
Write-Host "[CLIProxyAPI] 获取 latest release 信息..."

# GitHub API 要求 User-Agent
$release = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "AIProxyHub" }
if (-not $release) { throw "无法读取 GitHub latest release：$api" }

$asset = $null
foreach ($a in ($release.assets | Where-Object { $_ })) {
  if ($a.name -match "_windows_amd64\\.zip$") { $asset = $a; break }
}
if (-not $asset) {
  throw "未找到 windows_amd64.zip 资产。请打开 release 手动确认：$($release.html_url)"
}

$url = [string]$asset.browser_download_url
$name = [string]$asset.name
if (-not $url) { throw "未找到下载地址（browser_download_url 为空）" }

$tmpDir = Join-Path $env:TEMP ("aiproxyhub_cli_" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $tmpDir | Out-Null
$zipPath = Join-Path $tmpDir $name
$extractDir = Join-Path $tmpDir "extract"

try {
  Write-Host "[CLIProxyAPI] 下载：$name"
  Write-Host "  $url"
  curl.exe -L $url -o $zipPath | Out-Null
  if (-not (Test-Path $zipPath)) { throw "下载失败：$zipPath" }

  Write-Host "[CLIProxyAPI] 解压..."
  Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

  # 兼容不同打包命名：cli-proxy-api.exe / CLIProxyAPI.exe
  $cand = @(
    (Get-ChildItem -Path $extractDir -Recurse -File -Filter "cli-proxy-api.exe" -ErrorAction SilentlyContinue | Select-Object -First 1),
    (Get-ChildItem -Path $extractDir -Recurse -File -Filter "CLIProxyAPI.exe" -ErrorAction SilentlyContinue | Select-Object -First 1)
  ) | Where-Object { $_ } | Select-Object -First 1

  if (-not $cand) {
    $exes = Get-ChildItem -Path $extractDir -Recurse -File -Filter "*.exe" -ErrorAction SilentlyContinue | Select-Object -First 10
    $list = ($exes | ForEach-Object { $_.FullName }) -join "`n"
    throw "解压后未找到 cli-proxy-api.exe/CLIProxyAPI.exe。候选 exe：`n$list"
  }

  Copy-Item -Force $cand.FullName $outExe
  $h = (Get-FileHash -Algorithm SHA256 $outExe).Hash
  Write-Host "[CLIProxyAPI] 已写入：$outExe"
  Write-Host "[CLIProxyAPI] SHA256: $h"
} finally {
  try { Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue } catch {}
}

