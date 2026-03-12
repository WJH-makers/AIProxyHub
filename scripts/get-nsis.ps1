param(
  [string]$Version = "3.11"
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$toolsDir = Join-Path $root ".tools"
$nsisDir = Join-Path $toolsDir ("nsis-" + $Version)

# NSIS zip 解压后结构：<nsisDir>\nsis-<ver>\Bin\makensis.exe
$makensis = Join-Path $nsisDir ("nsis-" + $Version + "\\Bin\\makensis.exe")
if (Test-Path $makensis) {
  Write-Output $makensis
  exit 0
}

New-Item -ItemType Directory -Force $nsisDir | Out-Null

$zipName = "nsis-$Version.zip"
$zipPath = Join-Path $nsisDir $zipName
$url = "https://sourceforge.net/projects/nsis/files/NSIS%203/$Version/$zipName/download"

Write-Host "[NSIS] 下载 NSIS $Version（zip）..."
Write-Host "  $url"

# 说明：
# - 使用 curl -L 以兼容 SourceForge 的跳转下载
# - 若网络环境受限，请自行设置代理（例如：$env:HTTPS_PROXY）
curl.exe -L $url -o $zipPath | Out-Null

if (-not (Test-Path $zipPath)) {
  throw "NSIS 下载失败：$zipPath"
}

Write-Host "[NSIS] 解压..."
Expand-Archive -Path $zipPath -DestinationPath $nsisDir -Force

if (-not (Test-Path $makensis)) {
  throw "未找到 makensis.exe（解压后仍不存在）：$makensis"
}

Write-Output $makensis

