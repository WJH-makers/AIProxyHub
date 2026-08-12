# AIProxyHub 管理 API

默认地址：`http://127.0.0.1:9090`。管理端口被占用时，launcher 会选择空闲端口并在控制台输出实际地址，同时把实际端口以非敏感的纯文本写入应用数据目录中的 `launcher.port`。代理端口 `8317` 被占用时，启动代理也会自动切换并保存实际端口。脚本不要假定自动切换后仍是 9090。

管理 API 不是业务 provider API。管理面板默认只监听本机；管理面板远程监听要求显式 `--allow-remote`。业务网关远程监听还要在设置中显式开启 `allow_remote_proxy`，并配置独立且足够强的 `admin_api_key`、强客户端 Key 与网络隔离，不能复用客户端 `api_key`。

## GET

| Endpoint | 说明 |
|---|---|
| `/` | 本机管理面板 |
| `/api/settings` | 脱敏后的当前设置（本机面板或管理 Bearer） |
| `/api/status` | 代理、网关、缓存、限流和只读监控状态 |
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
$managementPort = 9090
$portFile = Join-Path $env:LOCALAPPDATA "AIProxyHub\launcher.port"
if (Test-Path -LiteralPath $portFile) {
    $managementPort = [int](Get-Content -LiteralPath $portFile -Raw).Trim()
}
$managementBase = "http://127.0.0.1:$managementPort"
Invoke-RestMethod "$managementBase/api/ext/status" -Headers $h
Invoke-RestMethod "$managementBase/api/ext/start_proxy" -Method Post -Headers $h
Invoke-RestMethod "$managementBase/api/ext/usage_summary" -Headers $h
```

如果管理端口发生自动切换，可读取当前应用数据根目录中的 `launcher.port`，也可以使用 launcher 启动时打印的实际 URL；需要固定端口时使用 `--port <端口> --strict-port`。

接口不会创建、删除、轮换或上传 provider 账户凭据。

## 网关限流状态

`/api/status` 的 `gateway.limits` 回报本地闸门的实时状态：

```json
{
  "rate_limit_rpm": 600,
  "max_concurrent_requests": 64,
  "scope": "per-client-credential",
  "tracked_clients": 2,
  "rate_limited": 0,
  "concurrency_active": 1,
  "concurrency_peak": 7,
  "concurrency_rejected": 0
}
```

客户端超出 RPM 时，网关返回 429 并带 `Retry-After`；在途请求达到全局上限时返回 503。两个上限在配置页调整，改动需重启代理生效。`tracked_clients` 只是凭据摘要的计数，不含明文 key。
