# AIProxyHub

AIProxyHub 是一个面向 Windows 的本地优先 AI 网关管理器：负责启动和管理用户已经授权的 provider、提供 OpenAI-compatible 转发接口、可选的请求级缓存、用量摘要、健康监控和可审计的运行日志。

它不是账号生成器，也不是订阅共享或限额规避工具。AIProxyHub 不创建第三方账户、不使用临时邮箱或 OTP、不模拟反滥用挑战、不生成虚假身份、不批量提取消费者 OAuth token，也不自动轮换账户来规避 provider 使用限制。用户必须自行提供有权使用的 provider 配置，并遵守适用的服务协议。

## 主要能力

- Windows 管理面板：启动、停止、重启代理，查看状态、日志和用量摘要。
- CLIProxyAPI 生命周期管理：运行时配置写入系统临时目录，敏感设置在 Windows 上使用 DPAPI 保护。
- OpenAI-compatible gateway：可选支持 `/v1/responses` 与 `/v1/chat/completions` 的请求级缓存、singleflight、大小/数量/内存上限和 stale 策略。
- 缓存隔离：缓存 key 始终包含客户端 `Authorization` 边界，并保留项目/组织/租户 header 维度；不提供跨 API Key 共享缓存。
- 只读健康监控：只检查本地代理可达性和用量摘要，不删除、创建、上传或轮换 provider 凭据。
- 发布冒烟与自动化检查：测试、编译、Focused lint、秘密扫描和发布前构建检查。

## 快速开始

源码运行需要 Python 3.10+：

```powershell
.启动.bat
```

或者：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launcher.py --no-browser
```

默认管理面板只监听 `127.0.0.1`。EXE 版默认将设置和运行数据保存到 `%LOCALAPPDATA%\AIProxyHub`；可以使用 `AIPROXYHUB_HOME` 指定隔离目录，或在 EXE 同目录创建 `AIProxyHub.portable` 使用便携模式。

首次启动会生成随机的管理密码和客户端 API Key。设置文件中的敏感字段由 Windows DPAPI 保护；日志和页面不会默认回显密钥。远程监听必须显式使用 `--allow-remote`，并设置独立且足够强的 `admin_api_key`，同时做好网络隔离。

## 客户端接入

启动代理后，使用以下 OpenAI-compatible 地址：

```text
Base URL: http://127.0.0.1:8317/v1（如果 8317 被占用，启动时会自动切换到可用端口；以面板/日志中的实际端口为准）
API Key: 你在配置中设置的客户端 API Key
```

适用于 Codex CLI、Cursor、Cline、Continue、OpenCode 以及其他支持 OpenAI-compatible API 的客户端；实际可用模型和参数取决于用户配置的上游 provider。AIProxyHub 不授予任何 provider 的访问权，也不代表上游服务商。

## 构建发布包

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\get-cliproxyapi.ps1 -Tag <CLIProxyAPI-tag> -ExpectedSha256 <CLIProxyAPI-exe-sha256>
powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1
```

产物位于 `dist\` 和 `release\`。构建会携带 CLIProxyAPI 的第三方许可声明；发布前应固定上游版本并核对 SHA-256。安装包构建：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-installer-nsis.ps1
```

发布冒烟：

```powershell
.\.venv\Scripts\python.exe .\scripts\smoke-release-exe.py --zip .\release\AIProxyHub-<version>-win64.zip --timeout 240
```

## 项目边界与安全

- 只支持 BYOK/BYOA：用户主动配置自己有权使用的 provider/API credential。
- 不支持消费者账号批量创建、临时邮箱收码、验证码提取、反机器人挑战模拟、虚假注册信息、账号农场、凭据转售或 quota-evasion failover。
- 不要将 `settings.json`、`data/`、`.env`、provider auth 文件或任何 token 提交到仓库。
- 缓存可能包含 prompt/response 内容；默认关闭，启用前请确认本机和团队的数据边界。
- 默认代理端口 8317 被其他本机服务占用时，AIProxyHub 会自动选择并保存一个可用端口；管理面板仍会显示实际地址。
- 本项目本身不运营托管服务，默认不收集遥测；数据保留和 provider 侧处理取决于用户配置。详见 [PRIVACY.md](PRIVACY.md) 和 [SECURITY.md](SECURITY.md)。

## 维护与工具辅助署名

主要维护者：`WJH-makers`。

部分代码维护、测试设计、文档整理和审查工作可能使用 OpenAI Codex 辅助完成；所有变更由主要维护者审阅、验证并负责。Codex 是开发工具，不是本项目的版权持有人、维护者、共同作者或背书方。

AIProxyHub 是独立开源项目，不隶属于、未获 OpenAI 或其他 provider 赞助、认可或背书。OpenAI、ChatGPT、Codex、GPT 及其他名称归各自权利人所有。

## 第三方组件

本项目集成或分发 CLIProxyAPI 及 Python 依赖。来源、版本、许可证和分发边界见 [NOTICE.md](NOTICE.md) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。贡献代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
