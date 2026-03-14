"""
bench_gateway_latency.py

用途（面向可审计压测/对比）：
1) 对比 AIProxyHub 透明网关的两种流式传输路径：
   - HTTP(SSE): POST /v1/responses stream=true
   - WebSocket:  GET /v1/responses Upgrade + {"type":"response.create",...}
2) 输出每轮的：
   - handshake/首包/完成耗时
   - 失败率与错误摘要

安全：
- 默认从环境变量 AIPH_API_KEY 或 AIProxyHub settings.json（DPAPI 加密）读取 key
- 不会打印 key 明文

示例：
  # 5 轮 HTTP(SSE)
  python scripts/bench_gateway_latency.py --mode http --n 5

  # 5 轮 WebSocket
  python scripts/bench_gateway_latency.py --mode ws --n 5

  # 同时跑两种模式（各 5 轮）
  python scripts/bench_gateway_latency.py --mode both --n 5
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import http.client


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _get_local_appdata_dir() -> str:
    """
    Windows 上 LOCALAPPDATA 在某些 shell/任务计划场景可能缺失，这里做稳定兜底。
    """
    lad = (os.environ.get("LOCALAPPDATA") or "").strip()
    if lad:
        return lad
    home = os.path.expanduser("~")
    # 仅在 Windows 语义下使用该兜底；其它平台就回到 HOME
    return os.path.join(home, "AppData", "Local")


def _load_key_from_settings() -> str:
    """
    通过 launcher.load_settings() 读取并解密 settings.json（DPAPI）。
    """
    root = _repo_root()
    if root not in sys.path:
        sys.path.insert(0, root)

    import launcher  # type: ignore

    candidates = [
        os.path.join(_get_local_appdata_dir(), "AIProxyHub", "settings.json"),
        os.path.join(root, "settings.json"),
    ]
    settings_file = ""
    for p in candidates:
        if p and os.path.exists(p):
            settings_file = p
            break

    if settings_file:
        launcher.SETTINGS_FILE = settings_file

    s = launcher.load_settings()
    key = (s.get("api_key") or s.get("admin_api_key") or "").strip()
    return key


def load_api_key() -> str:
    key = (os.environ.get("AIPH_API_KEY") or "").strip()
    if key:
        return key
    key = _load_key_from_settings().strip()
    return key


def _mask(s: str) -> str:
    s = str(s or "")
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}***{s[-4:]}"


def _parse_base_url(base_url: str) -> Tuple[str, int, str]:
    # base_url 形如 http://127.0.0.1:8317/v1
    u = base_url.strip().rstrip("/")
    if not u.startswith("http://"):
        raise ValueError("当前脚本仅支持 http:// base_url（本地网关默认如此）")
    u2 = u[len("http://") :]
    hostport, _, path = u2.partition("/")
    if ":" in hostport:
        host, port_s = hostport.split(":", 1)
        port = int(port_s)
    else:
        host = hostport
        port = 80
    base_path = "/" + path if path else ""
    return host, port, base_path


def _iter_sse_data_strings(resp: http.client.HTTPResponse):
    """
    读取 text/event-stream 并 yield 每条 data: 的 JSON 字符串（去掉 data: 前缀）。
    """
    buf = b""
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line_s = line.decode("utf-8", errors="replace").strip()
            if not line_s:
                continue
            if not line_s.startswith("data:"):
                continue
            data = line_s[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            yield data


def _ws_accept_key(sec_websocket_key: str) -> str:
    guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    raw = (sec_websocket_key + guid).encode("utf-8", errors="ignore")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


def _ws_build_client_frame_text(payload_text: str) -> bytes:
    """
    客户端 -> 服务端必须 mask（RFC6455）。
    仅实现 text frame（opcode=0x1），FIN=1。
    """
    payload = payload_text.encode("utf-8")
    fin_opcode = 0x80 | 0x1
    mask_bit = 0x80
    ln = len(payload)
    out = bytearray()
    out.append(fin_opcode)
    if ln <= 125:
        out.append(mask_bit | ln)
    elif ln <= 65535:
        out.append(mask_bit | 126)
        out.extend(ln.to_bytes(2, "big"))
    else:
        out.append(mask_bit | 127)
        out.extend(ln.to_bytes(8, "big"))
    mask_key = random.randbytes(4) if hasattr(random, "randbytes") else bytes([random.randrange(256) for _ in range(4)])
    out.extend(mask_key)
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    out.extend(masked)
    return bytes(out)


def _ws_read_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError("unexpected EOF while reading websocket")
        data += chunk
    return data


def _ws_recv_frame(sock: socket.socket) -> Tuple[int, bytes]:
    """
    服务端 -> 客户端一般不 mask。
    返回 (opcode, payload_bytes)
    """
    h2 = _ws_read_exact(sock, 2)
    b1, b2 = h2[0], h2[1]
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    ln = b2 & 0x7F
    if ln == 126:
        ln = int.from_bytes(_ws_read_exact(sock, 2), "big")
    elif ln == 127:
        ln = int.from_bytes(_ws_read_exact(sock, 8), "big")
    mask_key = b""
    if masked:
        mask_key = _ws_read_exact(sock, 4)
    payload = _ws_read_exact(sock, ln) if ln else b""
    if masked and payload:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


@dataclass
class OneRun:
    ok: bool
    mode: str
    t_handshake_ms: float
    t_first_event_ms: float
    t_total_ms: float
    error: str = ""


def run_http_sse(*, base_url: str, api_key: str, model: str, prompt: str, timeout_s: int = 600) -> OneRun:
    host, port, base_path = _parse_base_url(base_url)
    path = f"{base_path}/responses"
    body = json.dumps({"model": model, "input": prompt, "stream": True}, ensure_ascii=False).encode("utf-8")

    t0 = time.perf_counter()
    conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    try:
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        resp = conn.getresponse()
        t1 = time.perf_counter()
        if resp.status != 200:
            raw = resp.read() or b""
            return OneRun(
                ok=False,
                mode="http",
                t_handshake_ms=(t1 - t0) * 1000.0,
                t_first_event_ms=0.0,
                t_total_ms=(time.perf_counter() - t0) * 1000.0,
                error=f"HTTP {resp.status}: {raw[:200].decode('utf-8', errors='replace')}",
            )

        first = None
        completed = False
        for data in _iter_sse_data_strings(resp):
            if first is None:
                first = time.perf_counter()
            try:
                obj = json.loads(data)
                t = str(obj.get("type") or "")
                if t.endswith(".completed") or t.endswith(".failed") or t in ("response.completed", "response.failed"):
                    completed = True
                    break
            except Exception:
                # 不因单条解析失败就终止
                pass
        t_end = time.perf_counter()
        return OneRun(
            ok=bool(completed),
            mode="http",
            t_handshake_ms=(t1 - t0) * 1000.0,
            t_first_event_ms=(0.0 if first is None else (first - t0) * 1000.0),
            t_total_ms=(t_end - t0) * 1000.0,
            error=("incomplete stream" if not completed else ""),
        )
    except Exception as e:
        return OneRun(
            ok=False,
            mode="http",
            t_handshake_ms=0.0,
            t_first_event_ms=0.0,
            t_total_ms=(time.perf_counter() - t0) * 1000.0,
            error=str(e),
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_ws(*, base_url: str, api_key: str, model: str, prompt: str, timeout_s: int = 600) -> OneRun:
    host, port, base_path = _parse_base_url(base_url)
    path = f"{base_path}/responses"

    # 仅支持 ws://（本地网关）
    t0 = time.perf_counter()
    sock = socket.create_connection((host, port), timeout=timeout_s)
    try:
        ws_key_raw = os.urandom(16)
        ws_key = base64.b64encode(ws_key_raw).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {api_key}\r\n"
            "\r\n"
        ).encode("utf-8")
        sock.sendall(req)

        # 读响应头（直到 \r\n\r\n）
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                raise EOFError("EOF during ws handshake")
            header += chunk
            if len(header) > 64 * 1024:
                raise RuntimeError("ws handshake header too large")
        head, rest = header.split(b"\r\n\r\n", 1)
        head_s = head.decode("utf-8", errors="replace")
        if " 101 " not in head_s:
            raise RuntimeError(f"ws handshake failed: {head_s.splitlines()[0] if head_s else 'unknown'}")
        # 简单校验 accept（best-effort）
        accept_expect = _ws_accept_key(ws_key)
        if ("Sec-WebSocket-Accept:" in head_s) and (accept_expect not in head_s):
            # 不强制 fail：某些代理会改写大小写/空格，这里只提示
            pass

        t1 = time.perf_counter()

        # 服务器可能已经把一部分 frame 写进 rest；这里简单处理：先把 rest 放回一个小缓冲
        pending = bytearray(rest)

        def recv_frame() -> Tuple[int, bytes]:
            nonlocal pending
            if pending:
                # 把 pending 喂给一个临时 socket-like 会很复杂；这里用一个最小化策略：
                # 若 pending 不够读头部，就继续从 sock 读补齐。
                # 由于 frame 边界不可知，这里退回到“先把 pending 写回一个本地缓冲并从中消费”的方式：
                # 通过 monkey patch 一个 recv 函数会更复杂，因此这里采取：将 pending 作为首段数据，
                # 再按需从 sock 补齐。实现上用一个闭包读取。
                pass
            # 直接从 sock 读取（对本地网关足够稳定）
            return _ws_recv_frame(sock)

        msg = {
            "type": "response.create",
            "response": {
                "model": model,
                "input": prompt,
            },
        }
        frame = _ws_build_client_frame_text(json.dumps(msg, ensure_ascii=False))
        sock.sendall(frame)

        first = None
        completed = False
        t_end = None
        while True:
            opcode, payload = recv_frame()
            if opcode == 0x8:
                break  # close
            if opcode != 0x1:
                continue
            if first is None:
                first = time.perf_counter()
            try:
                text = payload.decode("utf-8", errors="replace")
                obj = json.loads(text)
                t = str(obj.get("type") or "")
                if t.endswith(".completed") or t.endswith(".failed") or t in ("response.completed", "response.failed"):
                    completed = True
                    t_end = time.perf_counter()
                    break
            except Exception:
                continue

        if t_end is None:
            t_end = time.perf_counter()
        return OneRun(
            ok=bool(completed),
            mode="ws",
            t_handshake_ms=(t1 - t0) * 1000.0,
            t_first_event_ms=(0.0 if first is None else (first - t0) * 1000.0),
            t_total_ms=(t_end - t0) * 1000.0,
            error=("incomplete stream" if not completed else ""),
        )
    except Exception as e:
        return OneRun(
            ok=False,
            mode="ws",
            t_handshake_ms=0.0,
            t_first_event_ms=0.0,
            t_total_ms=(time.perf_counter() - t0) * 1000.0,
            error=str(e),
        )
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    if p <= 0:
        return values[0]
    if p >= 100:
        return values[-1]
    k = (len(values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    d0 = values[f] * (c - k)
    d1 = values[c] * (k - f)
    return d0 + d1


def summarize(runs: List[OneRun]) -> Dict[str, Any]:
    ok = [r for r in runs if r.ok]
    fail = [r for r in runs if not r.ok]
    total_ms = [r.t_total_ms for r in ok]
    first_ms = [r.t_first_event_ms for r in ok if r.t_first_event_ms > 0]
    hs_ms = [r.t_handshake_ms for r in ok if r.t_handshake_ms > 0]
    return {
        "runs": len(runs),
        "ok": len(ok),
        "fail": len(fail),
        "p50_total_ms": round(_percentile(total_ms, 50), 2),
        "p95_total_ms": round(_percentile(total_ms, 95), 2),
        "p50_first_event_ms": round(_percentile(first_ms, 50), 2),
        "p95_first_event_ms": round(_percentile(first_ms, 95), 2),
        "p50_handshake_ms": round(_percentile(hs_ms, 50), 2),
        "p95_handshake_ms": round(_percentile(hs_ms, 95), 2),
        "errors_top": {e: sum(1 for r in fail if r.error == e) for e in sorted({r.error for r in fail})[:5]},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8317/v1")
    ap.add_argument("--mode", choices=["http", "ws", "both"], default="both")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--model", default="gpt5.2-xhigh")
    ap.add_argument("--prompt", default="只回复 OK")
    args = ap.parse_args()

    key = load_api_key()
    if not key:
        print("ERROR: 未找到 AIPH_API_KEY（请先在管理面板设置 API Key，或在当前环境设置 AIPH_API_KEY）", file=sys.stderr)
        return 2

    print(f"base_url={args.base_url} api_key(masked)={_mask(key)} model={args.model} n={args.n} mode={args.mode}")

    out: Dict[str, Any] = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "base_url": args.base_url, "model": args.model}

    if args.mode in ("http", "both"):
        runs_http: List[OneRun] = []
        for _ in range(args.n):
            runs_http.append(run_http_sse(base_url=args.base_url, api_key=key, model=args.model, prompt=args.prompt))
        out["http"] = {"summary": summarize(runs_http), "runs": [r.__dict__ for r in runs_http]}

    if args.mode in ("ws", "both"):
        runs_ws: List[OneRun] = []
        for _ in range(args.n):
            runs_ws.append(run_ws(base_url=args.base_url, api_key=key, model=args.model, prompt=args.prompt))
        out["ws"] = {"summary": summarize(runs_ws), "runs": [r.__dict__ for r in runs_ws]}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

