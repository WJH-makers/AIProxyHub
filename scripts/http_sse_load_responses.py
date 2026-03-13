#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
HTTP SSE 压测脚本：对 AIProxyHub 网关的 /v1/responses（stream=true, text/event-stream）进行并发/吞吐/稳定性测试。

用途：
- 与 scripts/ws_load_responses.py 配套，用同一组参数对比 **HTTP SSE vs WebSocket** 的端到端耗时分布。
- 重点衡量：response.completed 的端到端延迟（不等同于首 token 延迟）。

认证来源（按优先级）：
1) 环境变量 AIPH_API_KEY
2) 从项目根目录 launcher.load_settings() 读取 settings.json 解密后的 api_key

示例：
  E:/AIProxyHub/.venv/Scripts/python.exe E:/AIProxyHub/scripts/http_sse_load_responses.py --concurrency 30 --requests-per-conn 2

注意：
- 本脚本不会回显任何 API Key 明文。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Tuple


# 允许从 scripts/ 目录运行并导入项目根目录的 launcher.py
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import launcher  # type: ignore  # noqa: E402
except Exception:
    launcher = None  # type: ignore


def _load_token() -> str:
    env = str(os.getenv("AIPH_API_KEY", "") or "").strip()
    if env:
        return env
    if launcher is not None:
        try:
            s = launcher.load_settings()
            t = str(s.get("admin_api_key") or s.get("api_key") or "").strip()
            if t:
                return t
        except Exception:
            pass
    return ""


def _ensure_proxy_running(*, launcher_host: str, launcher_port: int, token: str, timeout_s: int) -> Tuple[bool, str]:
    url = f"http://{str(launcher_host)}:{int(launcher_port)}/api/ext/start_proxy"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {str(token)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as r:
            raw = r.read() or b""
            try:
                obj = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                obj = {}
            ok = bool(isinstance(obj, dict) and obj.get("ok") is True)
            msg = str(obj.get("msg") or "")
            return ok, msg
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception:
            obj = {}
        msg = str(obj.get("msg") or raw.decode("utf-8", errors="replace")[:200])
        return False, f"http={getattr(e, 'code', 0)} {msg}"
    except Exception as e:
        return False, str(e)


def _iter_sse_data_strings(resp) -> "List[str]":
    """
    把 text/event-stream 解析为 data 字段字符串序列。

    - 以空行分隔 event block（\\n\\n 或 \\r\\n\\r\\n）
    - 每个 block 提取所有 data: 行并拼接
    """
    buf = b""
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        buf += chunk

        while True:
            i_lf = buf.find(b"\n\n")
            i_crlf = buf.find(b"\r\n\r\n")
            if i_lf == -1 and i_crlf == -1:
                break
            if i_crlf != -1 and (i_lf == -1 or i_crlf < i_lf):
                block = buf[:i_crlf]
                buf = buf[i_crlf + 4 :]
            else:
                block = buf[:i_lf]
                buf = buf[i_lf + 2 :]

            try:
                text = block.decode("utf-8", errors="replace")
            except Exception:
                continue
            data_lines = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            data = "\n".join(data_lines).strip()
            if data:
                yield data


@dataclass
class RequestResult:
    ok: bool
    latency_s: float
    bytes_in: int
    error: str


def _one_request(
    conn: http.client.HTTPConnection,
    *,
    host: str,
    model: str,
    input_text: str,
    timeout_s: int,
    token: str,
    cache_mode: str,
    cache_group: str,
) -> RequestResult:
    req_obj = {
        "model": model,
        "input": input_text,
        "store": False,
        "stream": True,
    }
    body = json.dumps(req_obj, ensure_ascii=False).encode("utf-8")
    headers = {
        "Host": host,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Content-Length": str(len(body)),
    }
    cache_mode_norm = str(cache_mode or "").strip().lower()
    if cache_mode_norm and cache_mode_norm != "on":
        headers["X-AIProxyHub-Cache"] = cache_mode_norm
    cache_group_norm = str(cache_group or "").strip()
    if cache_group_norm:
        headers["X-AIProxyHub-Cache-Group"] = cache_group_norm

    started = time.time()
    bytes_in = 0
    try:
        conn.request("POST", "/v1/responses", body=body, headers=headers)
        resp = conn.getresponse()
    except Exception as e:
        return RequestResult(ok=False, latency_s=0.0, bytes_in=0, error=f"request_error:{type(e).__name__}:{e}")

    try:
        status = int(getattr(resp, "status", 0) or 0)
        ct = ""
        try:
            for hk, hv in (resp.getheaders() or []):
                if str(hk).lower() == "content-type":
                    ct = str(hv)
                    break
        except Exception:
            ct = ""

        if status != 200 or not str(ct).lower().startswith("text/event-stream"):
            raw = b""
            try:
                raw = resp.read() or b""
            except Exception:
                raw = b""
            bytes_in += len(raw)
            return RequestResult(
                ok=False,
                latency_s=time.time() - started,
                bytes_in=bytes_in,
                error=f"bad_response:http={status} ct={ct} body={raw[:160].decode('utf-8', errors='replace').replace('\\n',' ')}",
            )

        for data in _iter_sse_data_strings(resp):
            bytes_in += len(data.encode("utf-8", errors="replace"))
            try:
                evt = json.loads(data)
            except Exception:
                continue
            if not isinstance(evt, dict):
                continue
            t = str(evt.get("type") or "")
            if t == "error":
                err = evt.get("error") if isinstance(evt.get("error"), dict) else {}
                status2 = err.get("status") if isinstance(err, dict) else None
                msg_text = err.get("message") if isinstance(err, dict) else None
                msg_short = str(msg_text or "")[:160].replace("\n", " ")
                return RequestResult(
                    ok=False,
                    latency_s=time.time() - started,
                    bytes_in=bytes_in,
                    error=f"error_event_status={status2} msg={msg_short}",
                )
            if t == "response.failed":
                return RequestResult(ok=False, latency_s=time.time() - started, bytes_in=bytes_in, error="response_failed")
            if t == "response.completed":
                return RequestResult(ok=True, latency_s=time.time() - started, bytes_in=bytes_in, error="")

        return RequestResult(ok=False, latency_s=time.time() - started, bytes_in=bytes_in, error="eof_before_completed")
    finally:
        try:
            resp.close()
        except Exception:
            pass


@dataclass
class WorkerResult:
    ok: int
    total: int
    latencies_s: List[float]
    bytes_in: int
    errors: Dict[str, int]


def _worker(
    *,
    wid: int,
    host: str,
    port: int,
    model: str,
    input_text: str,
    requests_per_conn: int,
    timeout_s: int,
    sleep_ms: int,
    token: str,
    cache_mode: str,
    cache_group: str,
    verbose: bool,
) -> WorkerResult:
    ok = 0
    total = 0
    latencies: List[float] = []
    bytes_in = 0
    errors: Dict[str, int] = {}

    addr = f"{host}:{int(port)}"
    conn: http.client.HTTPConnection | None = None

    def _ensure_conn() -> http.client.HTTPConnection:
        nonlocal conn
        if conn is None:
            conn = http.client.HTTPConnection(host, int(port), timeout=float(timeout_s))
        return conn

    for i in range(int(requests_per_conn)):
        total += 1
        try:
            c = _ensure_conn()
            r = _one_request(
                c,
                host=addr,
                model=model,
                input_text=input_text,
                timeout_s=timeout_s,
                token=token,
                cache_mode=cache_mode,
                cache_group=cache_group,
            )
        except Exception as e:
            r = RequestResult(ok=False, latency_s=0.0, bytes_in=0, error=f"exception:{type(e).__name__}:{e}")
            # 连接可能已坏，清掉重建
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None

        bytes_in += int(r.bytes_in or 0)
        if r.ok:
            ok += 1
            latencies.append(float(r.latency_s))
            if verbose:
                print(f"[w{wid}] req#{i+1} ok latency_s={r.latency_s:.3f} bytes_in={r.bytes_in}")
        else:
            errors[r.error] = errors.get(r.error, 0) + 1
            if verbose:
                print(f"[w{wid}] req#{i+1} FAIL err={r.error}")
            # 失败后倾向于重建连接（避免 keep-alive 处于半开状态）
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None

        if sleep_ms and sleep_ms > 0:
            time.sleep(float(sleep_ms) / 1000.0)

    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass

    return WorkerResult(ok=ok, total=total, latencies_s=latencies, bytes_in=bytes_in, errors=errors)


def _pct(v: float) -> str:
    return f"{(float(v) * 100.0):.2f}%"


def _quantile(xs: List[float], q: float) -> float:
    if not xs:
        return 0.0
    xs2 = sorted(xs)
    if q <= 0:
        return float(xs2[0])
    if q >= 1:
        return float(xs2[-1])
    pos = (len(xs2) - 1) * float(q)
    lo = int(pos)
    hi = min(lo + 1, len(xs2) - 1)
    if hi == lo:
        return float(xs2[lo])
    frac = pos - lo
    return float(xs2[lo]) * (1.0 - frac) + float(xs2[hi]) * frac


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--launcher-host", default=os.getenv("AIPH_LAUNCHER_HOST", "127.0.0.1"))
    ap.add_argument("--launcher-port", type=int, default=int(os.getenv("AIPH_LAUNCHER_PORT", "9090")))
    ap.add_argument("--ensure-proxy", action="store_true", help="测试前尝试通过 External API 启动代理（幂等）")
    ap.add_argument("--host", default=os.getenv("AIPH_HTTP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("AIPH_HTTP_PORT", "8317")))
    ap.add_argument("--model", default=os.getenv("AIPH_HTTP_MODEL", "gpt-5.2-xhigh"))
    ap.add_argument("--input", dest="input_text", default=os.getenv("AIPH_HTTP_INPUT", "ping"))
    ap.add_argument(
        "--cache-mode",
        choices=["on", "bypass", "refresh", "no-store"],
        default=os.getenv("AIPH_HTTP_CACHE_MODE", "bypass"),
        help="缓存控制：on=默认；bypass=绕过读写；refresh=绕过读但允许写；no-store=不写但允许读",
    )
    ap.add_argument("--cache-group", default=os.getenv("AIPH_HTTP_CACHE_GROUP", "loadtest"))
    ap.add_argument("--concurrency", type=int, default=int(os.getenv("AIPH_HTTP_CONCURRENCY", "10")))
    ap.add_argument("--requests-per-conn", type=int, default=int(os.getenv("AIPH_HTTP_REQUESTS_PER_CONN", "3")))
    ap.add_argument("--timeout-s", type=int, default=int(os.getenv("AIPH_HTTP_TIMEOUT_S", "60")))
    ap.add_argument("--sleep-ms", type=int, default=int(os.getenv("AIPH_HTTP_SLEEP_MS", "0")))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    token = _load_token()
    if not token:
        print("缺少 API Key：请设置环境变量 AIPH_API_KEY 或在 AIProxyHub UI 配置 api_key（本脚本不会回显密钥）。")
        return 2

    if bool(args.ensure_proxy):
        ok, msg = _ensure_proxy_running(
            launcher_host=str(args.launcher_host),
            launcher_port=int(args.launcher_port),
            token=token,
            timeout_s=min(120, int(args.timeout_s)),
        )
        print("ensure_proxy.ok=", ok)
        print("ensure_proxy.msg=", msg)
        if not ok:
            return 2

    host = str(args.host)
    port = int(args.port)
    concurrency = max(1, int(args.concurrency or 1))
    rpc = max(1, int(args.requests_per_conn or 1))

    print("target.http_sse=", f"http://{host}:{port}/v1/responses (stream=true)")
    print("concurrency=", concurrency)
    print("requests_per_conn=", rpc)
    print("total_requests=", concurrency * rpc)
    print("timeout_s=", int(args.timeout_s))
    print("sleep_ms=", int(args.sleep_ms))
    print("model=", str(args.model))
    print("input_len=", len(str(args.input_text or "")))
    print("cache_mode=", str(args.cache_mode))
    print("cache_group=", str(args.cache_group))
    print(
        "auth.source=",
        "env:AIPH_API_KEY" if str(os.getenv("AIPH_API_KEY", "") or "").strip() else "settings.json(api_key)",
    )

    t0 = time.time()
    results: List[WorkerResult] = []
    lock = threading.Lock()

    def _run_one(wid: int):
        r = _worker(
            wid=wid,
            host=host,
            port=port,
            model=str(args.model),
            input_text=str(args.input_text),
            requests_per_conn=rpc,
            timeout_s=int(args.timeout_s),
            sleep_ms=int(args.sleep_ms),
            token=token,
            cache_mode=str(args.cache_mode),
            cache_group=str(args.cache_group),
            verbose=bool(args.verbose),
        )
        with lock:
            results.append(r)

    threads = [threading.Thread(target=_run_one, args=(i,), daemon=True) for i in range(concurrency)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    dt = max(0.001, time.time() - t0)
    ok = sum(r.ok for r in results)
    total = sum(r.total for r in results)
    bytes_in = sum(r.bytes_in for r in results)
    all_lat = [x for r in results for x in (r.latencies_s or [])]

    err: Dict[str, int] = {}
    for r in results:
        for k, v in (r.errors or {}).items():
            err[k] = err.get(k, 0) + int(v or 0)

    print("---- summary ----")
    print("wall_s=", f"{dt:.3f}")
    print("rps=", f"{(total / dt):.2f}")
    print("ok=", ok)
    print("total=", total)
    print("success_rate=", _pct(ok / total) if total else "0.00%")
    print("bytes_in=", bytes_in)

    if all_lat:
        print("latency_p50_s=", f"{_quantile(all_lat, 0.50):.3f}")
        print("latency_p90_s=", f"{_quantile(all_lat, 0.90):.3f}")
        print("latency_p95_s=", f"{_quantile(all_lat, 0.95):.3f}")
        print("latency_p99_s=", f"{_quantile(all_lat, 0.99):.3f}")
        try:
            print("latency_mean_s=", f"{statistics.mean(all_lat):.3f}")
        except Exception:
            pass

    if err:
        print("---- errors ----")
        for k in sorted(err.keys(), key=lambda x: (-err[x], x))[:20]:
            print(f"err.count={err[k]}\terr={k[:240]}")

    return 0 if total and ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
