import threading
import unittest

from helpers import isolated_launcher_fs


class TestGatewayRateLimit(unittest.TestCase):
    def test_rpm_window_is_per_client_credential(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings({"gateway_rate_limit_rpm": 2})
            launcher._ratelimit_reset()

            self.assertEqual(launcher._ratelimit_check("Bearer A")[0], True)
            self.assertEqual(launcher._ratelimit_check("Bearer A")[0], True)

            allowed, retry_after = launcher._ratelimit_check("Bearer A")
            self.assertFalse(allowed)
            self.assertGreaterEqual(retry_after, 1)

            # A separate credential must not inherit A's exhausted window.
            self.assertEqual(launcher._ratelimit_check("Bearer B")[0], True)

    def test_rpm_zero_disables_the_limit(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings({"gateway_rate_limit_rpm": 0})
            launcher._ratelimit_reset()
            for _ in range(50):
                self.assertEqual(launcher._ratelimit_check("Bearer A")[0], True)
            self.assertEqual(launcher._ratelimit_stats()["rate_limited"], 0)

    def test_identity_never_exposes_the_raw_credential(self):
        with isolated_launcher_fs() as (launcher, _td):
            ident = launcher._ratelimit_identity("Bearer sk-secret-value-123456")
            self.assertNotIn("sk-secret-value-123456", ident)
            self.assertEqual(len(ident), 16)
            self.assertEqual(launcher._ratelimit_identity(""), "anonymous")

    def test_tracked_identities_are_bounded(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings({"gateway_rate_limit_rpm": 10})
            launcher._ratelimit_reset()
            for i in range(launcher._RATELIMIT_MAX_TRACKED + 200):
                launcher._ratelimit_check(f"Bearer key-{i}")
            self.assertLessEqual(
                launcher._ratelimit_stats()["tracked_clients"],
                launcher._RATELIMIT_MAX_TRACKED,
            )

    def test_concurrency_limit_blocks_then_recovers(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings({"gateway_max_concurrent_requests": 2})
            launcher._ratelimit_reset()

            self.assertTrue(launcher._concurrency_acquire())
            self.assertTrue(launcher._concurrency_acquire())
            self.assertFalse(launcher._concurrency_acquire())

            launcher._concurrency_release()
            self.assertTrue(launcher._concurrency_acquire())

            stats = launcher._ratelimit_stats()
            self.assertEqual(stats["concurrency_peak"], 2)
            self.assertEqual(stats["concurrency_rejected"], 1)

    def test_concurrency_counter_is_thread_safe(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings({"gateway_max_concurrent_requests": 0})
            launcher._ratelimit_reset()

            def worker():
                for _ in range(200):
                    if launcher._concurrency_acquire():
                        launcher._concurrency_release()

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(launcher._ratelimit_stats()["concurrency_active"], 0)

    def test_release_never_goes_negative(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._ratelimit_reset()
            launcher._concurrency_release()
            launcher._concurrency_release()
            self.assertEqual(launcher._ratelimit_stats()["concurrency_active"], 0)

    def test_invalid_or_cleared_limit_values_are_coerced(self):
        with isolated_launcher_fs() as (launcher, _td):
            # UI number inputs arrive as strings.
            launcher._gateway_set_config_from_settings(
                {"gateway_rate_limit_rpm": "120", "gateway_max_concurrent_requests": "8"}
            )
            self.assertEqual(launcher._gateway_cfg["rate_limit_rpm"], 120)
            self.assertEqual(launcher._gateway_cfg["max_concurrent_requests"], 8)

            # Garbage falls back to the shipped gate instead of raising.
            launcher._gateway_set_config_from_settings({"gateway_rate_limit_rpm": "abc"})
            self.assertEqual(
                launcher._gateway_cfg["rate_limit_rpm"], launcher._DEFAULT_RATE_LIMIT_RPM
            )

            # A cleared field means "off", not "restore the default".
            launcher._gateway_set_config_from_settings({"gateway_rate_limit_rpm": ""})
            self.assertEqual(launcher._gateway_cfg["rate_limit_rpm"], 0)

            # Negative values are not a valid way to disable the limit.
            launcher._gateway_set_config_from_settings({"gateway_max_concurrent_requests": -5})
            self.assertEqual(
                launcher._gateway_cfg["max_concurrent_requests"],
                launcher._DEFAULT_MAX_CONCURRENT_REQUESTS,
            )

    def test_limits_are_reported_in_status(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._gateway_set_config_from_settings({"gateway_rate_limit_rpm": 300})
            stats = launcher._ratelimit_stats()
            self.assertEqual(stats["rate_limit_rpm"], 300)
            self.assertEqual(stats["scope"], "per-client-credential")

    def test_defaults_ship_with_limits_enabled(self):
        with isolated_launcher_fs() as (launcher, _td):
            defaults = launcher.default_settings()
            self.assertGreater(defaults["gateway_rate_limit_rpm"], 0)
            self.assertGreater(defaults["gateway_max_concurrent_requests"], 0)

    def test_module_defaults_match_shipped_settings(self):
        """``/api/status`` reads ``_gateway_cfg`` even before the gateway starts.

        If the two default sources drift, the status endpoint reports a limit of
        0 while the effective setting is non-zero, which reads as "limiting is
        off" for a control that is actually enabled.
        """
        with isolated_launcher_fs() as (launcher, _td):
            defaults = launcher.default_settings()
            fresh = launcher._fresh_gateway_cfg_defaults()
            self.assertEqual(fresh["rate_limit_rpm"], defaults["gateway_rate_limit_rpm"])
            self.assertEqual(
                fresh["max_concurrent_requests"], defaults["gateway_max_concurrent_requests"]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
