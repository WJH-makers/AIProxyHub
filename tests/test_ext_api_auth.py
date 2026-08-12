import json
import threading
import urllib.error
import urllib.request
import unittest

import http.server

from helpers import isolated_launcher_fs


class TestExtApiAuth(unittest.TestCase):
    def test_ext_api_requires_bearer_and_accepts_correct_token(self):
        with isolated_launcher_fs() as (launcher, _td):
            # 写入配置（明文即可，load_settings 会迁移为 dpapi:...）
            with open(launcher.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({"api_key": "k1234567890abcdef", "management_password": "pw1234567890"}, f, ensure_ascii=False)

            # 启动一个随机端口的 HTTP server（仅用于测试 handler 行为）
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), launcher.Handler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{port}"

                # 无 Authorization -> 401
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(base + "/api/ext/status", timeout=3)
                self.assertEqual(ctx.exception.code, 401)
                try:
                    ctx.exception.close()
                except Exception:
                    pass

                # 错误 token -> 401
                req = urllib.request.Request(
                    base + "/api/ext/status",
                    headers={"Authorization": "Bearer wrong"},
                    method="GET",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx2:
                    urllib.request.urlopen(req, timeout=3)
                self.assertEqual(ctx2.exception.code, 401)
                try:
                    ctx2.exception.close()
                except Exception:
                    pass

                # 正确 token -> 200
                req_ok = urllib.request.Request(
                    base + "/api/ext/status",
                    headers={"Authorization": "Bearer k1234567890abcdef"},
                    method="GET",
                )
                resp = urllib.request.urlopen(req_ok, timeout=3)
                body = resp.read().decode("utf-8", errors="replace")
                data = json.loads(body)
                self.assertTrue(data.get("ok"), data)
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
