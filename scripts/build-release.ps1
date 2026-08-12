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
  Write-Host "[1/5] 安装固定范围的开发/打包依赖..."
  $devRequirements = Join-Path $root "requirements-dev.txt"
  & $py -c "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('pip') else 1)" 2>$null
  $hasPip = $LASTEXITCODE -eq 0
  if ($hasPip) {
    & $py -m pip install -q -r (Join-Path $root "requirements.txt") -r $devRequirements | Out-Null
  } elseif (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "[INFO] 当前虚拟环境无 pip，使用 uv pip 安装开发/打包依赖。"
    & uv pip install --python $py -q -r (Join-Path $root "requirements.txt") -r $devRequirements | Out-Null
  } else {
    Write-Host "[INFO] 当前虚拟环境无 pip，尝试 ensurepip。"
    & $py -m ensurepip --upgrade | Out-Null
    & $py -m pip install -q -r (Join-Path $root "requirements.txt") -r $devRequirements | Out-Null
  }
  if ($LASTEXITCODE -ne 0) { throw "开发/打包依赖安装失败，退出码 $LASTEXITCODE" }

  $cliProxy = Join-Path $root "cli-proxy-api.exe"
  if (-not (Test-Path $cliProxy)) {
    throw "未找到 cli-proxy-api.exe：$cliProxy。它属于第三方组件（CLIProxyAPI Windows 版本）。请先运行：powershell -ExecutionPolicy Bypass -File .\\scripts\\get-cliproxyapi.ps1（或手动下载并放到项目根目录）。"
  }

  $provenancePath = Join-Path $root "cli-proxy-api.provenance.json"
  if (-not (Test-Path -LiteralPath $provenancePath)) {
    throw "未找到第三方二进制 provenance manifest：$provenancePath"
  }
  try {
    $provenance = Get-Content -LiteralPath $provenancePath -Raw | ConvertFrom-Json
    $expected = ([string]$provenance.executable_sha256).Trim().ToUpperInvariant()
    if ($expected -notmatch '^[0-9A-F]{64}$') { throw 'provenance 中的 executable_sha256 无效' }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $cliProxy).Hash.ToUpperInvariant()
    if ($actual -ne $expected) { throw "CLIProxyAPI SHA-256 不匹配：provenance=$expected，实际=$actual" }
  } catch {
    throw "第三方二进制 provenance 校验失败：$($_.Exception.Message)"
  }

  $distDir = Join-Path $root "dist"
  $buildDir = Join-Path $root "build"
  foreach ($d in @($distDir, $buildDir)) {
    if (Test-Path $d) {
      try {
        Remove-Item -Recurse -Force $d
      } catch {
        throw "无法清理构建目录：$d。可能原因：AIProxyHub.exe 正在运行导致文件被占用。请先关闭所有 AIProxyHub 进程（例如：Get-Process AIProxyHub | Stop-Process -Force）后重试。原始错误：$($_.Exception.Message)"
      }
    }
  }

  Write-Host "[2/5] PyInstaller 打包..."
  $args = @("--noconfirm", "--clean", "--name", "AIProxyHub", "--add-binary", ("{0};." -f $cliProxy))
  if ($OneDir) { $args += "--onedir" } else { $args += "--onefile" }
  if (-not $Console) { $args += "--noconsole" }

  & $py -m PyInstaller @args (Join-Path $root "launcher.py")

  if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 失败，退出码 $LASTEXITCODE"
  }

  Write-Host "[3/5] 生成 zip 发布包（不包含 settings.json/data 等本机私密文件）..."
  $ver = (& $py -c "import launcher; print(getattr(launcher,'APP_VERSION','unknown'))").Trim()
  $releaseDir = Join-Path $root "release"
  New-Item -ItemType Directory -Force $releaseDir | Out-Null

  $zipPath = Join-Path $releaseDir ("AIProxyHub-$ver-win64.zip")
  if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

  $exePayload = if ($OneDir) { (Join-Path $distDir "AIProxyHub") } else { (Join-Path $distDir "AIProxyHub.exe") }

  $payload = @(
    $exePayload,
    (Join-Path $root "LICENSE"),
    (Join-Path $root "NOTICE.md"),
    (Join-Path $root "THIRD_PARTY_NOTICES.md"),
    (Join-Path $root "SECURITY.md"),
    (Join-Path $root "PRIVACY.md"),
    (Join-Path $root "MAINTAINERS.md"),
    (Join-Path $root "CHANGELOG.md"),
    (Join-Path $root "licenses"),
    (Join-Path $root "cli-proxy-api.provenance.json"),
    (Join-Path $root "使用指南.md"),
    (Join-Path $root "API.md")
  ) | Where-Object { Test-Path $_ }

  Compress-Archive -Path $payload -DestinationPath $zipPath -Force

  Write-Host "[4/5] 生成发布包 SHA-256 清单..."
  $hashFile = Join-Path $releaseDir ("AIProxyHub-$ver-win64-SHA256SUMS.txt")
  $hashLines = @()
  foreach ($item in @($exePayload, $zipPath)) {
    if (Test-Path $item -PathType Leaf) {
      $hash = (Get-FileHash -Algorithm SHA256 $item).Hash.ToLowerInvariant()
      $hashLines += ("{0}  {1}" -f $hash, (Split-Path $item -Leaf))
    }
  }
  Set-Content -Path $hashFile -Value ($hashLines -join "`r`n") -Encoding utf8

  Write-Host "[5/5] 完成"
  Write-Host "EXE: $exePayload"
  Write-Host "ZIP: $zipPath"
  Write-Host "SHA256: $hashFile"
} finally {
  Pop-Location
}
