import json
import os
import threading
import urllib.request
import unittest

import http.server

from helpers import isolated_launcher_fs


class _FakeProxy401(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            body = b'{"error":"unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestProxyExternalDetection(unittest.TestCase):
    def test_start_proxy_is_idempotent_when_port_already_has_proxy(self):
        with isolated_launcher_fs() as (launcher, td):
            # 外部服务占用端口（模拟另一个 CLIProxyAPI 实例）
            fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeProxy401)
            fake_port = int(fake.server_address[1])
            t = threading.Thread(target=fake.serve_forever, daemon=True)
            t.start()

            try:
                # 满足 preflight：需要存在 proxy exe 文件
                # 注意：isolated_launcher_fs 只会 patch ROOT；若直接调用 _get_proxy_exe_path()，
                # 可能回落到项目目录下真实的 cli-proxy-api.exe（在本机运行时还可能被正在运行的代理进程锁定）。
                # 因此这里显式在沙箱 ROOT 下创建一个假的 exe 文件，确保 preflight 通过且不污染真实文件。
                exe_path = os.path.join(launcher.ROOT, "cli-proxy-api.exe")
                with open(exe_path, "wb") as f:
                    f.write(b"")

                with open(launcher.SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "api_key": "k1234567890abcdef",
                            "management_password": "pw1234567890",
                            "proxy_port": fake_port,
                            "proxy_host": "127.0.0.1",
                            "proxy": "http://127.0.0.1:7890",
                            "gateway_enabled": False,
                            "cache_enabled": False,
                        },
                        f,
                        ensure_ascii=False,
                    )

                r = launcher.start_proxy()
                self.assertTrue(bool(r.get("ok")), r)
                self.assertTrue(bool(r.get("already_running")), r)
                self.assertFalse(bool(r.get("managed")), r)
                self.assertIsNone(launcher.proxy_process)
                self.assertIsNone(launcher.gateway_server)
            finally:
                fake.shutdown()
                fake.server_close()

    def test_status_reports_external_proxy_as_running(self):
        with isolated_launcher_fs() as (launcher, _td):
            fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeProxy401)
            fake_port = int(fake.server_address[1])
            t = threading.Thread(target=fake.serve_forever, daemon=True)
            t.start()

            # 写入配置：指向外部代理端口
            with open(launcher.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "api_key": "k1234567890abcdef",
                        "management_password": "pw1234567890",
                        "proxy_port": fake_port,
                    },
                    f,
                    ensure_ascii=False,
                )

            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), launcher.Handler)
            port = int(httpd.server_address[1])
            th = threading.Thread(target=httpd.serve_forever, daemon=True)
            th.start()

            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                self.assertTrue(bool(data.get("proxy")), data)
                self.assertTrue(bool(data.get("proxy_external")), data)
                self.assertFalse(bool(data.get("proxy_managed")), data)
            finally:
                httpd.shutdown()
                httpd.server_close()
                fake.shutdown()
                fake.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
