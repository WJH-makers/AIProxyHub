import os
import json
import unittest

from helpers import isolated_launcher_fs


class TestSecureCleanupOutputs(unittest.TestCase):
    def test_secure_cleanup_outputs_redacts_and_deletes(self):
        with isolated_launcher_fs() as (launcher, _td):
            # 避免本机已有服务占用默认 8317 端口导致 _proxy_reachable=True 从而跳过删除 runtime yaml
            with open(launcher.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({"proxy_port": 59999}, f, ensure_ascii=False)

            # 1) registered_accounts.txt（包含明文密码）
            reg_fp = os.path.join(launcher.DATA_DIR, "registered_accounts.txt")
            with open(reg_fp, "w", encoding="utf-8") as f:
                f.write("a@example.com----p1----p2----oauth=ok\n")
                f.write("b@example.com----***----***----oauth=ok\n")

            # 2) ak/rk 明文 token
            for name in ("ak.txt", "rk.txt"):
                with open(os.path.join(launcher.DATA_DIR, name), "w", encoding="utf-8") as f:
                    f.write("SENSITIVE_TOKEN\n")

            # 3) token_json 目录（data + runtime）
            data_token_dir = os.path.join(launcher.DATA_DIR, "codex_tokens")
            runtime_token_dir = os.path.join(launcher.RUNTIME_DIR, "codex_tokens")
            os.makedirs(os.path.join(data_token_dir, "nested"), exist_ok=True)
            os.makedirs(runtime_token_dir, exist_ok=True)
            with open(os.path.join(data_token_dir, "a.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            with open(os.path.join(data_token_dir, "nested", "b.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            with open(os.path.join(runtime_token_dir, "c.json"), "w", encoding="utf-8") as f:
                f.write("{}")

            # 4) 运行时配置（异常退出可能残留）
            with open(launcher.RUNTIME_PROXY_CONFIG, "w", encoding="utf-8") as f:
                f.write("secret-key: 'x'\n")
            with open(launcher.RUNTIME_REGISTER_CONFIG, "w", encoding="utf-8") as f:
                f.write("{}\n")
            with open(os.path.join(launcher.RUNTIME_DIR, "_test_register_cfg.json"), "w", encoding="utf-8") as f:
                f.write("{\"duckmail_bearer\":\"dk_secret\",\"proxy\":\"http://127.0.0.1:7890\"}\n")

            r = launcher.secure_cleanup_outputs()
            self.assertTrue(r.get("ok"), r)

            # registered_accounts.txt 密码段应被覆盖为 ***
            with open(reg_fp, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("a@example.com----***----***----oauth=ok", content)

            # ak/rk 应被删除
            self.assertFalse(os.path.exists(os.path.join(launcher.DATA_DIR, "ak.txt")))
            self.assertFalse(os.path.exists(os.path.join(launcher.DATA_DIR, "rk.txt")))

            # token_json 应被删除
            self.assertFalse(os.path.exists(os.path.join(data_token_dir, "a.json")))
            self.assertFalse(os.path.exists(os.path.join(data_token_dir, "nested", "b.json")))
            self.assertFalse(os.path.exists(os.path.join(runtime_token_dir, "c.json")))

            # runtime 配置应被删除
            self.assertFalse(os.path.exists(launcher.RUNTIME_PROXY_CONFIG))
            self.assertFalse(os.path.exists(launcher.RUNTIME_REGISTER_CONFIG))
            self.assertFalse(os.path.exists(os.path.join(launcher.RUNTIME_DIR, "_test_register_cfg.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
