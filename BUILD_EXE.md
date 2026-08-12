# AIProxyHub 打包为 Windows EXE（发布包）

本项目默认以源码方式运行（`启动.bat` 自动创建 `.venv` 并安装依赖）。如果你希望分发为“下载即用”的 EXE，可使用 PyInstaller 打包。

> 安全提示：发布包 **不应** 包含 `settings.json` / `data/` 等本机私密文件（哪怕已 DPAPI 加密，也会造成误分发风险）。

## 1) 前置条件

- Windows x64
- 已在项目目录生成虚拟环境：`.venv\`
  - 最简单方式：先双击运行一次 `启动.bat`（会自动创建/复用 `.venv`）
- 项目根目录存在第三方组件：`cli-proxy-api.exe`（CLIProxyAPI Windows 版本）
  - 若缺失，可一键下载：

    ```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\get-cliproxyapi.ps1 -Tag <CLIProxyAPI-tag> -ExpectedSha256 <CLIProxyAPI-exe-sha256>
    ```

## 2) 一键打包（推荐）

在 PowerShell 里运行：

```powershell
cd <repo-root>

# 生成单文件 EXE + zip 发布包（默认不带控制台窗口）
powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1
```

输出：
- `dist\AIProxyHub.exe`：可执行文件
- `release\AIProxyHub-<version>-win64.zip`：发布压缩包（包含 exe、使用指南、API、安全/隐私文档、第三方许可证正文和 SHA-256 清单）

## 2.5) 生成“安装包（setup.exe，带桌面图标）”（NSIS）

如果你希望像 QQ/微信 一样“安装到本机 + 桌面图标 + 开始菜单 + 卸载入口”，可用 NSIS 生成安装包：

```powershell
cd <repo-root>

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
cd <repo-root>

# 默认选择 release/ 下最新的 AIProxyHub-*-win64.zip
.\.venv\Scripts\python.exe .\scripts\smoke-release-exe.py --zip .\release\AIProxyHub-<version>-win64.zip
```

说明：
- 脚本会把 zip 解压到临时目录，并使用隔离的 `AIPROXYHUB_HOME` 启动 EXE。
- 它验证 launcher ready、敏感设置不回显、外部管理 API 未鉴权时返回 401，以及状态中不存在已删除的账号注册工作流。
- 该 smoke test 不联系 provider，不使用真实凭据；真正的 provider 兼容性应在本地授权测试环境单独验证。
