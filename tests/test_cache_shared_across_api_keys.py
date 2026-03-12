import unittest

from helpers import isolated_launcher_fs


class TestCacheSharedAcrossApiKeys(unittest.TestCase):
    def test_cache_auth_for_key_respects_share_flag(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings(
                {
                    "cache_enabled": True,
                    "cache_ttl_seconds": 3600,
                    "cache_max_entries": 10,
                    "cache_max_body_kb": 128,
                    "cache_shared_across_api_keys": False,
                }
            )
            self.assertFalse(bool(launcher._gateway_cfg.get("share_across_api_keys", False)))
            self.assertEqual(launcher._cache_auth_for_key("Bearer A"), "Bearer A")

            launcher._gateway_set_config_from_settings(
                {
                    "cache_enabled": True,
                    "cache_ttl_seconds": 3600,
                    "cache_max_entries": 10,
                    "cache_max_body_kb": 128,
                    "cache_shared_across_api_keys": True,
                }
            )
            self.assertTrue(bool(launcher._gateway_cfg.get("share_across_api_keys", False)))
            self.assertEqual(launcher._cache_auth_for_key("Bearer A"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)

