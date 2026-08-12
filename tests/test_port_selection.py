import unittest
from unittest import mock

from helpers import isolated_launcher_fs


class TestPortSelection(unittest.TestCase):
    def test_invalid_port_setting_falls_back_to_default(self):
        with isolated_launcher_fs() as (launcher, _td):
            self.assertEqual(launcher._coerce_port("not-a-port", launcher.DEFAULT_PROXY_PORT), 8317)
            self.assertEqual(launcher._coerce_port(70000, launcher.DEFAULT_PROXY_PORT), 8317)
            self.assertEqual(launcher._coerce_port(19091, launcher.DEFAULT_PROXY_PORT), 19091)

    def test_occupied_proxy_port_is_replaced_with_free_port(self):
        with isolated_launcher_fs() as (launcher, _td):
            with (
                mock.patch.object(launcher, "_proxy_reachable", return_value=False),
                mock.patch.object(launcher, "_is_port_free_local", return_value=False),
                mock.patch.object(launcher, "find_free_port", return_value=18318) as find_free,
            ):
                selected, reassigned = launcher._select_proxy_port(8317)

            self.assertEqual(selected, 18318)
            self.assertTrue(reassigned)
            find_free.assert_called_once_with(8318, host="127.0.0.1")

    def test_reachable_existing_proxy_port_is_kept(self):
        with isolated_launcher_fs() as (launcher, _td):
            with mock.patch.object(launcher, "_proxy_reachable", return_value=True), mock.patch.object(
                launcher, "_is_port_free_local"
            ) as is_free:
                selected, reassigned = launcher._select_proxy_port(8317)

            self.assertEqual(selected, 8317)
            self.assertFalse(reassigned)
            is_free.assert_not_called()


if __name__ == "__main__":
    unittest.main()
