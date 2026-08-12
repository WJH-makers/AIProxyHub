# AIProxyHub 管理 API

默认地址：`http://127.0.0.1:9090`。管理端口被占用时，launcher 会选择空闲端口并在控制台输出实际地址；代理端口 `8317` 被占用时，启动代理也会自动切换并保存实际端口。

管理 API 不是业务 provider API。默认只监听本机；远程调用需要 `Authorization: Bearer <admin_api_key>`。若没有单独配置 `admin_api_key`，为兼容性会使用客户端 `api_key`，生产或局域网环境建议分离两者。

## GET

| Endpoint | 说明 |
|---|---|
| `/` | 本机管理面板 |
| `/api/settings` | 脱敏后的当前设置（本机面板或管理 Bearer） |
| `/api/status` | 代理、网关、缓存和只读监控状态 |
| `/api/usage_summary` | 上游管理 API 的用量摘要 |
| `/api/logs` | 最近日志（已做基础脱敏） |
| `/api/ext/status` | Bearer 鉴权的状态接口 |
| `/api/ext/usage_summary` | Bearer 鉴权的用量摘要 |
| `/api/ext/logs` | Bearer 鉴权的日志接口 |
| `/api/ext/monitor_status` | Bearer 鉴权的只读监控状态 |
| `/api/ext/cache_stats` | Bearer 鉴权的缓存统计 |

## POST

Bearer 鉴权的外部控制接口：

| Endpoint | 说明 |
|---|---|
| `/api/ext/start_proxy` | 启动代理 |
| `/api/ext/stop_proxy` | 停止代理 |
| `/api/ext/restart_proxy` | 重启代理使配置生效 |
| `/api/ext/start_monitor` | 启动只读健康监控 |
| `/api/ext/stop_monitor` | 停止监控 |
| `/api/ext/trigger_monitor` | 立即执行一次健康检查 |
| `/api/ext/cache_clear` | 清空内存缓存 |

本机管理面板使用启动时生成的 `X-AIProxyHub-Token` 保护写操作。`/api/save`、`/api/clear_logs`、`/api/secure_cleanup_outputs` 等面板操作不应被脚本化暴露到远程网络。

## 示例

```powershell
$h = @{ Authorization = "Bearer <admin_api_key>" }
Invoke-RestMethod http://127.0.0.1:9090/api/ext/status -Headers $h
Invoke-RestMethod http://127.0.0.1:9090/api/ext/start_proxy -Method Post -Headers $h
Invoke-RestMethod http://127.0.0.1:9090/api/ext/usage_summary -Headers $h
```

接口不会创建、删除、轮换或上传 provider 账户凭据。
