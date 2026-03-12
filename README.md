# AIProxyHub（二次修改版）

AIProxyHub 是一个面向 Windows 的“一键整合”工具：把 **CLIProxyAPI**（OpenAI 兼容代理） + **ChatGPT 批量注册** + **本地管理面板** + **透明网关缓存池** 组合成一个可以直接分发的 EXE/安装包。

> 本仓库为“二次修改版”（在原始整合脚本的基础上做了安全与可发布性增强）。  
> 重要：请勿将 `settings.json` / `data/` 等本机敏感信息提交到公开仓库。

## 功能概览

- **管理面板（launcher）**：仪表盘 / 配置 / 批量注册 / 账号管理 / 运行日志
- **CLIProxyAPI 启动/停止/重启**：自动生成运行时配置（临时目录），并聚合日志
- **CPA 控制面板**：通过 `http://localhost:<proxy_port>/management.html` 访问
- **使用统计汇总**：面板展示总请求数/总 Tokens（来自 CPA 管理 API 的摘要）
- **透明网关 + 缓存池**：
  - 覆盖 `/v1/responses` 与 `/v1/chat/completions`（非 stream）
  - 支持 **跨 API Key 共享缓存**（仅可信团队场景，默认关闭）
  - singleflight 防止 cache stampede
- **EXE 发布包冒烟**：验证 EXE 可运行、鉴权、代理、usage、缓存命中，并支持 20 RPS（缓存 HIT 路径）压测

## 数据保存（EXE/安装版）

EXE 版默认把配置与数据写入：`%LOCALAPPDATA%\\AIProxyHub`（可用环境变量 `AIPROXYHUB_HOME` 覆盖）。  
如需便携模式（数据与 EXE 同目录），请在 `AIProxyHub.exe` 同目录创建空文件：`AIProxyHub.portable`。

## 快速开始（源码运行）

双击 `启动.bat`（会自动创建/复用 `.venv` 并安装依赖），随后浏览器打开管理面板。

## 构建发布包（zip）

```powershell
cd E:\AIProxyHub
powershell -ExecutionPolicy Bypass -File .\scripts\get-cliproxyapi.ps1   # 如已存在可跳过
powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1
```

输出：
- `dist\AIProxyHub.exe`
- `release\AIProxyHub-<version>-win64.zip`

## 构建安装包（setup.exe，带桌面图标）

```powershell
cd E:\AIProxyHub
powershell -ExecutionPolicy Bypass -File .\scripts\build-installer-nsis.ps1
```

输出：
- `release\AIProxyHub-<version>-setup-win64.exe`

说明：安装包默认安装到 `%LOCALAPPDATA%\\Programs\\AIProxyHub`，并创建桌面/开始菜单快捷方式；卸载默认不删除 `%LOCALAPPDATA%\\AIProxyHub` 用户数据。

## 发布包冒烟验证（推荐）

```powershell
cd E:\AIProxyHub
E:\AIProxyHub\.venv\Scripts\python.exe .\scripts\smoke-release-exe.py --require-responses --timeout 240
```

并发/缓存回归（示例：20 req/s，缓存 HIT 路径）：

```powershell
E:\AIProxyHub\.venv\Scripts\python.exe .\scripts\smoke-release-exe.py `
  --require-responses `
  --test-cache `
  --share-cache-across-api-keys `
  --load-rps 20 `
  --load-seconds 5 `
  --timeout 240
```

## 致谢（原创作者）

- **CLIProxyAPI**：由 `router-for-me` 开源维护（本项目用于启动其 Windows 版本并集成管理/注册流程）  
  https://github.com/router-for-me/CLIProxyAPI

如果本项目还基于其它上游仓库/作者，请在此补充原始来源与 License（公开发布前强烈建议做一次合规核对）。
