import re
import unittest

from helpers import isolated_launcher_fs

FIELDS = ("gateway_rate_limit_rpm", "gateway_max_concurrent_requests")


class TestGatewayLimitsSettingsRoundtrip(unittest.TestCase):
    """The limits must survive the path the UI actually uses.

    A setting that exists only in ``default_settings`` is a setting the user
    cannot change, so assert the input, the JS field list, the save/load
    round-trip, and the restart-required wiring together.
    """

    def test_fields_are_present_in_the_management_page(self):
        with isolated_launcher_fs() as (launcher, _td):
            html = launcher.HTML_PAGE
            match = re.search(r"const normal=\[(.*?)\],secrets=", html, re.S)
            self.assertIsNotNone(match, "could not locate the JS field list")
            js_fields = match.group(1)
            for field in FIELDS:
                self.assertIn(f'id="{field}"', html)
                self.assertIn(f'"{field}"', js_fields)

    def test_saved_string_values_become_effective_limits(self):
        with isolated_launcher_fs() as (launcher, _td):
            # Browser number inputs post strings, not ints.
            launcher.save_settings(
                {"gateway_rate_limit_rpm": "150", "gateway_max_concurrent_requests": "12"}
            )
            launcher._gateway_set_config_from_settings(launcher.load_settings())
            self.assertEqual(launcher._gateway_cfg["rate_limit_rpm"], 150)
            self.assertEqual(launcher._gateway_cfg["max_concurrent_requests"], 12)

    def test_changing_a_limit_requires_a_proxy_restart(self):
        with isolated_launcher_fs() as (launcher, _td):
            for field in FIELDS:
                self.assertIn(field, launcher.PROXY_RESTART_KEYS)

    def test_public_settings_expose_the_limits(self):
        with isolated_launcher_fs() as (launcher, _td):
            pub = launcher.get_settings_public()
            for field in FIELDS:
                self.assertIn(field, pub)


if __name__ == "__main__":
    unittest.main(verbosity=2)
