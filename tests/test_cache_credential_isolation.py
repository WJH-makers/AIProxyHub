import unittest

from helpers import isolated_launcher_fs


class TestCacheCredentialIsolation(unittest.TestCase):
    def test_cache_always_keeps_client_credential_boundary(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings(
                {
                    "cache_enabled": True,
                    "cache_ttl_seconds": 3600,
                    "cache_max_entries": 10,
                    "cache_max_body_kb": 128,
                }
            )
            self.assertEqual(launcher._cache_auth_for_key("Bearer A"), "Bearer A")
            self.assertEqual(launcher._cache_auth_for_key("Bearer B"), "Bearer B")
            self.assertNotEqual(
                launcher._cache_key_for_request("POST", "/v1/responses", "Bearer A", b'{"model":"x"}'),
                launcher._cache_key_for_request("POST", "/v1/responses", "Bearer B", b'{"model":"x"}'),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
