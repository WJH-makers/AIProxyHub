import json
import os
import unittest

from helpers import isolated_launcher_fs


class TestDeleteAccountValidation(unittest.TestCase):
    def test_delete_account_accepts_simple_json_filename_and_deletes(self):
        with isolated_launcher_fs() as (launcher, _td):
            fn = "user@example.com.json"
            fp = os.path.join(launcher.AUTH_DIR, fn)
            with open(fp, "w", encoding="utf-8") as f:
                json.dump({"ok": True}, f)

            r = launcher.delete_account(fn)
            self.assertTrue(r.get("ok"), r)
            self.assertFalse(os.path.exists(fp))

    def test_delete_account_rejects_path_traversal_and_non_json(self):
        with isolated_launcher_fs() as (launcher, _td):
            bad = [
                "",
                None,
                123,
                "../a.json",
                "..\\a.json",
                "a/b.json",
                "a\\b.json",
                ".hidden.json",
                "a.txt",
                "a.json.bak",
            ]
            for v in bad:
                r = launcher.delete_account(v)  # type: ignore[arg-type]
                self.assertFalse(r.get("ok"), f"value={v!r} should be rejected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
