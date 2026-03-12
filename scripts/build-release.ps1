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
  Write-Host "[1/4] 安装打包依赖（pyinstaller）..."
  & $py -m pip install -q pyinstaller | Out-Null

  $cliProxy = Join-Path $root "cli-proxy-api.exe"
  if (-not (Test-Path $cliProxy)) {
    throw "未找到 cli-proxy-api.exe：$cliProxy。它属于第三方组件（CLIProxyAPI Windows 版本）。请先运行：powershell -ExecutionPolicy Bypass -File .\\scripts\\get-cliproxyapi.ps1（或手动下载并放到项目根目录）。"
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

  Write-Host "[2/4] PyInstaller 打包..."
  $args = @("--noconfirm", "--clean", "--name", "AIProxyHub", "--add-binary", "cli-proxy-api.exe;.")
  if ($OneDir) { $args += "--onedir" } else { $args += "--onefile" }
  if (-not $Console) { $args += "--noconsole" }
  $args += "launcher.py"

  & $py -m PyInstaller @args

  if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 失败，退出码 $LASTEXITCODE"
  }

  Write-Host "[3/4] 生成 zip 发布包（不包含 settings.json/data 等本机私密文件）..."
  $ver = (& $py -c "import launcher; print(getattr(launcher,'APP_VERSION','unknown'))").Trim()
  $releaseDir = Join-Path $root "release"
  New-Item -ItemType Directory -Force $releaseDir | Out-Null

  $zipPath = Join-Path $releaseDir ("AIProxyHub-$ver-win64.zip")
  if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

  $exePayload = if ($OneDir) { (Join-Path $distDir "AIProxyHub") } else { (Join-Path $distDir "AIProxyHub.exe") }

  $payload = @(
    $exePayload,
    (Join-Path $root "使用指南.md"),
    (Join-Path $root "API.md")
  ) | Where-Object { Test-Path $_ }

  Compress-Archive -Path $payload -DestinationPath $zipPath -Force

  Write-Host "[4/4] 完成"
  Write-Host "EXE: $exePayload"
  Write-Host "ZIP: $zipPath"
} finally {
  Pop-Location
}
