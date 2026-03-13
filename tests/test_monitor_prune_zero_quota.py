import json
import os
import unittest

from helpers import isolated_launcher_fs


class TestMonitorPruneZeroQuota(unittest.TestCase):
    def test_prune_respects_min_keep_and_deletes_only_usage_limit(self):
        with isolated_launcher_fs() as (launcher, _td):
            # 准备 3 个账号文件
            for fn in ["a.json", "b.json", "c.json"]:
                with open(os.path.join(launcher.AUTH_DIR, fn), "w", encoding="utf-8") as f:
                    json.dump({"ok": True}, f)

            q = {
                "ok": True,
                "accounts": [
                    {"available": False, "file": "a.json", "error_type": "usage_limit_reached"},
                    {"available": False, "file": "b.json", "error_type": "usage_limit_reached"},
                    {"available": False, "file": "c.json", "error_type": "usage_limit_reached"},
                    # 这个账号不可用，但不是额度耗尽 → 不应删除
                    {"available": False, "file": "should_not_delete.json", "error_type": "temporary_error"},
                ],
            }
            cfg = {
                "prune_zero_quota_enabled": True,
                "prune_only_usage_limit_reached": True,
                "min_keep_accounts": 1,
                "dry_run": False,
            }
            r = launcher._monitor_prune_zero_quota(q, cfg)
            self.assertTrue(r.get("ok"), r)
            # 3 个候选，但最少保留 1 个 → 最多删除 2 个
            self.assertEqual(r.get("candidates"), 2, r)
            self.assertEqual(r.get("deleted"), 2, r)
            self.assertFalse(os.path.exists(os.path.join(launcher.AUTH_DIR, "a.json")))
            self.assertFalse(os.path.exists(os.path.join(launcher.AUTH_DIR, "b.json")))
            self.assertTrue(os.path.exists(os.path.join(launcher.AUTH_DIR, "c.json")))

    def test_prune_dry_run_does_not_delete(self):
        with isolated_launcher_fs() as (launcher, _td):
            fn = "dry.json"
            fp = os.path.join(launcher.AUTH_DIR, fn)
            with open(fp, "w", encoding="utf-8") as f:
                json.dump({"ok": True}, f)

            q = {"ok": True, "accounts": [{"available": False, "file": fn, "status_message": "usage_limit_reached"}]}
            cfg = {
                "prune_zero_quota_enabled": True,
                "prune_only_usage_limit_reached": True,
                "min_keep_accounts": 0,
                "dry_run": True,
            }
            r = launcher._monitor_prune_zero_quota(q, cfg)
            self.assertTrue(r.get("ok"), r)
            self.assertEqual(r.get("candidates"), 1, r)
            self.assertEqual(r.get("deleted"), 0, r)
            self.assertTrue(os.path.exists(fp))


if __name__ == "__main__":
    unittest.main(verbosity=2)

