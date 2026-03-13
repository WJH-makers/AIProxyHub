#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WebSocket 压测脚本：对 AIProxyHub 网关的 /v1/responses WebSocket mode 进行并发/吞吐/稳定性测试。

特点：
- **零第三方依赖**（不需要安装 websockets 库），便于在打包环境/干净 venv 里直接运行。
- 不会回显任何 API Key 明文。
- 支持并发连接数、每连接请求数、请求超时、请求间隔等参数化。

默认目标：
  ws://127.0.0.1:8317/v1/responses

认证来源（按优先级）：
1) 环境变量 AIPH_API_KEY
2) 从项目根目录 launcher.load_settings() 读取 settings.json 解密后的 api_key

示例（PowerShell）：
  # 方式 1：你已经在当前 shell 注入了 AIPH_API_KEY
  E:/AIProxyHub/.venv/Scripts/python.exe E:/AIProxyHub/scripts/ws_load_responses.py --concurrency 10 --requests-per-conn 3

  # 方式 2：不注入环境变量，直接使用 settings.json 里的 api_key（需要在 AIProxyHub UI 已配置）
  E:/AIProxyHub/.venv/Scripts/python.exe E:/AIProxyHub/scripts/ws_load_responses.py --concurrency 10 --requests-per-conn 3

注意：
- WebSocket mode 强制使用 stream 语义（事件流）。当 AIProxyHub 开启 cache_stream_enabled 后，WS 也会命中“事件流缓存”，命中时会快速回放整条事件流（适合重复压测/重复任务）。
- 如需压测真实回源延迟，请使用 `--cache-mode bypass` 绕过缓存读写。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple
import urllib.request
import urllib.error


# 允许从 scripts/ 目录运行并导入项目根目录的 launcher.py
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import launcher  # type: ignore  # noqa: E402
except Exception:
    launcher = None  # type: ignore


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("unexpected EOF")
        buf.extend(chunk)
    return bytes(buf)


def _send_frame_masked(sock: socket.socket, opcode: int, payload: bytes) -> None:
    payload = payload or b""
    ln = len(payload)
    first = 0x80 | (opcode & 0x0F)

    # Client->Server 必须 mask（RFC6455）
    mask_key = os.urandom(4)
    masked = bytes(payload[i] ^ mask_key[i % 4] for i in range(ln))

    header = bytearray([first])
    if ln <= 125:
        header.append(0x80 | ln)
    elif ln <= 65535:
        header.append(0x80 | 126)
        header.extend(ln.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(ln.to_bytes(8, "big"))
    header.extend(mask_key)
    sock.sendall(bytes(header) + masked)


def _recv_frame(sock: socket.socket) -> Tuple[bool, int, bytes]:
    b1, b2 = _read_exact(sock, 2)
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    ln = b2 & 0x7F
    if ln == 126:
        ln = int.from_bytes(_read_exact(sock, 2), "big")
    elif ln == 127:
        ln = int.from_bytes(_read_exact(sock, 8), "big")
    mask = _read_exact(sock, 4) if masked else b""
    payload = _read_exact(sock, ln) if ln else b""
    if masked and payload:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return fin, opcode, payload


def _recv_message(sock: socket.socket) -> Tuple[int | None, bytes]:
    """
    接收一条“消息级”载荷，自动处理 ping/pong 与分片。
    返回 (opcode, payload)；opcode=None 表示 close/EOF。
    """
    opcode = None
    chunks: List[bytes] = []
    while True:
        fin, op, payload = _recv_frame(sock)
        if op == 0x8:  # close
            return None, b""
        if op == 0x9:  # ping
            _send_frame_masked(sock, 0xA, payload)  # pong
            continue
        if op == 0xA:  # pong
            continue
        if op != 0x0:
            opcode = op
        if payload:
            chunks.append(payload)
        if fin:
            break
    return opcode, b"".join(chunks)


def _connect_ws(*, host: str, port: int, path: str, token: str, timeout_s: int, extra_headers: Dict[str, str] | None) -> socket.socket:
    ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
    req_lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Key: {ws_key}",
        f"Authorization: Bearer {token}",
    ]
    for k, v in (extra_headers or {}).items():
        if not k:
            continue
        req_lines.append(f"{k}: {v}")
    req = "\r\n".join(req_lines) + "\r\n\r\n"

    sock = socket.create_connection((host, int(port)), timeout=float(timeout_s))
    sock.sendall(req.encode("utf-8"))
    head = sock.recv(4096).decode("utf-8", errors="replace")
    first_line = head.splitlines()[0] if head else ""
    if "101" not in first_line:
        try:
            sock.close()
        except Exception:
            pass
        raise RuntimeError(f"handshake failed: {first_line}")
    try:
        sock.settimeout(float(timeout_s))
    except Exception:
        pass
    return sock


@dataclass
class RequestResult:
    ok: bool
    latency_s: float
    bytes_in: int
    error: str


def _one_request(sock: socket.socket, *, model: str, input_text: str, timeout_s: int) -> RequestResult:
    msg = {
        "type": "response.create",
        "response": {
            "model": model,
            "input": input_text,
            # 明确不落库（对齐 Codex 默认更安全行为）
            "store": False,
        },
    }
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    _send_frame_masked(sock, 0x1, payload)

    started = time.time()
    bytes_in = 0
    while True:
        if time.time() - started > float(timeout_s):
            return RequestResult(ok=False, latency_s=float(timeout_s), bytes_in=bytes_in, error="timeout_wait_completed")
        op, raw = _recv_message(sock)
        if op is None:
            return RequestResult(ok=False, latency_s=time.time() - started, bytes_in=bytes_in, error="server_closed")
        bytes_in += len(raw or b"")
        if op != 0x1:
            continue
        try:
            evt = json.loads((raw or b"").decode("utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        t = str(evt.get("type") or "")
        if t == "error":
            err = evt.get("error") if isinstance(evt.get("error"), dict) else {}
            status = err.get("status") if isinstance(err, dict) else None
            msg_text = err.get("message") if isinstance(err, dict) else None
            msg_short = str(msg_text or "")[:160].replace("\n", " ")
            return RequestResult(
                ok=False,
                latency_s=time.time() - started,
                bytes_in=bytes_in,
                error=f"error_event_status={status} msg={msg_short}",
            )
        if t == "response.failed":
            return RequestResult(ok=False, latency_s=time.time() - started, bytes_in=bytes_in, error="response_failed")
        if t == "response.completed":
            return RequestResult(ok=True, latency_s=time.time() - started, bytes_in=bytes_in, error="")


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
    path: str,
    token: str,
    model: str,
    input_text: str,
    requests_per_conn: int,
    timeout_s: int,
    sleep_ms: int,
    extra_headers: Dict[str, str] | None,
    verbose: bool,
) -> WorkerResult:
    ok = 0
    total = 0
    latencies: List[float] = []
    bytes_in = 0
    errors: Dict[str, int] = {}

    try:
        sock = _connect_ws(
            host=host,
            port=port,
            path=path,
            token=token,
            timeout_s=timeout_s,
            extra_headers=extra_headers,
        )
    except Exception as e:
        msg = f"handshake_error:{type(e).__name__}:{e}"
        errors[msg] = errors.get(msg, 0) + 1
        return WorkerResult(ok=0, total=0, latencies_s=[], bytes_in=0, errors=errors)

    try:
        for i in range(int(requests_per_conn)):
            total += 1
            try:
                r = _one_request(sock, model=model, input_text=input_text, timeout_s=timeout_s)
            except Exception as e:
                r = RequestResult(ok=False, latency_s=0.0, bytes_in=0, error=f"exception:{type(e).__name__}:{e}")

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

            if sleep_ms and sleep_ms > 0:
                time.sleep(float(sleep_ms) / 1000.0)
    finally:
        try:
            sock.close()
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
    # 线性插值（与常见压测工具一致的近似）
    pos = (len(xs2) - 1) * float(q)
    lo = int(pos)
    hi = min(lo + 1, len(xs2) - 1)
    if hi == lo:
        return float(xs2[lo])
    frac = pos - lo
    return float(xs2[lo]) * (1.0 - frac) + float(xs2[hi]) * frac


def _load_token() -> str:
    env = str(os.getenv("AIPH_API_KEY", "") or "").strip()
    if env:
        return env
    if launcher is not None:
        try:
            s = launcher.load_settings()
            t = str(s.get("api_key", "") or "").strip()
            if t:
                return t
        except Exception:
            pass
    return ""


def _ensure_proxy_running(*, launcher_host: str, launcher_port: int, token: str, timeout_s: int) -> Tuple[bool, str]:
    """
    通过 AIProxyHub External API 启动代理（幂等）。
    - 不回显 token
    - 仅输出 ok/msg
    """
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--launcher-host", default=os.getenv("AIPH_LAUNCHER_HOST", "127.0.0.1"))
    ap.add_argument("--launcher-port", type=int, default=int(os.getenv("AIPH_LAUNCHER_PORT", "9090")))
    ap.add_argument("--ensure-proxy", action="store_true", help="测试前尝试通过 External API 启动代理（幂等）")
    ap.add_argument("--host", default=os.getenv("AIPH_WS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("AIPH_WS_PORT", "8317")))
    ap.add_argument("--path", default=os.getenv("AIPH_WS_PATH", "/v1/responses"))
    ap.add_argument("--model", default=os.getenv("AIPH_WS_MODEL", "gpt5.2-xhigh"))
    ap.add_argument("--input", dest="input_text", default=os.getenv("AIPH_WS_INPUT", "ping"))
    ap.add_argument(
        "--cache-mode",
        choices=["on", "bypass", "refresh", "no-store"],
        default=os.getenv("AIPH_WS_CACHE_MODE", "bypass"),
        help="缓存控制：on=默认；bypass=绕过读写；refresh=绕过读但允许写；no-store=不写但允许读",
    )
    ap.add_argument("--cache-group", default=os.getenv("AIPH_WS_CACHE_GROUP", "loadtest"))
    ap.add_argument("--concurrency", type=int, default=int(os.getenv("AIPH_WS_CONCURRENCY", "10")))
    ap.add_argument("--requests-per-conn", type=int, default=int(os.getenv("AIPH_WS_REQUESTS_PER_CONN", "3")))
    ap.add_argument("--timeout-s", type=int, default=int(os.getenv("AIPH_WS_TIMEOUT_S", "60")))
    ap.add_argument("--sleep-ms", type=int, default=int(os.getenv("AIPH_WS_SLEEP_MS", "0")))
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument(
        "--mock",
        action="store_true",
        help="仅用于压测：在握手时附加 X-AIProxyHub-Mock: 1（需要网关支持该调试头）",
    )
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

    concurrency = max(1, int(args.concurrency or 1))
    rpc = max(1, int(args.requests_per_conn or 1))

    extra_headers: Dict[str, str] = {}
    if bool(args.mock):
        extra_headers["X-AIProxyHub-Mock"] = "1"
    cache_mode = str(args.cache_mode or "").strip()
    if cache_mode and cache_mode.lower() != "on":
        extra_headers["X-AIProxyHub-Cache"] = cache_mode
    cache_group = str(args.cache_group or "").strip()
    if cache_group:
        extra_headers["X-AIProxyHub-Cache-Group"] = cache_group
    if not extra_headers:
        extra_headers = None  # type: ignore

    print("target.ws=", f"ws://{args.host}:{int(args.port)}{args.path}")
    print("concurrency=", concurrency)
    print("requests_per_conn=", rpc)
    print("total_requests=", concurrency * rpc)
    print("timeout_s=", int(args.timeout_s))
    print("sleep_ms=", int(args.sleep_ms))
    print("model=", str(args.model))
    print("input_len=", len(str(args.input_text or "")))
    print("cache_mode=", str(args.cache_mode))
    print("cache_group=", str(args.cache_group))
    print("auth.source=", "env:AIPH_API_KEY" if str(os.getenv("AIPH_API_KEY", "") or "").strip() else "settings.json(api_key)")

    t0 = time.time()
    results: List[WorkerResult] = []
    lock = threading.Lock()

    def _run_one(wid: int):
        r = _worker(
            wid=wid,
            host=str(args.host),
            port=int(args.port),
            path=str(args.path),
            token=token,
            model=str(args.model),
            input_text=str(args.input_text),
            requests_per_conn=rpc,
            timeout_s=int(args.timeout_s),
            sleep_ms=int(args.sleep_ms),
            extra_headers=extra_headers,
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

    # 聚合错误
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

    # 返回码：全成功为 0；否则 1
    return 0 if total and ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
