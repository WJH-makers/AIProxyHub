# Contributing to AIProxyHub

感谢贡献。AIProxyHub 的目标是做一个精简、可审计的 Windows 本地 AI 网关；贡献应优先改善可靠性、可解释性、安全边界、测试和维护工作流。

## 不接受的方向

不要提交或恢复以下能力：第三方账户批量注册、临时邮箱/OTP 自动读取、虚假身份、验证码或反机器人保护模拟、消费者 OAuth token 批量提取、凭据转售、账号农场或用于规避 provider 使用限制的轮换逻辑。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 本地检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q launcher.py launcher_monitor.py launcher_security.py launcher_settings.py
.\.venv\Scripts\ruff.exe check launcher.py launcher_monitor.py launcher_security.py launcher_settings.py --select E9,F821,F823,F811,F841
git diff --check
```

不要在测试中使用真实 provider token、真实账户、真实邮箱或真实 prompt。需要网络行为时使用本地 fake service，并为路径、权限、鉴权和异常退出补回归测试。

## Pull request 要求

- 使用 Conventional Commit 风格，并用中文说明主要变更。
- 描述用户可见变化、风险边界、测试命令和未解决限制。
- 新增 provider 配置或第三方组件时，补齐来源、版本和许可证说明。
- 改动凭据、远程管理、缓存隔离或发布流程时，必须增加对应测试或说明为什么无法测试。
- 不提交 `settings.json`、`data/`、`.env`、auth 文件、二进制 token 或本机日志。

Codex、其他 AI 工具或 IDE 可以辅助分析和生成补丁，但提交者必须理解、审阅并对最终变更负责。
