import contextlib
import os
import sys
import tempfile
from unittest import mock


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def import_launcher():
    """
    以“从项目根目录导入”的方式加载 launcher，避免测试运行目录不同导致 import 失败。
    """
    root = _project_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    import launcher  # noqa: F401
    return sys.modules["launcher"]


@contextlib.contextmanager
def isolated_launcher_fs():
    """
    为 launcher 打一个“隔离文件系统沙箱”，避免测试污染真实用户配置：
    - settings.json / data / auth / runtime 全部重定向到临时目录
    """
    launcher = import_launcher()
    with tempfile.TemporaryDirectory() as td:
        data_dir = os.path.join(td, "data")
        auth_dir = os.path.join(td, "auth")
        runtime_dir = os.path.join(td, "runtime")
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(auth_dir, exist_ok=True)
        os.makedirs(runtime_dir, exist_ok=True)

        patches = [
            mock.patch.object(launcher, "ROOT", td),
            mock.patch.object(launcher, "SETTINGS_FILE", os.path.join(td, "settings.json")),
            mock.patch.object(launcher, "DATA_DIR", data_dir),
            mock.patch.object(launcher, "AUTH_DIR", auth_dir),
            mock.patch.object(launcher, "RUNTIME_DIR", runtime_dir),
            mock.patch.object(launcher, "RUNTIME_PROXY_CONFIG", os.path.join(runtime_dir, "cli-proxy-api.runtime.yaml")),
            mock.patch.object(launcher, "RUNTIME_REGISTER_CONFIG", os.path.join(runtime_dir, "register.runtime.json")),
        ]
        for p in patches:
            p.start()

        try:
            yield launcher, td
        finally:
            for p in reversed(patches):
                p.stop()

