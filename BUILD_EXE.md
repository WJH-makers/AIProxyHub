# AIProxyHub 打包为 Windows EXE（发布包）

本项目默认以源码方式运行（`启动.bat` 自动创建 `.venv` 并安装依赖）。如果你希望分发为“下载即用”的 EXE，可使用 PyInstaller 打包。

> 安全提示：发布包 **不应** 包含 `settings.json` / `data/` 等本机私密文件（哪怕已 DPAPI 加密，也会造成误分发风险）。

## 1) 前置条件

- Windows x64
- 已在项目目录生成虚拟环境：`E:\AIProxyHub\.venv\`
  - 最简单方式：先双击运行一次 `启动.bat`（会自动创建/复用 `.venv`）
- 项目根目录存在第三方组件：`cli-proxy-api.exe`（CLIProxyAPI Windows 版本）
  - 若缺失，可一键下载：

    ```powershell
    powershell -ExecutionPolicy Bypass -File .\scripts\get-cliproxyapi.ps1
    ```

## 2) 一键打包（推荐）

在 PowerShell 里运行：

```powershell
cd E:\AIProxyHub

# 生成单文件 EXE + zip 发布包（默认不带控制台窗口）
powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1
```

输出：
- `dist\AIProxyHub.exe`：可执行文件
- `release\AIProxyHub-<version>-win64.zip`：发布压缩包（包含 exe + 使用指南 + API 说明）

## 2.5) 生成“安装包（setup.exe，带桌面图标）”（NSIS）

如果你希望像 QQ/微信 一样“安装到本机 + 桌面图标 + 开始菜单 + 卸载入口”，可用 NSIS 生成安装包：

```powershell
cd E:\AIProxyHub

powershell -ExecutionPolicy Bypass -File .\scripts\build-installer-nsis.ps1
```

输出：
- `release\AIProxyHub-<version>-setup-win64.exe`

说明：
- 脚本会把 NSIS 解压到本项目 `.tools/` 下（不需要全局安装）。
- 安装包默认安装到：`%LOCALAPPDATA%\Programs\AIProxyHub`（无需管理员权限）。
- 用户数据默认保存到：`%LOCALAPPDATA%\AIProxyHub`（卸载默认不删除用户数据）。

## 3) 可选参数

- 生成目录版（启动更快，但是一个文件夹）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1 -OneDir
```

- 保留控制台窗口（调试更方便）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1 -Console
```

## 4) 运行说明（EXE 版）

- 直接双击 `AIProxyHub.exe` 即可启动面板（默认端口 9090；被占用会自动换端口）
- 配置文件与输出目录：
  - 默认写入 `%LOCALAPPDATA%\\AIProxyHub`（可通过 `AIPROXYHUB_HOME` 环境变量覆盖）
  - 如需便携模式（数据与 EXE 同目录）：在 EXE 同目录创建空文件 `AIProxyHub.portable`

## 5) 发布包冒烟验证（推荐）

在开发机/CI 上验证“zip 内的 EXE 真的能跑起来”，可以使用脚本：

```powershell
cd E:\AIProxyHub

# 默认选择 release/ 下最新的 AIProxyHub-*-win64.zip
E:\AIProxyHub\.venv\Scripts\python.exe .\scripts\smoke-release-exe.py --require-responses
```

如需验证“缓存池 + 共享缓存 + 20 RPS 并发能力”（推荐用于团队高并发场景的回归），可运行：

```powershell
E:\AIProxyHub\.venv\Scripts\python.exe .\scripts\smoke-release-exe.py `
  --require-responses `
  --test-cache `
  --share-cache-across-api-keys `
  --load-rps 20 `
  --load-seconds 5 `
  --timeout 240
```

说明：
- 脚本会把 zip 解压到临时目录，并使用隔离的 `AIPROXYHUB_HOME` 启动 EXE
- onefile EXE 启动时需要解包到临时目录，首次启动在部分机器/杀软环境下可能较慢；可用 `--timeout 180` / `--timeout 300` 放宽等待时间
- 会验证：launcher ready、ext API 鉴权、start/stop proxy、/v1/models、/v1/responses（可选强制）、CPA /v0/management/usage 累积
- 若本机没有任何可用账号认证文件（`~/.cli-proxy-api/`），`/v1/responses` 可能会失败；此时可不加 `--require-responses`
