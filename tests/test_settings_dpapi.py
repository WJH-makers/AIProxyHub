import json
import os
import sys
import unittest

from helpers import isolated_launcher_fs


@unittest.skipUnless(sys.platform.startswith("win"), "DPAPI 仅支持 Windows")
class TestSettingsDPAPI(unittest.TestCase):
    def test_migrates_plaintext_secrets_to_dpapi_on_load(self):
        with isolated_launcher_fs() as (launcher, _td):
            # 写入旧版“明文 secret” settings.json
            plain = {
                "duckmail_token": "dk_test_123456789",
                "management_password": "pw_test_123456",
                "api_key": "aiph_test_key_1234567890",
                "proxy": "http://127.0.0.1:7890",
                "total_accounts": 3,
                "max_workers": 2,
            }
            with open(launcher.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(plain, f, ensure_ascii=False, indent=2)

            s = launcher.load_settings()

            # 内存中应返回明文（解密后）
            self.assertEqual(s["duckmail_token"], plain["duckmail_token"])
            self.assertEqual(s["management_password"], plain["management_password"])
            self.assertEqual(s["api_key"], plain["api_key"])

            # 落盘应迁移为 dpapi:...（不再明文）
            with open(launcher.SETTINGS_FILE, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
            for k in launcher.SECRET_FIELDS:
                self.assertIsInstance(on_disk.get(k), str)
                self.assertTrue(on_disk[k].startswith("dpapi:"), f"{k} 未迁移为 dpapi:...")

    def test_save_settings_keeps_secret_when_ui_sends_empty_string(self):
        with isolated_launcher_fs() as (launcher, _td):
            # 先写入并触发迁移
            with open(launcher.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "duckmail_token": "dk_test_123456789",
                        "management_password": "pw_test_123456",
                        "api_key": "aiph_test_key_1234567890",
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            s0 = launcher.load_settings()

            # UI 传入空字符串：表示保持不变
            s1 = launcher.save_settings({"api_key": ""})
            self.assertEqual(s1["api_key"], s0["api_key"])

            # 落盘仍为 dpapi:...
            with open(launcher.SETTINGS_FILE, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertTrue(str(on_disk.get("api_key", "")).startswith("dpapi:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
