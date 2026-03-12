# AIProxyHub External API

Base URL: `http://localhost:9090`（默认 launcher 端口；若 9090 被占用，launcher 会自动选择空闲端口并在控制台输出最终 URL，请以实际输出为准）
Authentication: `Authorization: Bearer <api_key>`（在管理面板「配置」页点击“显示/复制”获取；首次启动会自动生成强随机 key）

## GET Endpoints

| Endpoint | Description |
|---|---|
| `/api/ext/status` | 代理/注册/autopilot/monitor 状态 |
| `/api/ext/quota` | 所有账号配额 (active/total/pct/below_threshold) |
| `/api/ext/usage_summary` | CPA 使用统计摘要（total_requests/total_tokens 等） |
| `/api/ext/logs` | 最近 200 条日志 |
| `/api/ext/accounts` | 已注册账号列表 |
| `/api/ext/monitor_status` | 自动监控是否运行 |
| `/api/ext/cache_stats` | 缓存池统计（命中/未命中/命中率/条目数等） |

## POST Endpoints

| Endpoint | Description |
|---|---|
| `/api/ext/register` | 启动注册 (按 settings 中 total_accounts/max_workers) |
| `/api/ext/stop_register` | 停止注册 |
| `/api/ext/start_proxy` | 启动 CPA 代理 |
| `/api/ext/stop_proxy` | 停止代理 |
| `/api/ext/restart_proxy` | 重启代理（stop -> start，用于应用配置变更） |
| `/api/ext/autopilot` | 一键全流程 (启动代理->清理->注册) |
| `/api/ext/stop_autopilot` | 停止全流程 |
| `/api/ext/cleanup` | 清理无效账号 |
| `/api/ext/start_monitor` | 启动自动监控 (每60s检查, <20%自动注册, 额度用完自动删除) |
| `/api/ext/stop_monitor` | 停止自动监控 |
| `/api/ext/cache_clear` | 清空缓存池 |

## Auto-Monitor

启动后每 60 秒执行:
1. 查询所有账号配额
2. 删除 `unavailable` 且 `usage_limit_reached` 的账号
3. 可用率 < 20% 且无注册任务时, 自动触发注册

## Examples

```bash
# 查配额
curl -s http://localhost:9090/api/ext/quota -H "Authorization: Bearer <api_key>"

# 注册
curl -s -X POST http://localhost:9090/api/ext/register -H "Authorization: Bearer <api_key>"

# 启动自动监控
curl -s -X POST http://localhost:9090/api/ext/start_monitor -H "Authorization: Bearer <api_key>"

# 查日志
curl -s http://localhost:9090/api/ext/logs -H "Authorization: Bearer <api_key>"

# 查使用统计摘要
curl -s http://localhost:9090/api/ext/usage_summary -H "Authorization: Bearer <api_key>"
```
