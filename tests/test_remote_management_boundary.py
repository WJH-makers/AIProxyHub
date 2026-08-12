import unittest
from unittest import mock

from helpers import isolated_launcher_fs


class TestRemoteManagementBoundary(unittest.TestCase):
    def test_remote_switch_is_visible_and_off_by_default(self):
        with isolated_launcher_fs() as (launcher, _td):
            self.assertFalse(launcher.default_settings()["allow_remote_proxy"])
            self.assertIn('id="allow_remote_proxy"', launcher.HTML_PAGE)

    def test_remote_proxy_is_disabled_by_default(self):
        with isolated_launcher_fs() as (launcher, _td):
            base = {
                "proxy": "",
                "proxy_host": "192.0.2.10",
                "allow_remote_proxy": False,
                "api_key": "client-key-with-enough-length-1234",
                "management_password": "management-secret-with-enough-length-1234",
            }
            with mock.patch.object(launcher, "_get_proxy_exe_path", return_value="fake.exe"), mock.patch(
                "os.path.exists", return_value=True
            ):
                ok, message = launcher.preflight("start_proxy", base)
            self.assertFalse(ok)
            self.assertIn("允许远程网关", message)

    def test_enabled_remote_proxy_requires_a_separate_admin_key(self):
        with isolated_launcher_fs() as (launcher, _td):
            base = {
                "proxy": "",
                "proxy_host": "192.0.2.10",
                "allow_remote_proxy": True,
                "api_key": "client-key-with-enough-length-1234",
                "management_password": "management-secret-with-enough-length-1234",
            }
            with mock.patch.object(launcher, "_get_proxy_exe_path", return_value="fake.exe"), mock.patch(
                "os.path.exists", return_value=True
            ):
                ok, message = launcher.preflight("start_proxy", base)
            self.assertFalse(ok)
            self.assertIn("独立的管理 API Key", message)

    def test_remote_proxy_accepts_distinct_strong_admin_key(self):
        with isolated_launcher_fs() as (launcher, _td):
            settings = {
                "proxy": "",
                "proxy_host": "192.0.2.10",
                "allow_remote_proxy": True,
                "api_key": "client-key-with-enough-length-1234",
                "admin_api_key": "admin-key-with-enough-length-5678",
                "management_password": "management-secret-with-enough-length-1234",
            }
            with mock.patch.object(launcher, "_get_proxy_exe_path", return_value="fake.exe"), mock.patch(
                "os.path.exists", return_value=True
            ):
                ok, message = launcher.preflight("start_proxy", settings)
            self.assertTrue(ok, message)


if __name__ == "__main__":
    unittest.main()
