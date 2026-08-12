# Privacy and local data handling

AIProxyHub 本身不运营托管服务，默认不收集遥测。它会把下列数据保存在本机或按用户配置转发给已配置的上游 provider：

- `settings.json`：非敏感设置和由 Windows DPAPI 保护的本地密钥字段；
- 运行时临时配置：启动上游代理时写入按应用根目录隔离的 `%TEMP%\AIProxyHub\<namespace>`，退出后尽量清理；
- `launcher.port`：应用数据根目录中的当前管理端口文本，不包含凭据；
- 日志：生命周期、错误、健康和用量摘要；日志经过基础脱敏，但不应假定能识别所有秘密格式；
- 内存缓存：开启后可能包含请求和响应内容，始终按客户端凭据隔离；
- 上游 provider auth/config：由 CLIProxyAPI 或用户配置管理，AIProxyHub 不拥有其授权权利。

## 控制和删除

关闭代理后，可在管理面板执行“清理过期运行时文件”，或手动删除对应 namespace 下不再使用的运行时配置。清空日志会清除当前 launcher 进程内的日志缓冲；上游 provider 的历史记录、账单、日志和数据保留由上游服务商决定。

启用缓存或远程管理前，请确认本机账户、团队成员、备份工具和 provider 之间的数据边界。不要把私人 prompt、response、API key、refresh token 或 auth 文件提交到 Git。
