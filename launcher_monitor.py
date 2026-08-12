"""Read-only health monitor for the local proxy and gateway.

The monitor deliberately never creates, deletes, rotates, or uploads provider
credentials. It only samples service reachability and the upstream usage
summary exposed by the already configured local proxy.
"""

import threading
import time

_monitor_running = False
_monitor_cancel = threading.Event()
_monitor_wake = threading.Event()
_monitor_stats_lock = threading.Lock()
_monitor_stats = {
    "last_run_ts": 0.0,
    "last_ok": False,
    "last_msg": "",
    "last_proxy_reachable": False,
    "last_usage_ok": False,
}


def _monitor_get_cfg() -> dict:
    from launcher import load_settings

    settings = load_settings()
    interval = int(settings.get("monitor_interval_seconds", 60) or 60)
    return {
        "enabled": bool(settings.get("monitor_enabled", False)),
        "interval_seconds": max(10, min(interval, 24 * 3600)),
        "mode": "read-only-health-and-usage",
    }


def _monitor_loop():
    global _monitor_running
    from launcher import DEFAULT_PROXY_PORT, _coerce_port, _proxy_reachable, _query_usage_summary, load_settings, log

    log("[MONITOR] 只读健康监控已启动")
    previous = None
    while not _monitor_cancel.is_set():
        cfg = _monitor_get_cfg()
        try:
            settings = load_settings()
            port = _coerce_port(settings.get("proxy_port"), DEFAULT_PROXY_PORT)
            proxy_ok = bool(_proxy_reachable(port))
            usage = _query_usage_summary() if proxy_ok else {"ok": False, "msg": "代理不可达"}
            ok = proxy_ok and bool(usage.get("ok", False))
            message = "" if ok else str(usage.get("msg", "代理不可达") or "代理不可达")
            current = (proxy_ok, bool(usage.get("ok", False)), message)
            with _monitor_stats_lock:
                _monitor_stats.update(
                    {
                        "last_run_ts": time.time(),
                        "last_ok": ok,
                        "last_msg": message,
                        "last_proxy_reachable": proxy_ok,
                        "last_usage_ok": bool(usage.get("ok", False)),
                    }
                )
            if current != previous:
                log(f"[MONITOR] 健康检查：代理={'可达' if proxy_ok else '不可达'}，用量摘要={'可读' if usage.get('ok') else '不可用'}")
                previous = current
        except Exception as exc:
            with _monitor_stats_lock:
                _monitor_stats.update(
                    {
                        "last_run_ts": time.time(),
                        "last_ok": False,
                        "last_msg": str(exc),
                        "last_proxy_reachable": False,
                        "last_usage_ok": False,
                    }
                )
            log(f"[MONITOR] 健康检查异常：{exc}")

        _monitor_wake.clear()
        interval = int(cfg.get("interval_seconds", 60) or 60)
        for _ in range(interval):
            if _monitor_cancel.is_set():
                break
            if _monitor_wake.wait(timeout=1.0):
                break

    _monitor_running = False
    log("[MONITOR] 只读健康监控已停止")


def start_monitor():
    global _monitor_running
    from launcher import _lock

    with _lock:
        if _monitor_running:
            return {"ok": False, "msg": "监控已在运行中"}
        _monitor_cancel.clear()
        _monitor_running = True
        threading.Thread(target=_monitor_loop, daemon=True, name="AIProxyHubHealthMonitor").start()
    return {"ok": True, "msg": "只读健康监控已启动"}


def stop_monitor():
    global _monitor_running
    from launcher import log

    _monitor_cancel.set()
    _monitor_wake.set()
    _monitor_running = False
    log("[MONITOR] 已请求停止只读健康监控")
    return {"ok": True, "msg": "监控已停止"}


def get_monitor_status():
    cfg = _monitor_get_cfg()
    with _monitor_stats_lock:
        stats = dict(_monitor_stats)
    return {"ok": True, "running": _monitor_running, "cfg": cfg, "stats": stats}


def trigger_monitor_now():
    if not _monitor_running:
        return {"ok": False, "msg": "monitor not running"}
    _monitor_wake.set()
    return {"ok": True, "msg": "immediate health check triggered"}
