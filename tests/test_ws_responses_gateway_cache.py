import base64
import json
import socket
import threading
import time
import unittest
import http.server

from helpers import isolated_launcher_fs


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
    mask_key = b"\x01\x02\x03\x04"
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


def _recv_frame(sock: socket.socket) -> tuple[bool, int, bytes]:
    b1, b2 = _read_exact(sock, 2)
    fin = bool(b1 & 0x80)
    opcode = int(b1 & 0x0F)
    masked = bool(b2 & 0x80)
    ln = int(b2 & 0x7F)
    if ln == 126:
        ln = int.from_bytes(_read_exact(sock, 2), "big")
    elif ln == 127:
        ln = int.from_bytes(_read_exact(sock, 8), "big")
    mask = _read_exact(sock, 4) if masked else b""
    payload = _read_exact(sock, ln) if ln else b""
    if masked and payload:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return fin, opcode, payload


def _recv_message(sock: socket.socket) -> tuple[int | None, bytes]:
    """
    接收一条“消息级”载荷，自动处理 ping/pong 与分片。
    返回 (opcode, payload)；opcode=None 表示 close/EOF。
    """
    opcode = None
    chunks: list[bytes] = []
    while True:
        fin, op, payload = _recv_frame(sock)
        if op == 0x8:  # close
            return None, b""
        if op == 0x9:  # ping
            # server->client ping: 回 pong
            _send_frame_masked(sock, 0xA, payload)
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


def _ws_request_once(*, host: str, port: int, token: str, msg: dict, timeout_s: float = 10.0) -> list[str]:
    ws_key = base64.b64encode(b"test-ws-key-1234").decode("ascii")
    req = (
        "GET /v1/responses HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        f"Authorization: Bearer {token}\r\n"
        "\r\n"
    )

    sock = socket.create_connection((host, int(port)), timeout=timeout_s)
    try:
        sock.sendall(req.encode("utf-8"))
        # 读取握手响应头
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 8192:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        first = head.splitlines()[0].decode("utf-8", errors="replace") if head else ""
        if "101" not in first:
            raise AssertionError(f"handshake failed: {first}")

        # 放宽超时等待 completed
        try:
            sock.settimeout(timeout_s)
        except Exception:
            pass

        _send_frame_masked(sock, 0x1, json.dumps(msg, ensure_ascii=False).encode("utf-8"))

        types: list[str] = []
        started = time.time()
        while True:
            if time.time() - started > timeout_s:
                raise TimeoutError("timeout_wait_completed")
            op, payload = _recv_message(sock)
            if op is None:
                raise EOFError("server_closed")
            if op != 0x1:
                continue
            evt = json.loads(payload.decode("utf-8", errors="replace"))
            if isinstance(evt, dict) and evt.get("type"):
                t = str(evt.get("type"))
                types.append(t)
                if t in ("response.completed", "response.failed"):
                    return types
    finally:
        try:
            sock.close()
        except Exception:
            pass


class TestWSResponsesGatewayCache(unittest.TestCase):
    def test_ws_cache_replay_hits_and_singleflight_prevents_stampede(self):
        with isolated_launcher_fs() as (launcher, _td):
            # 打开网关缓存（含 stream 缓存），并清空缓存统计
            launcher._gateway_set_config_from_settings(
                {
                    "api_key": "sk-test",
                    "cache_enabled": True,
                    "cache_stream_enabled": True,
                    "cache_shared_across_api_keys": False,
                    "cache_ttl_seconds": 3600,
                    "cache_ttl_jitter_seconds": 0,
                    "cache_max_entries": 50,
                    "cache_max_body_kb": 128,
                    "cache_max_total_mb": 0,
                    "cache_stale_while_revalidate_seconds": 0,
                    "cache_stale_if_error_seconds": 0,
                }
            )
            launcher._cache_clear()

            counter = {"n": 0}

            def make_upstream_handler(delay_s: float):
                class UpstreamHandler(http.server.BaseHTTPRequestHandler):
                    protocol_version = "HTTP/1.1"

                    def log_message(self, *a):
                        return

                    def do_POST(self):
                        if self.path.split("?", 1)[0] != "/v1/responses":
                            self.send_response(404)
                            self.end_headers()
                            return
                        counter["n"] += 1
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.close_connection = True
                        # 慢一点，确保并发时能制造重叠窗口（用于验证 singleflight）
                        events = [
                            {"type": "response.created"},
                            {"type": "response.completed"},
                        ]
                        for obj in events:
                            b = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
                            self.wfile.write(b)
                            try:
                                self.wfile.flush()
                            except Exception:
                                pass
                            time.sleep(delay_s)

                return UpstreamHandler

            # 启动 mock upstream
            upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_upstream_handler(delay_s=0.15))
            upstream_port = int(upstream.server_address[1])
            t_up = threading.Thread(target=upstream.serve_forever, daemon=True)
            t_up.start()

            # 启动 gateway（上游指向 mock upstream）
            gw_handler = launcher._make_gateway_handler("127.0.0.1", upstream_port)
            gateway = http.server.ThreadingHTTPServer(("127.0.0.1", 0), gw_handler)
            gw_port = int(gateway.server_address[1])
            t_gw = threading.Thread(target=gateway.serve_forever, daemon=True)
            t_gw.start()

            try:
                msg = {
                    "type": "response.create",
                    "response": {"model": "gpt5.2-xhigh", "input": "ping", "store": False},
                }
                # Codex(v0.114.0+) 真实形态（扁平）：type + 其余字段直接等同 /v1/responses body
                msg_flat = {
                    "type": "response.create",
                    "model": "gpt5.2-xhigh",
                    "input": "ping",
                    "store": False,
                }
                # 第一次：冷缓存（必须回源 1 次）
                types1 = _ws_request_once(host="127.0.0.1", port=gw_port, token="sk-test", msg=msg, timeout_s=10.0)
                self.assertIn("response.completed", types1)
                self.assertEqual(counter["n"], 1)

                # 第二次：命中缓存（不应再次回源）
                types2 = _ws_request_once(host="127.0.0.1", port=gw_port, token="sk-test", msg=msg, timeout_s=10.0)
                self.assertIn("response.completed", types2)
                self.assertEqual(counter["n"], 1)

                # 第三次：同请求但用“扁平消息形态”发起 → 仍应命中同一缓存键（不应再次回源）
                types3 = _ws_request_once(
                    host="127.0.0.1", port=gw_port, token="sk-test", msg=msg_flat, timeout_s=10.0
                )
                self.assertIn("response.completed", types3)
                self.assertEqual(counter["n"], 1)

                # 并发：两条连接同时发同一个请求 → singleflight 下仍应只回源 1 次
                launcher._cache_clear()
                counter["n"] = 0

                out: list[list[str]] = []
                lock = threading.Lock()

                def worker():
                    # 并发场景也用 Codex 形态覆盖一次
                    tps = _ws_request_once(
                        host="127.0.0.1", port=gw_port, token="sk-test", msg=msg_flat, timeout_s=10.0
                    )
                    with lock:
                        out.append(tps)

                th1 = threading.Thread(target=worker)
                th2 = threading.Thread(target=worker)
                th1.start()
                th2.start()
                th1.join(timeout=15)
                th2.join(timeout=15)

                self.assertEqual(len(out), 2)
                self.assertTrue(all("response.completed" in x for x in out))
                self.assertEqual(counter["n"], 1)
            finally:
                try:
                    gateway.shutdown()
                except Exception:
                    pass
                try:
                    gateway.server_close()
                except Exception:
                    pass
                try:
                    upstream.shutdown()
                except Exception:
                    pass
                try:
                    upstream.server_close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)

