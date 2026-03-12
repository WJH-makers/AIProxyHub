import unittest
from unittest import mock

from helpers import isolated_launcher_fs


class TestCacheAdvancedFeatures(unittest.TestCase):
    def test_gateway_config_includes_cache_limits_and_stale_windows(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings(
                {
                    "cache_enabled": True,
                    "cache_ttl_seconds": 3600,
                    "cache_ttl_jitter_seconds": 30,
                    "cache_max_entries": 10,
                    "cache_max_body_kb": 128,
                    "cache_max_total_mb": 64,
                    "cache_stale_while_revalidate_seconds": 5,
                    "cache_stale_if_error_seconds": 7,
                }
            )
            cfg = launcher._gateway_cfg
            self.assertEqual(int(cfg.get("ttl_seconds") or 0), 3600)
            self.assertEqual(int(cfg.get("ttl_jitter_seconds") or 0), 30)
            self.assertEqual(int(cfg.get("max_entries") or 0), 10)
            self.assertEqual(int(cfg.get("max_body_bytes") or 0), 128 * 1024)
            self.assertEqual(int(cfg.get("max_total_bytes") or 0), 64 * 1024 * 1024)
            self.assertEqual(int(cfg.get("stale_while_revalidate_seconds") or 0), 5)
            self.assertEqual(int(cfg.get("stale_if_error_seconds") or 0), 7)

    def test_cache_put_applies_ttl_jitter(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings(
                {
                    "cache_enabled": True,
                    "cache_ttl_seconds": 100,
                    "cache_ttl_jitter_seconds": 30,
                    "cache_max_entries": 10,
                    "cache_max_body_kb": 128,
                    "cache_max_total_mb": 0,
                }
            )
            launcher._cache_clear()
            with mock.patch.object(launcher.time, "time", return_value=1000.0), mock.patch.object(
                launcher.random, "randint", return_value=30
            ):
                ok = launcher._cache_put("k1", status=200, headers=[], body=b'{"usage":{"total_tokens":10}}')
            self.assertTrue(ok)
            e = launcher._cache_store.get("k1") or {}
            self.assertEqual(float(e.get("expires_at") or 0), 1070.0)  # 100 - 30 = 70

    def test_cache_lookup_classifies_stale_while_revalidate(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings(
                {
                    "cache_enabled": True,
                    "cache_ttl_seconds": 10,
                    "cache_ttl_jitter_seconds": 0,
                    "cache_stale_while_revalidate_seconds": 20,
                    "cache_stale_if_error_seconds": 0,
                    "cache_max_entries": 10,
                    "cache_max_body_kb": 128,
                }
            )
            launcher._cache_clear()
            with mock.patch.object(launcher.time, "time", return_value=100.0):
                launcher._cache_put("k1", status=200, headers=[], body=b"ok")

            with mock.patch.object(launcher.time, "time", return_value=115.0):
                kind, e = launcher._cache_lookup("k1")
            self.assertEqual(kind, "STALE")
            self.assertIsInstance(e, dict)

            with mock.patch.object(launcher.time, "time", return_value=131.0):
                kind2, e2 = launcher._cache_lookup("k1")
            self.assertEqual(kind2, "MISS")
            self.assertIsNone(e2)

    def test_cache_lookup_classifies_stale_if_error(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings(
                {
                    "cache_enabled": True,
                    "cache_ttl_seconds": 10,
                    "cache_ttl_jitter_seconds": 0,
                    "cache_stale_while_revalidate_seconds": 0,
                    "cache_stale_if_error_seconds": 20,
                    "cache_max_entries": 10,
                    "cache_max_body_kb": 128,
                }
            )
            launcher._cache_clear()
            with mock.patch.object(launcher.time, "time", return_value=100.0):
                launcher._cache_put("k1", status=200, headers=[], body=b"ok")

            with mock.patch.object(launcher.time, "time", return_value=115.0):
                kind, e = launcher._cache_lookup("k1")
            self.assertEqual(kind, "SIE")
            self.assertIsInstance(e, dict)

    def test_cache_max_total_bytes_evicts_lru(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings(
                {
                    "cache_enabled": True,
                    "cache_ttl_seconds": 3600,
                    "cache_ttl_jitter_seconds": 0,
                    "cache_max_entries": 10,
                    "cache_max_body_kb": 2048,
                    "cache_max_total_mb": 1,  # 1MB
                }
            )
            launcher._cache_clear()
            body1 = b"x" * (700 * 1024)
            body2 = b"y" * (700 * 1024)
            launcher._cache_put("k1", status=200, headers=[], body=body1)
            launcher._cache_put("k2", status=200, headers=[], body=body2)
            # 达到总上限后应驱逐最旧条目（k1）
            self.assertNotIn("k1", launcher._cache_store)
            self.assertIn("k2", launcher._cache_store)
            self.assertLessEqual(int(launcher._cache_bytes), 1 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)

