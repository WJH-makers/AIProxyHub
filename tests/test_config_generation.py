import os
import unittest

from helpers import isolated_launcher_fs


class TestConfigGeneration(unittest.TestCase):
    def test_generate_proxy_config_writes_expected_fields_and_escapes_quotes(self):
        with isolated_launcher_fs() as (launcher, _td):
            out = os.path.join(launcher.RUNTIME_DIR, "out.yaml")
            s = {
                "proxy_host": "127.0.0.1",
                "proxy_port": 8317,
                "management_password": "pw'123",
                "api_key": "aiph_key'456",
                "proxy": "http://127.0.0.1:7890",
                "routing_strategy": "round-robin",
                "request_retry": 3,
                "quota_switch_project": True,
                "quota_switch_preview": True,
                "debug": False,
            }
            launcher.generate_proxy_config(s, out)

            with open(out, "r", encoding="utf-8") as f:
                text = f.read()

            # 关键字段存在
            self.assertIn("host:", text)
            self.assertIn("port:", text)
            self.assertIn("proxy-url:", text)
            self.assertIn("remote-management:", text)
            self.assertIn("api-keys:", text)

            # 单引号应被 YAML escape（''）
            self.assertIn("pw''123", text)
            self.assertIn("aiph_key''456", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
