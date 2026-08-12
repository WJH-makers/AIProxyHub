"""
最小可复现：验证 AIProxyHub 网关的 /v1/responses WebSocket mode 是否可用。

用法（PowerShell 示例，注意不要把 key 打到终端历史里）：

  # 方式 1：用现有 wrapper 注入 AIPH_API_KEY 后再跑
  #   powershell -ExecutionPolicy Bypass -File C:/Users/wjh19/scripts/run-codex-with-aiproxyhub-key.ps1 -KeepEnv -- --version
  #   $env:AIPH_API_KEY 仍在当前会话时：
  #   E:/AIProxyHub/.venv/Scripts/python.exe E:/AIProxyHub/scripts/ws_smoke_responses.py
  #
  # 方式 2：自行注入 AIPH_API_KEY（仅当前命令进程）
  #   $env:AIPH_API_KEY="***"; E:/AIProxyHub/.venv/Scripts/python.exe E:/AIProxyHub/scripts/ws_smoke_responses.py

本脚本不会回显任何密钥内容。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import time
from typing import Tuple


HOST = os.getenv("AIPH_WS_HOST", "127.0.0.1")
PORT = int(os.getenv("AIPH_WS_PORT", "8317"))
PATH = os.getenv("AIPH_WS_PATH", "/v1/responses")

# 允许从任意工作目录运行：自动定位项目根目录并读取 settings.json（DPAPI 解密后仅在内存使用）
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import launcher  # type: ignore
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

    # Client->Server 必须 mask
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
    opcode = None
    chunks = []
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


def main() -> int:
    token = str(os.getenv("AIPH_API_KEY", "") or "").strip()
    if not token and launcher is not None:
        try:
            s = launcher.load_settings()
            token = str(s.get("admin_api_key") or s.get("api_key") or "").strip()
        except Exception:
            token = ""
    if not token:
        print("缺少 API Key：请设置环境变量 AIPH_API_KEY 或在 AIProxyHub UI 配置 api_key（本脚本不会回显密钥）。")
        return 2

    ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        f"GET {PATH} HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        f"Authorization: Bearer {token}\r\n"
        "\r\n"
    )

    sock = socket.create_connection((HOST, PORT), timeout=8)
    try:
        sock.sendall(req.encode("utf-8"))
        head = sock.recv(4096).decode("utf-8", errors="replace")
        first_line = head.splitlines()[0] if head else ""
        if "101" not in first_line:
            print("WebSocket 握手失败：", first_line)
            return 1
        print("WebSocket 握手成功：", first_line)
        # 读取事件期间放宽超时（避免首包较慢时误判失败）
        try:
            sock.settimeout(60)
        except Exception:
            pass

        # WebSocket mode: 客户端发送 response.create 事件
        #
        # 说明：
        # - Codex(v0.114.0+) 实际发送的是“扁平形态”：
        #     {"type":"response.create", ...其余字段直接等同 /v1/responses body...}
        # - 早期自测脚本常用的 {"type":"response.create","response":{...}} 形态也被网关兼容，
        #   但这里默认用 Codex 真实形态，避免压测/验收与实际不一致。
        msg = {
            "type": "response.create",
            "model": "gpt5.2-xhigh",
            "input": "ping",
            "store": False,
        }
        _send_frame_masked(sock, 0x1, json.dumps(msg, ensure_ascii=False).encode("utf-8"))

        # 接收事件直到 response.completed / response.failed
        started = time.time()
        while True:
            if time.time() - started > 60:
                print("超时：60s 内未收到 completed/failed。")
                return 1
            try:
                op, payload = _recv_message(sock)
            except TimeoutError:
                print("超时：等待服务端事件超时。")
                return 1
            if op is None:
                print("服务端关闭连接。")
                return 1
            if op != 0x1:
                continue
            try:
                evt = json.loads(payload.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if isinstance(evt, dict) and evt.get("type"):
                t = str(evt.get("type"))
                print("event:", t)
                if t == "error":
                    err = evt.get("error") if isinstance(evt.get("error"), dict) else {}
                    msg = err.get("message") if isinstance(err, dict) else None
                    status = err.get("status") if isinstance(err, dict) else None
                    print("error.status:", status)
                    if msg:
                        print("error.message:", str(msg)[:500])
                    return 1
                if t in ("response.completed", "response.failed"):
                    return 0
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
