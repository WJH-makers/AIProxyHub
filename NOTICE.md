# NOTICE

AIProxyHub 的原创代码由 `WJH-makers` 维护，根目录 `LICENSE` 采用 MIT License。

AIProxyHub 是独立的整合与管理项目。它不声称拥有下列第三方组件的版权，也不代表或背书任何上游 provider。发布包应同时携带与实际分发内容相匹配的第三方许可文本。

## CLIProxyAPI

- 来源：https://github.com/router-for-me/CLIProxyAPI
- 固定版本：v7.2.129，exe SHA-256 `45192C808339F17778574616FBDBAD8BB1FA1239AD4EFF90E87C221B4DDE50FE`（见 `cli-proxy-api.provenance.json`）
- 用途：提供用户已授权 provider 的 OpenAI-compatible 代理和管理接口；AIProxyHub 只负责本地启动、配置和管理面板整合。
- 许可证：上游 MIT License。发布包含其可执行文件时，必须保留上游版权和许可声明。

## Python 构建工具

- `PyInstaller`：GPL with bootloader exception；用于构建本项目可执行文件时应遵守其例外及其他依赖的许可要求。
- `zstandard`：BSD 3-Clause；用于解压和归一化客户端 zstd 压缩请求。

准确的版本与许可证清单见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；许可证正文位于 `licenses/`，并随发布包分发。如果发布包增加或替换组件，必须同步更新清单，不得留下“以后再补”的未完成声明。
