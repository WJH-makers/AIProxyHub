param(
  [switch]$OneDir,
  [switch]$Console
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$py = Join-Path $root ".venv\\Scripts\\python.exe"

if (-not (Test-Path $py)) {
  throw "未找到虚拟环境 Python：$py。请先双击 启动.bat 或手动创建 .venv。"
}

Push-Location $root
try {
  Write-Host "[1/4] 生成 EXE + zip 发布包..."
  $args = @()
  if ($OneDir) { $args += "-OneDir" }
  if ($Console) { $args += "-Console" }
  powershell -ExecutionPolicy Bypass -File (Join-Path $root "scripts\\build-release.ps1") @args

  $ver = (& $py -c "import launcher; print(getattr(launcher,'APP_VERSION','unknown'))").Trim()
  if (-not $ver) { throw "无法读取 APP_VERSION" }

  Write-Host "[2/4] 准备 NSIS（本地 .tools，避免全局安装）..."
  $makensis = powershell -ExecutionPolicy Bypass -File (Join-Path $root "scripts\\get-nsis.ps1") | Select-Object -Last 1
  $makensis = ($makensis | Out-String).Trim()
  if (-not (Test-Path $makensis)) {
    throw "未找到 makensis.exe：$makensis"
  }

  Write-Host "[3/4] 生成安装包（setup.exe）..."
  $releaseDir = Join-Path $root "release"
  New-Item -ItemType Directory -Force $releaseDir | Out-Null

  $out = Join-Path $releaseDir ("AIProxyHub-$ver-setup-win64.exe")
  $nsi = Join-Path $root "installer\\AIProxyHub.nsi"
  if (-not (Test-Path $nsi)) { throw "未找到 NSIS 脚本：$nsi" }

  # 兼容性：NSIS 对“UTF-8 无 BOM”的脚本在部分环境会报 `Bad text encoding`。
  # 这里做一次兜底，确保 .nsi 文件为 UTF-8 BOM（不改变内容，只修复编码）。
  try {
    $bytes = [System.IO.File]::ReadAllBytes($nsi)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    if (-not $hasBom) {
      Write-Host "[NSIS] 检测到脚本缺少 UTF-8 BOM，自动修复：$nsi"
      $txt = Get-Content -Path $nsi -Raw -Encoding UTF8
      Set-Content -Path $nsi -Value $txt -Encoding utf8BOM
    }
  } catch {
    Write-Host "[NSIS] ⚠️ 无法自动检查/修复 .nsi 编码（将继续尝试构建）：$($_.Exception.Message)"
  }

  $defs = @("/DAPP_VERSION=$ver", "/DOUTFILE=$out")
  if ($OneDir) { $defs += "/DONEDIR=1" }
  & $makensis @defs $nsi | Out-Host
  if ($LASTEXITCODE -ne 0) {
    throw "makensis 失败，退出码 $LASTEXITCODE"
  }

  if (-not (Test-Path $out)) {
    throw "安装包未生成：$out"
  }

  Write-Host "[4/4] 完成"
  Write-Host "SETUP: $out"
} finally {
  Pop-Location
}
