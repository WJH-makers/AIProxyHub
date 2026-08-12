import os
import unittest
from unittest import mock
import urllib.error

from helpers import isolated_launcher_fs


class TestPortSelection(unittest.TestCase):
    def test_runtime_namespace_isolated_by_app_root(self):
        with isolated_launcher_fs() as (launcher, _td):
            first = launcher._runtime_dir_for_root(r"C:\AIProxyHub\one")
            second = launcher._runtime_dir_for_root(r"C:\AIProxyHub\two")

            self.assertNotEqual(first, second)
            self.assertEqual(len(os.path.basename(first)), 16)
            self.assertEqual(os.path.dirname(first), os.path.dirname(second))

    def test_proxy_probe_bypasses_system_proxy_and_keeps_auth_status(self):
        with isolated_launcher_fs() as (launcher, _td):
            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            opener = mock.Mock()
            opener.open.return_value = Response()
            with mock.patch("urllib.request.build_opener", return_value=opener) as build_opener:
                self.assertTrue(launcher._proxy_reachable("8317", timeout=0.4))
                build_opener.assert_called_once()
                self.assertEqual(build_opener.call_args.args[0].proxies, {})
                opener.open.assert_called_once()
                request, = opener.open.call_args.args
                self.assertEqual(request.full_url, "http://127.0.0.1:8317/v1/models")
                self.assertEqual(opener.open.call_args.kwargs["timeout"], 0.4)

                def raise_unauthorized(*args, **kwargs):
                    raise urllib.error.HTTPError(
                        request.full_url, 401, "unauthorized", {}, None
                    )

                opener.open.side_effect = raise_unauthorized
                self.assertTrue(launcher._proxy_reachable(8317))

    def test_launcher_probe_bypasses_system_proxy(self):
        with isolated_launcher_fs() as (launcher, _td):
            class Response:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            opener = mock.Mock()
            opener.open.return_value = Response()
            with mock.patch("urllib.request.build_opener", return_value=opener) as build_opener:
                self.assertTrue(launcher._probe_launcher_port(19090))
                build_opener.assert_called_once()
                self.assertEqual(build_opener.call_args.args[0].proxies, {})
                request, = opener.open.call_args.args
                self.assertEqual(request.full_url, "http://127.0.0.1:19090/api/status")
                self.assertEqual(opener.open.call_args.kwargs["timeout"], 0.35)

    def test_launcher_port_file_is_atomic_and_validated(self):
        with isolated_launcher_fs() as (launcher, _td):
            launcher._write_launcher_port_file(19123)
            self.assertEqual(launcher._read_launcher_port_file(), 19123)

            with open(launcher._launcher_port_file(), "w", encoding="utf-8") as f:
                f.write("not-a-port")
            self.assertEqual(launcher._read_launcher_port_file(), 0)

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
