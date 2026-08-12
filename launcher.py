"""AIProxyHub local-first Windows gateway manager.

The launcher owns the local proxy lifecycle, optional OpenAI-compatible gateway,
health/usage views, and a small management API. Provider accounts and API keys
must be supplied and authorized by the user; this project does not create or
harvest third-party consumer accounts.
"""

import http.server
import http.client
import argparse
import atexit
import base64
import ctypes
import gzip
import hashlib
import json
import os
import random
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque, OrderedDict
from ctypes import wintypes
from urllib.parse import urlsplit

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

# PyInstaller --noconsole / pythonw.exe 场景下，sys.stdout/sys.stderr 可能为 None，
# 直接 print() 会抛异常导致程序崩溃。这里兜底为写入空设备。
try:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
except Exception:
    pass


def _get_bundle_dir() -> str:
    """
    获取“只读资源目录”（源码运行=项目目录；PyInstaller=解包目录）。

    - PyInstaller onefile: sys._MEIPASS 指向临时解包目录
    - PyInstaller onedir: sys._MEIPASS 通常存在；否则使用可执行文件目录
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return SOURCE_DIR


def _dir_is_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".__wtest_{os.getpid()}_{int(time.time()*1000)}")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def _get_app_root() -> str:
    """
    获取“可写工作目录”（settings.json/data 等均写入此处）。

    规则：
    1) 若设置 AIPROXYHUB_HOME，则优先使用
    2) 若为 PyInstaller 打包运行：默认使用 LocalAppData\\AIProxyHub（更符合“安装版”软件的数据保存习惯）
       - 如需“便携版（数据与 EXE 同目录）”：在 exe 同目录创建一个空文件 `AIProxyHub.portable`
    3) 源码运行：使用项目目录
    """
    env_home = str(os.getenv("AIPROXYHUB_HOME", "") or "").strip()
    if env_home:
        return os.path.abspath(os.path.expanduser(env_home))

    # 重要：为避免“源码运行(ROOT=项目目录)”与“安装版运行(ROOT=LocalAppData)”并存时出现配置漂移，
    # 这里在源码运行场景下做一个向后兼容的优先级调整：
    # - 若用户曾运行过安装版（或显式把数据放进 LocalAppData），且 LocalAppData\\AIProxyHub\\settings.json 存在，
    #   则优先使用 LocalAppData\\AIProxyHub 作为 ROOT，使两种启动方式共享同一份 settings.json/api_key，
    #   避免出现“网关与 Codex 使用不同 key 导致 401 Invalid API key”的隐蔽问题。
    # - 若本机没有该 settings.json（纯源码首次运行/开发者常见），仍保持历史行为：使用项目目录作为 ROOT。
    if not getattr(sys, "frozen", False):
        try:
            base = os.getenv("LOCALAPPDATA")
            if not base:
                home_local = os.path.join(os.path.expanduser("~"), "AppData", "Local")
                if os.path.isdir(home_local):
                    base = home_local
            if base:
                local_root = os.path.join(base, "AIProxyHub")
                local_settings = os.path.join(local_root, "settings.json")
                if os.path.exists(local_settings):
                    return local_root
        except Exception:
            # 任何探测异常都不应影响源码运行
            pass

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        portable_flag = os.path.join(exe_dir, "AIProxyHub.portable")
        if os.path.exists(portable_flag) and _dir_is_writable(exe_dir):
            return exe_dir
        # Windows 上 LOCALAPPDATA 在某些非交互/受限环境里可能缺失（例如任务计划/某些 shell）。
        # 若直接回退到 APPDATA（Roaming），会导致 settings 路径漂移，引发“面板 key/配置不一致”。
        base = os.getenv("LOCALAPPDATA")
        if not base:
            # 兜底：显式拼出用户 LocalAppData（更符合“安装版”软件语义）
            home_local = os.path.join(os.path.expanduser("~"), "AppData", "Local")
            if os.path.isdir(home_local):
                base = home_local
        if not base:
            base = os.getenv("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "AIProxyHub")

    return SOURCE_DIR


BUNDLE_DIR = _get_bundle_dir()
ROOT = _get_app_root()
SETTINGS_FILE = os.path.join(ROOT, "settings.json")
# 兼容旧版本：历史上会在项目目录生成明文 config.yaml。
# 当前版本默认只在运行时临时生成，尽量不让敏感配置长期驻留在项目目录。
PROXY_CONFIG = os.path.join(ROOT, "config.yaml")  # legacy (不再默认写入)
AUTH_DIR = os.path.expanduser("~/.cli-proxy-api")
DATA_DIR = os.path.join(ROOT, "data")
APP_VERSION = "2.0.0-clean"
LAUNCHER_HOST = "127.0.0.1"
LAUNCHER_PORT = 9090
DEFAULT_PROXY_PORT = 8317

os.makedirs(DATA_DIR, exist_ok=True)


def _coerce_port(value, default: int) -> int:
    """Return a valid TCP port, falling back instead of raising on bad settings."""
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return int(default)
    if 1 <= port <= 65535:
        return port
    return int(default)


def _coerce_non_negative_int(value, default: int) -> int:
    """Return a non-negative int, falling back instead of raising on bad settings.

    An empty value means "explicitly cleared" and maps to 0 (limit off) rather
    than to ``default``; otherwise clearing the field in the UI would silently
    restore the default limit.
    """
    if value is None:
        return int(default)
    text = str(value).strip()
    if not text:
        return 0
    try:
        n = int(text)
    except ValueError:
        return int(default)
    return n if n >= 0 else int(default)


def _coerce_bool(value, default: bool = False) -> bool:
    """Parse persisted/UI booleans without treating the string ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return bool(default)


def _runtime_dir_for_root(root: str) -> str:
    """Return a private temp namespace for one AIProxyHub data root."""
    normalized = os.path.normcase(os.path.abspath(str(root)))
    namespace = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), "AIProxyHub", namespace)


def _get_proxy_exe_path() -> str:
    """
    获取 CLIProxyAPI 可执行文件路径。

    优先级：
    1) ROOT 目录下同名文件（便于用户替换/升级 cli-proxy-api.exe 而无需重打包）
    2) BUNDLE_DIR（PyInstaller 打包内置资源）
    """
    candidates = [
        os.path.join(ROOT, "cli-proxy-api.exe"),
        os.path.join(BUNDLE_DIR, "cli-proxy-api.exe"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[-1]

RUNTIME_DIR = _runtime_dir_for_root(ROOT)
RUNTIME_PROXY_CONFIG = os.path.join(RUNTIME_DIR, "cli-proxy-api.runtime.yaml")

SECRET_FIELDS = ("management_password", "api_key", "admin_api_key", "api_keys_extra")
# Keys used by pre-2.0 account-registration builds. They are deliberately
# discarded during settings load so legacy secrets are not returned or carried
# forward into the clean runtime configuration.
DEPRECATED_SETTINGS = {
    "duckmail_token",
    "total_accounts",
    "max_workers",
    "monitor_low_available_threshold_pct",
    "monitor_auto_register_enabled",
    "monitor_prune_zero_quota_enabled",
    "monitor_prune_only_usage_limit_reached",
    "monitor_prune_invalid_token_enabled",
    "monitor_min_keep_accounts",
    "monitor_dry_run",
    "store_passwords",
    "write_ak_rk",
    "keep_token_json",
    "keep_token_json_on_fail",
    "cache_shared_across_api_keys",
    # Do not retain legacy quota-switch controls.  AIProxyHub never changes
    # projects/models to work around an upstream usage limit.
    "quota_switch_project",
    "quota_switch_preview",
}
# 这些配置变更会影响 proxy（cli-proxy-api.exe 或透明网关）的行为；若代理正在运行，需要重启后生效。
PROXY_RESTART_KEYS = {
    "proxy",
    "proxy_port",
    "proxy_host",
    "allow_remote_proxy",
    "routing_strategy",
    "request_retry",
    "debug",
    "management_password",
    "api_key",
    "api_keys_extra",
    # 网关/缓存池
    "gateway_enabled",
    "cache_enabled",
    # 是否对 stream=true 的 SSE/WS 做缓存（命中时“快速回放”）；开关变化需要重启网关生效
    "cache_stream_enabled",
    "cache_vary_headers",
    "cache_ttl_seconds",
    "cache_max_entries",
    "cache_max_body_kb",
    "cache_max_total_mb",
    "cache_ttl_jitter_seconds",
    "cache_stale_while_revalidate_seconds",
    "cache_stale_if_error_seconds",
    "gateway_rate_limit_rpm",
    "gateway_max_concurrent_requests",
}

proxy_process = None
proxy_config_path = None
MAX_LOG = 800
log_lines = deque(maxlen=MAX_LOG)
_log_lock = threading.Lock()
_lock = threading.RLock()
_UI_API_TOKEN = secrets.token_urlsafe(24)

# ----------------------------
# 单实例（Windows EXE 默认启用）
# ----------------------------

_SINGLE_INSTANCE_MUTEX = None


def _is_frozen_exe() -> bool:
    return bool(getattr(sys, "frozen", False))


def _windows_message_box(text: str, title: str = "AIProxyHub"):
    """在 Windows 上弹一个信息框；失败时静默。"""
    if not _is_windows():
        return
    try:
        MB_OK = 0x00000000
        MB_ICONINFORMATION = 0x00000040
        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), MB_OK | MB_ICONINFORMATION)
    except Exception:
        pass


def _single_instance_mutex_name() -> str:
    """
    生成单实例互斥体名（按 ROOT 分区）。

    说明：
    - 默认使用“全局单实例”（避免安装版/便携版/旧版本同时运行，导致端口冲突与管理密码不一致，从而出现黑框闪退/面板数据为空等误判）。
    - 若显式设置 AIPROXYHUB_HOME（例如冒烟脚本/隔离运行），则按 ROOT 分区允许多实例并存。
    """
    # 显式隔离：允许多实例并存（按 ROOT 分区）
    env_home = str(os.getenv("AIPROXYHUB_HOME", "") or "").strip()
    if env_home:
        root = os.path.abspath(str(ROOT or "")).lower().encode("utf-8", errors="ignore")
        h = hashlib.sha1(root).hexdigest()[:12]
        return f"Local\\AIProxyHub_{h}"
    # 默认：全局单实例
    return "Local\\AIProxyHub"


def _acquire_single_instance_mutex() -> bool:
    """
    尝试获取单实例互斥体。

    返回：
    - True: 本进程为唯一实例（或无法获取但 fail-open）
    - False: 已存在另一个实例
    """
    global _SINGLE_INSTANCE_MUTEX
    if not _is_windows():
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.restype = wintypes.DWORD

        name = _single_instance_mutex_name()
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            # 无法创建互斥体：保守 fail-open，避免把用户卡死
            return True
        _SINGLE_INSTANCE_MUTEX = handle
        # ERROR_ALREADY_EXISTS = 183
        return int(kernel32.GetLastError() or 0) != 183
    except Exception:
        return True


def _launcher_port_file() -> str:
    return os.path.join(ROOT, "launcher.port")


def _write_launcher_port_file(port: int):
    try:
        os.makedirs(ROOT, exist_ok=True)
        target = _launcher_port_file()
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(int(port)))
        os.replace(tmp, target)
    except Exception:
        pass


def _read_launcher_port_file() -> int:
    try:
        with open(_launcher_port_file(), "r", encoding="utf-8") as f:
            v = f.read().strip()
        port = int(v)
        return port if 1 <= port <= 65535 else 0
    except Exception:
        return 0


def _probe_launcher_port(port: int) -> bool:
    """检查指定端口是否存在正在运行的 launcher（用于单实例场景下的“打开已运行窗口”）。"""
    try:
        import urllib.request
        import urllib.error

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}/api/status",
            headers={"Connection": "close"},
        )
        with opener.open(request, timeout=0.35) as r:
            code = int(getattr(r, "status", 0) or 0)
        return 200 <= code < 300
    except urllib.error.HTTPError as e:
        # 只要能连上并返回 JSON/HTML，就说明该端口确实被占用（但不一定是 AIProxyHub）
        try:
            return int(getattr(e, "code", 0) or 0) in (200, 401, 403)
        except Exception:
            return False
    except Exception:
        return False


def _discover_existing_launcher_port(preferred: list[int]) -> int:
    """从一组候选端口中发现正在运行的 launcher 端口；找不到则返回 0。"""
    seen = set()
    for p in (preferred or []):
        try:
            p = int(p or 0)
        except Exception:
            p = 0
        if p <= 0 or p in seen:
            continue
        seen.add(p)
        if _probe_launcher_port(p):
            return p
    return 0

# ===== 透明网关（cache-front）+ 请求级缓存 =====
# 说明：
# - 默认关闭（gateway_enabled/cache_enabled 均为 False）
# - 开启后：网关占用对外端口(proxy_port)，CLIProxyAPI 改为内部端口监听
# - 网关对 /v1/responses / /v1/chat/completions（非 stream）可做去重缓存；命中缓存不会产生回源请求
gateway_server = None
gateway_thread = None
gateway_upstream_port = None
gateway_listen_host = None
gateway_listen_port = None

_cache_lock = threading.Lock()
_cache_store = OrderedDict()  # key -> dict(entry)
_cache_bytes = 0
_cache_hits = 0
_cache_stale_hits = 0
_cache_sie_hits = 0
_cache_misses = 0
_cache_stores = 0
_cache_evictions = 0
_cache_bypass = 0
_cache_inflight_waits = 0
# 估算：缓存命中“节省的 tokens”（仅统计真正 HIT 返回；STALE/SWR/SIE 可能仍会触发回源，不计入节省）。
_cache_saved_tokens = 0
# 估算：从缓存返回给客户端的 tokens（HIT/STALE/SIE 均计入；best-effort）
_cache_served_tokens = 0
_cache_swr_refreshes = 0
_cache_swr_refresh_errors = 0
_cache_inflight = {}  # key -> threading.Event

# ---- 网关限流：per-client 凭据 RPM + 全局并发 ----
# 目的：网关持有的是用户自己的上游额度，任何一个客户端（或跑飞的循环）都不应该
# 独占它。这里只做本地闸门，不做账号轮换，也不试图绕过任何上游限制。
_ratelimit_lock = threading.Lock()
_ratelimit_hits = OrderedDict()  # identity -> deque[monotonic timestamps]
_ratelimit_rejects = 0
_concurrency_active = 0
_concurrency_peak = 0
_concurrency_rejects = 0
# 上限跟踪身份数，避免被大量伪造 Authorization 头撑爆内存
_RATELIMIT_MAX_TRACKED = 1024

_gateway_cfg = {
    "cache_enabled": False,
    "cache_stream_enabled": False,
    "ttl_seconds": 3600,
    "ttl_jitter_seconds": 0,
    "max_entries": 200,
    "max_body_bytes": 512 * 1024,
    "max_total_bytes": 0,
    "stale_while_revalidate_seconds": 0,
    "stale_if_error_seconds": 0,
}
# 闸门默认值只有一处事实源（default_settings）。网关未启动时 /api/status 也会读
# _gateway_cfg，若这里写 0 会让人误判限流是关闭的。
_DEFAULT_RATE_LIMIT_RPM = 600
_DEFAULT_MAX_CONCURRENT_REQUESTS = 64


def _fresh_gateway_cfg_defaults() -> dict:
    return {
        "rate_limit_rpm": _DEFAULT_RATE_LIMIT_RPM,
        "max_concurrent_requests": _DEFAULT_MAX_CONCURRENT_REQUESTS,
    }


_gateway_cfg.update(_fresh_gateway_cfg_defaults())


from launcher_security import _REDACT_PATTERNS, _redact_text

def log(msg):
    ts = time.strftime("%H:%M:%S")
    msg = _redact_text(msg)
    entry = f"[{ts}] {msg}"
    with _log_lock:
        log_lines.append(entry)
    try:
        print(entry)
    except Exception:
        # windowed 子系统或 stdout 被重定向异常时，仍保证 UI 内日志可用
        pass


def _tail_log_lines(contains: str, n: int = 12) -> list[str]:
    """
    从内存日志里取最后 N 行（仅用于错误提示/诊断，不用于持久化）。

    - contains: 仅保留包含该子串的行（例如 "[PROXY]"）
    - n: 返回行数上限
    """
    try:
        n = int(n or 0)
    except Exception:
        n = 12
    if n <= 0:
        n = 12
    with _log_lock:
        lines = list(log_lines)
    if contains:
        lines = [x for x in lines if contains in str(x)]
    return lines[-n:]


# 兼容旧版弱默认值（仅用于识别与拦截，不再作为默认发放）
#
# 注意：旧版本曾出现过“很短的 sk- 形态 key”（非 OpenAI 官方 key）。
# 为避免把任何可能仍在使用的真实 key 以明文写入仓库，这里不再保留具体字符串，
# 仅通过“长度阈值”等规则把弱 key 拦下（尤其是非本机回环监听时）。
LEGACY_DEFAULT_MANAGEMENT_PASSWORD = "cpa123456"


def _generate_default_management_password() -> str:
    """
    生成更安全的默认管理密码（首次启动/缺失字段时使用）。

    注意：返回值会在首次 load_settings() 时自动迁移为 DPAPI 加密落盘（dpapi:...）。
    """
    return secrets.token_urlsafe(18)  # ~24 字符


def _generate_default_api_key() -> str:
    """
    生成更安全的默认 External API Bearer token（首次启动/缺失字段时使用）。

    采用自定义前缀，避免误以为是 OpenAI 官方 sk- key。
    """
    return "aiph_" + secrets.token_urlsafe(24)  # ~32+ 字符


def _is_weak_secret(value: str, *, min_len: int, legacy_values=()) -> bool:
    v = str(value or "").strip()
    if not v:
        return True
    if legacy_values and v in set(legacy_values):
        return True
    return len(v) < int(min_len)


def _split_api_keys_text(text: str) -> list[str]:
    """
    把“多 API Key 文本”（每行一个/逗号分隔/空格分隔）拆成列表。

    约定：
    - 允许用户粘贴一大段（换行/逗号/空格混用）
    - 自动去空白、去重（保持顺序）
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = re.split(r"[\s,]+", raw)
    out: list[str] = []
    seen = set()
    for p in parts:
        k = str(p or "").strip()
        if not k:
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def _get_all_client_api_keys(s: dict) -> list[str]:
    """返回代理（/v1/*）允许的全部客户端 API Key（主 key + 额外 keys）。"""
    primary = str((s or {}).get("api_key", "") or "").strip()
    keys = [primary] if primary else []
    for k in _split_api_keys_text((s or {}).get("api_keys_extra", "")):
        if k and k not in keys:
            keys.append(k)
    return keys


def default_settings():
    return {
        "proxy": "http://127.0.0.1:7890",
        # 安全默认：首次启动不再发放固定弱口令/弱 key，改为强随机并自动 DPAPI 加密落盘
        "management_password": _generate_default_management_password(),
        # 代理（/v1/*）客户端使用的 API Key（默认单 key；可在 api_keys_extra 中追加多把 key）
        "api_key": _generate_default_api_key(),
        # 管理/运维 External API（/api/ext/*）使用的 Bearer token。
        # - 为空：为兼容历史行为，External API 仍使用 api_key
        # - 非空：External API 改为只接受 admin_api_key（推荐：与客户端 key 分离）
        "admin_api_key": "",
        # 额外客户端 API Keys（每行一个），用于“团队每人一把 key”。
        # 注意：该字段属于敏感信息（DPAPI 加密落盘）；修改后需要重启代理生效。
        "api_keys_extra": "",
        "proxy_port": DEFAULT_PROXY_PORT,
        "proxy_host": "127.0.0.1",
        "allow_remote_proxy": False,
        "routing_strategy": "round-robin",
        "request_retry": 3,
        "debug": False,
        # ===== 健康监控（只读）=====
        # 监控只做服务可达性和用量摘要检查，不修改用户的 provider 凭据或认证文件。
        "monitor_enabled": False,
        # 检查间隔（秒）
        "monitor_interval_seconds": 60,
        # ===== 网关（高级，默认关闭） =====
        # 网关只做 OpenAI-compatible 转发/缓存；不会创建、轮换或共享消费者账号。
        "gateway_enabled": False,
        # 是否启用响应缓存（仅网关模式生效；默认关闭以避免意外缓存敏感提示词/结果）
        "cache_enabled": False,
        # 是否允许对 stream=true 的响应做缓存（SSE/WS）。
        # - 默认关闭：保持“流式传输只做转发、不落盘/不缓存”的直觉语义
        # - 开启后：对完全相同的请求可直接从缓存“快速回放”，显著降低重复任务的端到端耗时
        # 注意：这会把流式响应内容写入内存缓存池；仅建议在可信环境/明确接受缓存语义时开启。
        "cache_stream_enabled": False,
        # 缓存 key 的 vary 维度（逗号分隔的 header 名列表；大小写不敏感）。
        # - OpenAI-Project / OpenAI-Organization：避免不同账单项目/组织误命中同一缓存
        # - X-AIProxyHub-Cache-Group：可选“租户/任务组”隔离维度（客户端自行传递）
        "cache_vary_headers": "OpenAI-Project,OpenAI-Organization,X-AIProxyHub-Cache-Group",
        # 缓存 TTL（秒）
        "cache_ttl_seconds": 3600,
        # TTL 抖动（秒）：写入缓存时会从 TTL 中随机减去 [0, jitter]，避免大量条目同一时刻过期造成抖动。
        # - 0：关闭
        # - 建议：10~60
        "cache_ttl_jitter_seconds": 30,
        # 最大缓存条目数（LRU）
        "cache_max_entries": 200,
        # 单条缓存最大响应体大小（KB），避免大响应占满内存
        "cache_max_body_kb": 512,
        # 缓存总内存上限（MB）；达到上限会按 LRU 继续驱逐，避免高并发下缓存把内存打爆。
        # - 0：不限制（不推荐）
        # - 建议：64~512
        "cache_max_total_mb": 128,
        # stale-while-revalidate（秒）：条目过期后，在该窗口内仍可返回旧缓存，同时后台刷新。
        # 注意：后台刷新会产生“真实回源请求”（会消耗额度）；默认 0=关闭。
        "cache_stale_while_revalidate_seconds": 0,
        # stale-if-error（秒）：条目过期后，在该窗口内若回源失败/限流，可返回旧缓存兜底。
        # 默认 0=关闭。
        "cache_stale_if_error_seconds": 0,
        # ===== 网关限流（每个客户端凭据独立计算）=====
        # 网关花的是用户自己的上游额度，所以默认就带闸门：单个客户端 key 跑飞
        # （重试风暴、循环调用）不应该把额度一次性烧光。默认值取正常单机用量的
        # 十几倍，够宽松；0 = 关闭。
        "gateway_rate_limit_rpm": _DEFAULT_RATE_LIMIT_RPM,
        # 全局在途请求上限（所有凭据合计），防止并发把内存和上游连接打满；0 = 关闭。
        "gateway_max_concurrent_requests": _DEFAULT_MAX_CONCURRENT_REQUESTS,
    }


# ``save_settings`` calls ``load_settings`` to merge the current values before
# writing.  Both functions must use the same lock, so this has to be reentrant;
# a plain Lock deadlocks whenever the UI saves settings a second time.
_settings_lock = threading.RLock()


def load_settings():
    """
    读取 settings.json（若存在），并将敏感字段解密到内存中。

    注意：settings.json 中敏感字段会以 dpapi:... 形式存储（Windows DPAPI）。
    """
    with _settings_lock:
        merged = default_settings()
        saved = {}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f) or {}
            except Exception as e:
                log(f"[WARN] 读取 settings.json 失败，将使用默认配置: {e}")
                saved = {}
        merged.update(saved)

        # Remove settings owned by the retired account-registration workflow.
        # Rewriting the file below also removes any old plaintext temporary-mail
        # token instead of merely hiding it from the UI.
        removed_deprecated = False
        for key in DEPRECATED_SETTINGS:
            if key in merged:
                removed_deprecated = True
                merged.pop(key, None)

        # 解密敏感字段（内存中保持明文，落盘保持加密）
        needs_migrate = removed_deprecated
        for k in SECRET_FIELDS:
            v = merged.get(k, "")
            if isinstance(v, str) and v and not v.startswith("dpapi:"):
                needs_migrate = True
            merged[k] = _maybe_dpapi_decrypt(v)

        # 自动迁移：发现旧版明文 secret 时，自动转成 dpapi 存储，避免继续明文落盘
        if needs_migrate:
            try:
                _write_settings_encrypted(merged)
                log("[SYS] 已自动加密本地敏感配置（DPAPI）")
            except Exception as e:
                log(f"[WARN] 自动加密迁移失败（将继续使用内存配置）: {e}")

        return merged


def save_settings(s):
    """
    保存配置：对敏感字段使用 DPAPI 加密存储。

    兼容行为：当 UI 传入敏感字段为空字符串时，表示"保持不变"。
    """
    with _settings_lock:
        current = load_settings()
        merged = dict(current)

        # 普通字段直接覆盖；敏感字段为空则保持原值
        for k, v in (s or {}).items():
            if k in SECRET_FIELDS:
                if isinstance(v, str) and v.strip():
                    merged[k] = v.strip()
                else:
                    # 空值：保持不变
                    pass
            else:
                merged[k] = v

        _write_settings_encrypted(merged)
        return merged


def get_settings_public():
    """返回给前端的配置（默认不回传敏感明文）。"""
    s = load_settings()
    pub = dict(s)
    pub["version"] = APP_VERSION
    for k in SECRET_FIELDS:
        pub[k] = ""
        pub[f"{k}_set"] = bool(s.get(k))
    # 仅回传“统计信息”，不泄露密钥明文（用于 UI 展示：已保存/数量等）
    try:
        pub["api_keys_extra_count"] = int(len(_split_api_keys_text(s.get("api_keys_extra", ""))))
        pub["client_api_keys_count"] = int(len(_get_all_client_api_keys(s)))
    except Exception:
        pub["api_keys_extra_count"] = 0
        pub["client_api_keys_count"] = 0
    return pub


def _ensure_runtime_dir():
    os.makedirs(RUNTIME_DIR, exist_ok=True)


from launcher_security import _try_truncate_file, _schedule_delete_on_reboot, _safe_unlink

def _is_windows():
    return os.name == "nt"

from launcher_settings import _maybe_dpapi_encrypt, _maybe_dpapi_decrypt, _DATA_BLOB, _dpapi_encrypt_bytes, _dpapi_decrypt_bytes


def _write_settings_encrypted(plain_settings: dict):
    to_store = dict(plain_settings or {})
    for k in SECRET_FIELDS:
        to_store[k] = _maybe_dpapi_encrypt(to_store.get(k, ""))
    # 更稳健：原子写入，避免意外中断导致 settings.json 损坏
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(to_store, f, indent=2, ensure_ascii=False)
    os.replace(tmp, SETTINGS_FILE)


def _yaml_escape(s):
    """YAML 单引号转义：单引号变双单引号"""
    return "'" + str(s).replace("'", "''") + "'"


def generate_proxy_config(s, path: str = None):
    """生成 CLIProxyAPI 配置文件（运行时临时生成，默认写入临时目录）。"""
    _ensure_runtime_dir()
    path = path or RUNTIME_PROXY_CONFIG
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))
    api_keys = _get_all_client_api_keys(s)
    if not api_keys:
        api_keys = [str(s.get("api_key", "") or "").strip()]
    api_keys_yaml = "\n".join([f"  - {_yaml_escape(k)}" for k in api_keys if str(k or "").strip()])
    yaml = f'''host: {_yaml_escape(s.get('proxy_host','127.0.0.1'))}
port: {s['proxy_port']}

remote-management:
  allow-remote: false
  secret-key: {_yaml_escape(s['management_password'])}
  disable-control-panel: false

auth-dir: "~/.cli-proxy-api"

api-keys:
{api_keys_yaml}

debug: {str(s.get('debug', False)).lower()}

# 启用代理端用量统计聚合（CPA 控制面板“使用统计/usage events”依赖此开关）。
usage-statistics-enabled: true

proxy-url: {_yaml_escape(s['proxy'])}

request-retry: {s.get('request_retry', 3)}
max-retry-credentials: 3

quota-exceeded:
  # AIProxyHub never changes projects or model classes to work around an
  # upstream usage limit.  These values are intentionally hard-disabled.
  switch-project: false
  switch-preview-model: false

routing:
  strategy: {_yaml_escape(s.get('routing_strategy', 'fill-first'))}

oauth-model-alias:
  codex:
    # 兼容“模型名携带推理档位”的客户端配置（例如 gpt5.2-codex-high / gpt5.2-xhigh）。
    #
    # 约定：
    # - gpt5.2-codex-high  → gpt-5.2-codex（并通过 payload.override 固定为 high）
    # - gpt5.2-xhigh       → gpt-5.2（并通过 payload.override 固定为 xhigh）
    #
    # 备注：是否允许 codex 模型使用 xhigh 由上游决定；AIProxyHub 不在网关层强制降级推理档位，
    # 只对“别名模型”做固定覆盖，避免客户端误传导致语义漂移。
    - name: "gpt-5.2-codex"
      alias: "gpt-5.2-codex-full"
    # codex-high（两种写法：带/不带 gpt- 前缀分隔符）
    - name: "gpt-5.2-codex"
      alias: "gpt5.2-codex-high"
      fork: true
    - name: "gpt-5.2-codex"
      alias: "gpt-5.2-codex-high"
      fork: true
    # xhigh（路由到 openai 模型 gpt-5.2）
    - name: "gpt-5.2"
      alias: "gpt5.2-xhigh"
      fork: true
    - name: "gpt-5.2"
      alias: "gpt-5.2-xhigh"
      fork: true
    # 注意：不要把 gpt-5.2-codex 静默 alias 到 mini。
    # - 这会导致“模型名与实际模型不一致”，并引发 client 侧参数兼容问题（例如 text.verbosity）。
    # - 如需更快/更省的模型，请显式选择 gpt-5-codex-mini。

oauth-excluded-models:
  codex:
    - "gpt-5.1-codex-max"

payload:
  # 仅对“档位写进模型名”的别名做强制归一：
  # - gpt5.2-codex-high / gpt-5.2-codex-high 固定为 high，避免客户端误传 xhigh 导致 400。
  # - gpt5.2-xhigh / gpt-5.2-xhigh 固定为 xhigh，保证该别名语义稳定。
  override:
    - models:
        - name: "gpt5.2-codex-high"
        - name: "gpt-5.2-codex-high"
      params:
        "reasoning.effort": "high"
    - models:
        - name: "gpt5.2-xhigh"
        - name: "gpt-5.2-xhigh"
      params:
        "reasoning.effort": "xhigh"
'''
    with open(path, "w", encoding="utf-8") as f:
        f.write(yaml)


def _check_proxy_url_format(proxy_url: str):
    if not proxy_url:
        return True, ""
    if re.match(r"^(http|https|socks5)://", str(proxy_url).strip(), re.IGNORECASE):
        return True, ""
    return False, "代理地址格式不正确：应以 http:// / https:// / socks5:// 开头"


def _is_port_free_local(port: int, host: str = "127.0.0.1"):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((str(host or "127.0.0.1"), int(port)))
            return True
    except Exception:
        return False


def preflight(kind: str, s: dict):
    """最小预检：在启动代理前检查路径、密钥和监听边界。"""
    # 代理地址格式
    ok, msg = _check_proxy_url_format(s.get("proxy", ""))
    if not ok:
        return False, msg

    # proxy 相关
    if kind == "start_proxy":
        exe = _get_proxy_exe_path()
        if not os.path.exists(exe):
            return False, f"未找到代理可执行文件: {exe}"
        if not str(s.get("api_key", "")).strip():
            return False, "请先在“配置”页设置 API Key"
        if not str(s.get("management_password", "")).strip():
            return False, "请先在“配置”页设置 管理密码"

        # 更安全默认：当监听地址非本机回环时，强制用户修改弱默认值，避免局域网误暴露
        host = str(s.get("proxy_host", "127.0.0.1") or "").strip().lower()
        if host and host not in ("127.0.0.1", "localhost", "::1"):
            if not _coerce_bool(s.get("allow_remote_proxy", False)):
                return False, "非本机网关监听默认关闭：请显式启用“允许远程网关”并做好网络隔离"
            weak_mgmt = _is_weak_secret(
                s.get("management_password", ""),
                min_len=12,
                legacy_values=(LEGACY_DEFAULT_MANAGEMENT_PASSWORD,),
            )
            client_keys = _get_all_client_api_keys(s)
            weak_client_key = any(_is_weak_secret(k, min_len=16) for k in (client_keys or []))
            admin_key = str(s.get("admin_api_key", "") or "").strip()
            if not admin_key:
                return False, "监听地址非本地时必须设置独立的管理 API Key，不能复用客户端 API Key"
            weak_admin_key = bool(admin_key) and _is_weak_secret(
                admin_key,
                min_len=16,
                legacy_values=(),
            )
            mgmt = str(s.get("management_password", "") or "").strip()
            reused = False
            if mgmt and admin_key and mgmt == admin_key:
                reused = True
            if mgmt and any(mgmt == k for k in (client_keys or [])):
                reused = True
            if admin_key and any(admin_key == k for k in (client_keys or [])):
                reused = True
            if weak_mgmt or weak_client_key or weak_admin_key or reused:
                return False, "监听地址非本地时，管理密码/客户端 API Key/管理 API Key 过弱或重复：请设置强随机值（建议 ≥16 位），避免被局域网访问"

        # 端口冲突由 start_proxy 统一自动选择可用端口；预检不能因为旧配置
        # 或其它本机服务占用默认端口而直接阻断启动。

    return True, ""


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _gateway_set_config_from_settings(s: dict):
    """从 settings 生成网关配置（仅 start_proxy 时调用）。"""
    global _gateway_cfg
    cache_enabled = bool(s.get("cache_enabled", False))
    cache_stream_enabled = bool(s.get("cache_stream_enabled", False))
    vary_headers = [h.lower() for h in _split_api_keys_text(s.get("cache_vary_headers", "")) if str(h or "").strip()]
    # 永远支持 X-AIProxyHub-Cache-Group（客户端可选传递，用于“租户/任务组”隔离 cache namespace）
    if "x-aiproxyhub-cache-group" not in vary_headers:
        vary_headers.append("x-aiproxyhub-cache-group")
    ttl = int(s.get("cache_ttl_seconds", 3600) or 3600)
    ttl_jitter = int(s.get("cache_ttl_jitter_seconds", 0) or 0)
    max_entries = int(s.get("cache_max_entries", 200) or 200)
    max_body_kb = int(s.get("cache_max_body_kb", 512) or 512)
    max_total_mb = int(s.get("cache_max_total_mb", 0) or 0)
    swr = int(s.get("cache_stale_while_revalidate_seconds", 0) or 0)
    sie = int(s.get("cache_stale_if_error_seconds", 0) or 0)
    if ttl < 1:
        ttl = 1
    if ttl_jitter < 0:
        ttl_jitter = 0
    if max_entries < 0:
        max_entries = 0
    if max_body_kb < 1:
        max_body_kb = 1
    if max_total_mb < 0:
        max_total_mb = 0
    if swr < 0:
        swr = 0
    if sie < 0:
        sie = 0
    # UI 的 number 输入回传的是字符串，且可能为空/非法；限流值不合法时退回默认闸门，
    # 不要让一次错误输入把整个网关的启动打挂。
    rate_limit_rpm = _coerce_non_negative_int(
        s.get("gateway_rate_limit_rpm"), _DEFAULT_RATE_LIMIT_RPM
    )
    max_concurrent = _coerce_non_negative_int(
        s.get("gateway_max_concurrent_requests"), _DEFAULT_MAX_CONCURRENT_REQUESTS
    )
    _gateway_cfg = {
        "cache_enabled": cache_enabled,
        "cache_stream_enabled": cache_stream_enabled,
        "ttl_seconds": ttl,
        "ttl_jitter_seconds": ttl_jitter,
        "max_entries": max_entries,
        "max_body_bytes": max_body_kb * 1024,
        "max_total_bytes": max_total_mb * 1024 * 1024,
        "stale_while_revalidate_seconds": swr,
        "stale_if_error_seconds": sie,
        "vary_headers": vary_headers,
        "rate_limit_rpm": rate_limit_rpm,
        "max_concurrent_requests": max_concurrent,
    }


def _cache_clear():
    global _cache_store, _cache_bytes, _cache_hits, _cache_stale_hits, _cache_sie_hits, _cache_misses, _cache_stores, _cache_evictions, _cache_bypass, _cache_inflight_waits, _cache_saved_tokens, _cache_served_tokens, _cache_swr_refreshes, _cache_swr_refresh_errors
    with _cache_lock:
        _cache_store.clear()
        _cache_bytes = 0
        _cache_hits = 0
        _cache_stale_hits = 0
        _cache_sie_hits = 0
        _cache_misses = 0
        _cache_stores = 0
        _cache_evictions = 0
        _cache_bypass = 0
        _cache_inflight_waits = 0
        _cache_saved_tokens = 0
        _cache_served_tokens = 0
        _cache_swr_refreshes = 0
        _cache_swr_refresh_errors = 0
        _cache_inflight.clear()


def _cache_stats():
    with _cache_lock:
        total = int(_cache_hits + _cache_stale_hits + _cache_sie_hits + _cache_misses) or 0
        hit_ratio = round((_cache_hits / total) * 100, 1) if total else 0.0
        serve_ratio = round(((_cache_hits + _cache_stale_hits + _cache_sie_hits) / total) * 100, 1) if total else 0.0
        return {
            "enabled": bool(_gateway_cfg.get("cache_enabled", False)),
            "stream_enabled": bool(_gateway_cfg.get("cache_stream_enabled", False)),
            "isolation": "per-client-credential",
            "hits": int(_cache_hits),
            "stale_hits": int(_cache_stale_hits),
            "stale_if_error_hits": int(_cache_sie_hits),
            "misses": int(_cache_misses),
            "hit_ratio_pct": hit_ratio,
            "served_ratio_pct": serve_ratio,
            # 估算节省：命中=节省 1 次回源；tokens 来自缓存写入时提取的 usage.total_tokens
            "saved_requests": int(_cache_hits),
            "saved_tokens": int(_cache_saved_tokens),
            "served_tokens": int(_cache_served_tokens),
            "swr_refreshes": int(_cache_swr_refreshes),
            "swr_refresh_errors": int(_cache_swr_refresh_errors),
            "stores": int(_cache_stores),
            "evictions": int(_cache_evictions),
            "bypass": int(_cache_bypass),
            "inflight_waits": int(_cache_inflight_waits),
            "entries": int(len(_cache_store)),
            "bytes": int(_cache_bytes),
            "ttl_seconds": int(_gateway_cfg.get("ttl_seconds", 0) or 0),
            "ttl_jitter_seconds": int(_gateway_cfg.get("ttl_jitter_seconds", 0) or 0),
            "max_entries": int(_gateway_cfg.get("max_entries", 0) or 0),
            "max_body_bytes": int(_gateway_cfg.get("max_body_bytes", 0) or 0),
            "max_total_bytes": int(_gateway_cfg.get("max_total_bytes", 0) or 0),
            "stale_while_revalidate_seconds": int(_gateway_cfg.get("stale_while_revalidate_seconds", 0) or 0),
            "stale_if_error_seconds": int(_gateway_cfg.get("stale_if_error_seconds", 0) or 0),
            "vary_headers": list(_gateway_cfg.get("vary_headers") or []),
        }


def _cache_auth_for_key(auth_header: str) -> str:
    """Return the credential boundary included in every cache key.

    Cache sharing across client credentials is intentionally unsupported. A
    gateway process is not a trust boundary, and identical prompts can still
    contain private source code or provider-specific policy context.
    """
    return str(auth_header or "")


def _ratelimit_identity(auth_header: str) -> str:
    """限流身份 = 客户端凭据的短摘要。

    只保留摘要，保证限流表和统计里不出现明文 key（日志脱敏之外的第二道防线）。
    """
    raw = str(auth_header or "").strip()
    if not raw:
        return "anonymous"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _ratelimit_check(auth_header: str) -> tuple[bool, int]:
    """滑动窗口 RPM 检查，返回 (是否放行, Retry-After 秒)。

    rpm <= 0 表示关闭。窗口按每个客户端凭据独立计算，凭据之间不共享配额。
    """
    global _ratelimit_rejects
    rpm = int(_gateway_cfg.get("rate_limit_rpm", 0) or 0)
    if rpm <= 0:
        return True, 0
    ident = _ratelimit_identity(auth_header)
    now = time.monotonic()
    with _ratelimit_lock:
        hits = _ratelimit_hits.get(ident)
        if hits is None:
            hits = deque()
            _ratelimit_hits[ident] = hits
        _ratelimit_hits.move_to_end(ident)
        while hits and (now - hits[0]) >= 60.0:
            hits.popleft()
        if len(hits) >= rpm:
            _ratelimit_rejects += 1
            return False, max(1, int(60.0 - (now - hits[0])) + 1)
        hits.append(now)
        # 当前身份刚被 move_to_end，淘汰最旧的不会误伤它
        while len(_ratelimit_hits) > _RATELIMIT_MAX_TRACKED:
            _ratelimit_hits.popitem(last=False)
        return True, 0


def _concurrency_acquire() -> bool:
    """占用一个全局并发槽。返回 False 表示已满（调用方回 503）。

    返回 True 时一定已计数，因此调用方必须在 finally 中配对调用
    ``_concurrency_release()``；这样即使运行中改了上限也不会泄漏计数。
    """
    global _concurrency_active, _concurrency_peak, _concurrency_rejects
    limit = int(_gateway_cfg.get("max_concurrent_requests", 0) or 0)
    with _ratelimit_lock:
        if limit > 0 and _concurrency_active >= limit:
            _concurrency_rejects += 1
            return False
        _concurrency_active += 1
        if _concurrency_active > _concurrency_peak:
            _concurrency_peak = _concurrency_active
        return True


def _concurrency_release():
    global _concurrency_active
    with _ratelimit_lock:
        if _concurrency_active > 0:
            _concurrency_active -= 1


def _ratelimit_reset():
    """清空限流窗口与计数（供测试和“清空缓存”类运维动作使用）。"""
    global _ratelimit_rejects, _concurrency_active, _concurrency_peak, _concurrency_rejects
    with _ratelimit_lock:
        _ratelimit_hits.clear()
        _ratelimit_rejects = 0
        _concurrency_active = 0
        _concurrency_peak = 0
        _concurrency_rejects = 0


def _ratelimit_stats():
    with _ratelimit_lock:
        return {
            "rate_limit_rpm": int(_gateway_cfg.get("rate_limit_rpm", 0) or 0),
            "max_concurrent_requests": int(_gateway_cfg.get("max_concurrent_requests", 0) or 0),
            "scope": "per-client-credential",
            "tracked_clients": int(len(_ratelimit_hits)),
            "rate_limited": int(_ratelimit_rejects),
            "concurrency_active": int(_concurrency_active),
            "concurrency_peak": int(_concurrency_peak),
            "concurrency_rejected": int(_concurrency_rejects),
        }


def _cache_key_for_request(method: str, path: str, auth: str, body_bytes: bytes, *, vary: list[tuple[str, str]] | None = None) -> str:
    """
    生成缓存 key：SHA256(method + path + auth + vary_headers + canonical_json(body)).

    设计目标：
    - 始终按客户端凭据隔离，避免不同调用方“读到别人的缓存”
    - 可选把某些 header（OpenAI-Project / OpenAI-Organization / X-AIProxyHub-Cache-Group 等）纳入 key，
      避免“不同租户/项目上下文”误命中同一缓存
    - 对 JSON 体做 canonicalize，避免空格/键顺序导致缓存失配
    """
    canonical = body_bytes or b""
    try:
        obj = json.loads((body_bytes or b"").decode("utf-8"))
        canonical = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except Exception:
        pass
    h = hashlib.sha256()
    h.update(str(method or "").upper().encode("utf-8"))
    h.update(b" ")
    h.update(str(path or "").encode("utf-8"))
    h.update(b"\n")
    h.update(str(auth or "").encode("utf-8"))
    h.update(b"\n")
    for hk, hv in (vary or []):
        h.update(b"H ")
        h.update(str(hk or "").lower().encode("utf-8"))
        h.update(b"=")
        h.update(str(hv or "").encode("utf-8"))
        h.update(b"\n")
    h.update(canonical)
    return h.hexdigest()


def _cache_lookup(key: str):
    """
    查询缓存条目并返回分类：

    返回：
    - ("HIT", entry)：新鲜缓存（expires_at > now）
    - ("STALE", entry)：过期但在 stale-while-revalidate 窗口内
    - ("SIE", entry)：过期但在 stale-if-error 窗口内
    - ("MISS", None)：未命中/硬过期（超过 hard_until，将被删除）

    说明：
    - 仅在 HIT 时计入 _cache_hits/_cache_saved_tokens/_cache_served_tokens
    - STALE/SIE 由调用方决定是否真正“返回旧缓存”，因此不在此处计数
    """
    global _cache_hits, _cache_misses, _cache_bytes, _cache_saved_tokens, _cache_served_tokens
    now = time.time()
    swr = int(_gateway_cfg.get("stale_while_revalidate_seconds", 0) or 0)
    sie = int(_gateway_cfg.get("stale_if_error_seconds", 0) or 0)

    with _cache_lock:
        entry = _cache_store.get(key)
        if not entry:
            _cache_misses += 1
            return "MISS", None

        hard_until = float(entry.get("hard_until", 0) or 0)
        if hard_until and hard_until <= now:
            # 硬过期：删除并视为 miss
            try:
                _cache_bytes -= int(entry.get("size", 0) or 0)
            except Exception:
                pass
            _cache_store.pop(key, None)
            _cache_misses += 1
            return "MISS", None

        expires_at = float(entry.get("expires_at", 0) or 0)
        if expires_at > now:
            _cache_store.move_to_end(key)
            _cache_hits += 1
            try:
                tokens = int(entry.get("tokens", 0) or 0)
                _cache_saved_tokens += tokens
                _cache_served_tokens += tokens
            except Exception:
                pass
            return "HIT", entry

        # 过期但仍可能“可用”（SWR/SIE 窗口）
        stale_until = float(entry.get("stale_until", expires_at) or expires_at)
        sie_until = float(entry.get("sie_until", expires_at) or expires_at)

        if swr > 0 and stale_until > now:
            _cache_store.move_to_end(key)
            return "STALE", entry

        if sie > 0 and sie_until > now:
            _cache_store.move_to_end(key)
            return "SIE", entry

        # 无窗口：删除并视为 miss（理论上 hard_until==expires_at 已在上方命中）
        try:
            _cache_bytes -= int(entry.get("size", 0) or 0)
        except Exception:
            pass
        _cache_store.pop(key, None)
        _cache_misses += 1
        return "MISS", None


def _extract_usage_total_tokens(body: bytes) -> int:
    """
    best-effort：从 OpenAI 兼容响应体中提取 usage.total_tokens，用于估算缓存命中节省的 tokens。

    支持两类常见返回：
    - Responses API: {"usage": {"total_tokens": 123, ...}, ...}
    - Chat Completions: {"usage": {"total_tokens": 123, ...}, ...}
    """
    try:
        obj = json.loads((body or b"").decode("utf-8"))
        if not isinstance(obj, dict):
            return 0
        usage = obj.get("usage")
        if isinstance(usage, dict):
            v = usage.get("total_tokens")
            try:
                return int(v or 0)
            except Exception:
                return 0
    except Exception:
        return 0
    return 0


def _cache_put(key: str, *, status: int, headers: list, body: bytes):
    """写入缓存（LRU + TTL + TTL jitter + max_entries + max_body_bytes + max_total_bytes）。"""
    global _cache_bytes, _cache_stores, _cache_evictions
    if not bool(_gateway_cfg.get("cache_enabled", False)):
        return False

    max_entries = int(_gateway_cfg.get("max_entries", 0) or 0)
    if max_entries <= 0:
        return False

    max_body_bytes = int(_gateway_cfg.get("max_body_bytes", 0) or 0)
    body_len = int(len(body or b""))
    if max_body_bytes > 0 and body_len > max_body_bytes:
        return False

    max_total_bytes = int(_gateway_cfg.get("max_total_bytes", 0) or 0)
    if max_total_bytes > 0 and body_len > max_total_bytes:
        # 单条就超过总上限：直接不缓存（避免驱逐所有条目仍无法满足）
        return False

    ttl = int(_gateway_cfg.get("ttl_seconds", 0) or 0)
    if ttl <= 0:
        return False

    now = time.time()
    tokens = _extract_usage_total_tokens(body or b"")
    jitter = int(_gateway_cfg.get("ttl_jitter_seconds", 0) or 0)
    if jitter < 0:
        jitter = 0
    # 按设计：从 TTL 中随机减去 [0, jitter]，避免大量条目同一时刻过期
    if jitter > 0:
        try:
            jitter = min(jitter, max(0, ttl - 1))
            ttl = max(1, ttl - int(random.randint(0, jitter)))
        except Exception:
            pass

    expires_at = now + int(ttl)
    swr = int(_gateway_cfg.get("stale_while_revalidate_seconds", 0) or 0)
    sie = int(_gateway_cfg.get("stale_if_error_seconds", 0) or 0)
    if swr < 0:
        swr = 0
    if sie < 0:
        sie = 0
    stale_until = expires_at + int(swr)
    sie_until = expires_at + int(sie)
    hard_until = max(stale_until, sie_until, expires_at)

    entry = {
        "status": int(status or 0),
        "headers": list(headers or []),
        "body": body or b"",
        "size": body_len,
        "tokens": int(tokens or 0),
        "created_at": now,
        "expires_at": expires_at,
        "stale_until": stale_until,
        "sie_until": sie_until,
        "hard_until": hard_until,
    }

    with _cache_lock:
        old = _cache_store.get(key)
        if old:
            try:
                _cache_bytes -= int(old.get("size", 0) or 0)
            except Exception:
                pass
        _cache_store[key] = entry
        _cache_store.move_to_end(key)
        _cache_bytes += int(entry.get("size", 0) or 0)
        _cache_stores += 1

        # LRU 驱逐
        while len(_cache_store) > max_entries:
            _, victim = _cache_store.popitem(last=False)
            try:
                _cache_bytes -= int(victim.get("size", 0) or 0)
            except Exception:
                pass
            _cache_evictions += 1

        # 总内存上限驱逐（按 LRU）
        if max_total_bytes > 0:
            while _cache_store and int(_cache_bytes) > int(max_total_bytes):
                _, victim = _cache_store.popitem(last=False)
                try:
                    _cache_bytes -= int(victim.get("size", 0) or 0)
                except Exception:
                    pass
                _cache_evictions += 1

    return True


def _cache_inflight_begin(key: str):
    """返回 (event, is_leader)；用于防止 cache stampede。"""
    global _cache_inflight_waits
    with _cache_lock:
        ev = _cache_inflight.get(key)
        if ev:
            _cache_inflight_waits += 1
            return ev, False
        ev = threading.Event()
        _cache_inflight[key] = ev
        return ev, True


def _cache_inflight_end(key: str, ev: threading.Event):
    try:
        ev.set()
    except Exception:
        pass
    with _cache_lock:
        if _cache_inflight.get(key) is ev:
            _cache_inflight.pop(key, None)


# ----------------------------
# WebSocket helpers（/v1/responses WebSocket mode）
# ----------------------------

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept_key(sec_websocket_key: str) -> str:
    """计算 RFC6455 Sec-WebSocket-Accept。"""
    raw = (str(sec_websocket_key or "") + _WS_GUID).encode("utf-8", errors="ignore")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii", errors="ignore")


def _read_exact(reader, n: int) -> bytes:
    """从 reader 读取 n 字节；不足则抛 EOFError。"""
    n = int(n or 0)
    if n <= 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = reader.read(n - len(buf))
        if not chunk:
            raise EOFError("unexpected EOF while reading websocket frame")
        buf.extend(chunk)
    return bytes(buf)


def _ws_send_frame(writer, opcode: int, payload: bytes = b""):
    """发送单个 WebSocket frame（服务端 -> 客户端，不做 mask）。"""
    if payload is None:
        payload = b""
    payload = bytes(payload)
    ln = len(payload)

    header = bytearray()
    header.append(0x80 | (int(opcode) & 0x0F))  # FIN + opcode
    if ln <= 125:
        header.append(ln)
    elif ln <= 65535:
        header.append(126)
        header.extend(int(ln).to_bytes(2, "big"))
    else:
        header.append(127)
        header.extend(int(ln).to_bytes(8, "big"))

    writer.write(bytes(header))
    if payload:
        writer.write(payload)
    try:
        writer.flush()
    except Exception:
        pass


def _ws_send_text(writer, text: str):
    _ws_send_frame(writer, 0x1, str(text or "").encode("utf-8", errors="replace"))


def _ws_send_close(writer, code: int = 1000, reason: str = ""):
    payload = b""
    try:
        payload = int(code).to_bytes(2, "big") + str(reason or "").encode("utf-8", errors="replace")
    except Exception:
        payload = b""
    _ws_send_frame(writer, 0x8, payload)


def _ws_recv_frame(reader):
    """接收单个 WebSocket frame（客户端 -> 服务端，通常带 mask）。"""
    b1, b2 = _read_exact(reader, 2)
    fin = bool(b1 & 0x80)
    opcode = int(b1 & 0x0F)
    masked = bool(b2 & 0x80)
    ln = int(b2 & 0x7F)
    if ln == 126:
        ln = int.from_bytes(_read_exact(reader, 2), "big")
    elif ln == 127:
        ln = int.from_bytes(_read_exact(reader, 8), "big")

    # 防止 OOM：限制单帧 payload 最大 10MB（与 HTTP body 限制一致）
    if ln > 10 * 1024 * 1024:
        raise ValueError(f"WebSocket frame payload too large: {ln} bytes")

    mask = _read_exact(reader, 4) if masked else b""
    payload = _read_exact(reader, ln) if ln else b""
    if masked and payload:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return fin, opcode, payload


def _ws_recv_message(reader, writer):
    """
    接收一个“消息级”载荷（自动处理 ping/pong 与分片）。

    返回：(opcode, payload_bytes) 或 (None, b"") 表示对端关闭/无更多数据。
    - opcode: 0x1 text, 0x2 binary
    """
    msg_opcode = None
    chunks = []
    while True:
        try:
            fin, opcode, payload = _ws_recv_frame(reader)
        except EOFError:
            return None, b""
        except Exception:
            return None, b""

        # 控制帧
        if opcode == 0x8:  # close
            try:
                _ws_send_close(writer, 1000, "")
            except Exception:
                pass
            return None, b""
        if opcode == 0x9:  # ping
            try:
                _ws_send_frame(writer, 0xA, payload)
            except Exception:
                pass
            continue
        if opcode == 0xA:  # pong
            continue

        # 数据帧
        if opcode != 0x0:
            msg_opcode = opcode
        if payload:
            chunks.append(payload)
        if fin:
            break

    if msg_opcode not in (0x1, 0x2):
        # 仅支持 text/binary
        return None, b""
    return msg_opcode, b"".join(chunks)


def _iter_sse_data_strings(resp) -> "list[str]":
    """
    把上游 SSE（text/event-stream）解析为 data 字段字符串序列。

    说明：
    - OpenAI Responses streaming 的 data 行通常为单行 JSON（含 type 字段）。
    - 这里不依赖 event: 行，按空行分隔 event block。
    """
    buf = bytearray()

    def _emit_block(block: bytes):
        try:
            text = block.decode("utf-8", errors="replace")
        except Exception:
            return

        data_lines = []
        for line in text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return
        data = "\n".join(data_lines).strip()
        if data:
            yield data

    while True:
        try:
            chunk = resp.read(8192)
        except Exception:
            break
        if not chunk:
            break
        buf.extend(chunk)

        while True:
            i_lf = buf.find(b"\n\n")
            i_crlf = buf.find(b"\r\n\r\n")
            if i_lf == -1 and i_crlf == -1:
                break
            if i_crlf != -1 and (i_lf == -1 or i_crlf < i_lf):
                block = bytes(buf[:i_crlf])
                del buf[: i_crlf + 4]
            else:
                block = bytes(buf[:i_lf])
                del buf[: i_lf + 2]
            for data in _emit_block(block):
                yield data

    # 兜底：上游异常断开时可能没有以空行收尾，仍尝试解析最后一个 block
    if buf:
        for data in _emit_block(bytes(buf)):
            yield data


def _sanitize_responses_body_bytes(body: bytes) -> bytes:
    """
    对 /v1/responses 请求体做最小必要归一：
    - 移除 CLIProxyAPI 暂不支持的 tool 类型（例如 image_generation）
    - 若 tool_choice 指向被移除的工具则清掉，避免二次校验失败
    - 兼容“模型名携带推理档位”的别名（例如 gpt5.2-codex-high / gpt5.2-xhigh）
    """
    if not body:
        return body
    # 自动检测 gzip（magic: 1f 8b）或 zstd（magic: 28 b5 2f fd）并解压
    # Codex enable_request_compression=true 默认使用 zstd 压缩
    if len(body) >= 4 and body[0:4] == b'\x28\xb5\x2f\xfd':
        try:
            import zstandard
            dctx = zstandard.ZstdDecompressor()
            body = dctx.decompress(body, max_output_size=10 * 1024 * 1024)
        except ImportError:
            # zstandard 未安装，尝试用 zstd 模块
            try:
                import zstd
                raw = zstd.decompress(body)
                if len(raw) > 10 * 1024 * 1024:
                    return body  # 解压后超限则拒绝
                body = raw
            except ImportError:
                return body  # 无法解压则原样返回
        except Exception:
            return body
    elif len(body) >= 2 and body[0:2] == b'\x1f\x8b':
        try:
            body = gzip.decompress(body)
        except Exception:
            return body
    try:
        obj = json.loads(body.decode("utf-8", errors="replace"))
        if not isinstance(obj, dict):
            return body
        changed = False

        # ---- model alias / reasoning.effort 归一（尽量放在最前，便于后续缓存 key 稳定）----
        try:
            model = obj.get("model")
            if isinstance(model, str):
                m0 = model
                m = model.strip()
                # 兼容缺少分隔符的写法：gpt5.2 -> gpt-5.2
                # （部分路由器会在 provider 判定前就尝试解析 model；这里提前归一，避免 502 unknown provider）
                m = re.sub(r"^gpt5(?=\.)", "gpt-5", m)

                fixed_effort: str | None = None
                target_model: str | None = None

                # 仅对“别名语义明确”的后缀做强制归一
                # - *-codex-high -> *-codex + reasoning.effort=high
                # - *-xhigh      -> *        + reasoning.effort=xhigh
                if (m.startswith("gpt-5.") or m.startswith("gpt5.")) and m.endswith("-codex-high"):
                    target_model = m[: -len("-high")]
                    fixed_effort = "high"
                elif (m.startswith("gpt-5.") or m.startswith("gpt5.")) and m.endswith("-xhigh"):
                    target_model = m[: -len("-xhigh")]
                    fixed_effort = "xhigh"

                # 若只是缺少分隔符（gpt5.2 -> gpt-5.2）且不属于“档位别名”，也尽量归一，避免 provider 判定失败。
                if (not target_model) and m and m != m0:
                    obj["model"] = m
                    changed = True

                if target_model:
                    # 再次归一：若上面分支里仍残留 gpt5. 前缀，转成 gpt-5.
                    target_model = re.sub(r"^gpt5(?=\.)", "gpt-5", str(target_model))
                    if target_model and target_model != m0:
                        obj["model"] = target_model
                        changed = True

                if fixed_effort:
                    # Responses API 形态：reasoning: { effort: "high|xhigh|..." }
                    r = obj.get("reasoning")
                    if isinstance(r, dict):
                        if str(r.get("effort") or "") != fixed_effort:
                            r["effort"] = fixed_effort
                            obj["reasoning"] = r
                            changed = True
                    else:
                        obj["reasoning"] = {"effort": fixed_effort}
                        changed = True
        except Exception:
            # 归一失败不应阻断请求
            pass

        # ---- text.verbosity 兼容 ----
        # 现状：Codex 系模型（*-codex*）在部分上游仅支持 text.verbosity=medium。
        # Codex CLI 默认可能发送 low；若不处理，会产生 400 unsupported_value。
        # 策略：仅在“模型名包含 codex”时，把 low 归一为 medium（不影响 gpt-5.2 这类支持 low 的模型）。
        try:
            model_now = obj.get("model")
            text_obj = obj.get("text")
            if isinstance(model_now, str) and isinstance(text_obj, dict):
                v = text_obj.get("verbosity")
                if isinstance(v, str) and v.strip().lower() == "low":
                    if "codex" in model_now.lower():
                        text_obj["verbosity"] = "medium"
                        obj["text"] = text_obj
                        changed = True
        except Exception:
            # 兼容失败不应阻断请求
            pass

        # ---- responses_websockets_v2 兼容（Codex WS v2）----
        # Codex(v0.114.0+) 的 WS 形态会在顶层携带 `generate` 字段（用于 WS 协议自身的语义），
        # 但上游 CLIProxyAPI 的 OpenAI-compat `/v1/responses` 会把它当成“不支持的参数”直接 400。
        # 这里做最小可行兼容：在转发到上游前移除该字段。
        try:
            if "generate" in obj:
                obj.pop("generate", None)
                changed = True
        except Exception:
            pass

        tools = obj.get("tools")
        if isinstance(tools, list):
            new_tools = []
            removed = 0
            for t in tools:
                if isinstance(t, dict) and str(t.get("type") or "") == "image_generation":
                    removed += 1
                    continue
                new_tools.append(t)
            if removed > 0:
                obj["tools"] = new_tools
                tc = obj.get("tool_choice")
                if isinstance(tc, dict) and str(tc.get("type") or "") == "image_generation":
                    obj.pop("tool_choice", None)
                changed = True
        if changed:
            return json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except Exception:
        return body
    return body


def _make_gateway_handler(upstream_host: str, upstream_port: int):
    upstream_host = str(upstream_host or "127.0.0.1").strip() or "127.0.0.1"
    upstream_port = int(upstream_port or 0)

    class GatewayHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            # 降低小包延迟（SSE/WS 高频 flush 场景），best-effort：不影响非 TCP/异常环境
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass

        def log_message(self, *a):
            # 不把每个请求刷到 stdout，避免污染 AIProxyHub 日志
            return

        def do_GET(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def do_PUT(self):
            self._dispatch()

        def do_PATCH(self):
            self._dispatch()

        def do_DELETE(self):
            self._dispatch()

        def _send_throttled(self, status: int, message: str, err_type: str, retry_after: int):
            body = json.dumps(
                {"error": {"message": message, "type": err_type, "code": status}},
                ensure_ascii=False,
            ).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                if retry_after > 0:
                    self.send_header("Retry-After", str(int(retry_after)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass

        def _dispatch(self):
            """限流闸门 → _handle。

            并发槽先占后放（finally 配对），RPM 在槽内检查：被限流的请求也算一次
            在途，避免限流本身变成绕过并发上限的旁路。
            """
            if not _concurrency_acquire():
                self._send_throttled(
                    503,
                    "网关在途请求已达上限，请稍后重试（可在管理面板调整“全局并发上限”）。",
                    "server_overloaded",
                    1,
                )
                return
            try:
                allowed, retry_after = _ratelimit_check(self.headers.get("Authorization", "") or "")
                if not allowed:
                    self._send_throttled(
                        429,
                        "已超出该客户端凭据的每分钟请求上限（可在管理面板调整 RPM）。",
                        "rate_limit_exceeded",
                        retry_after,
                    )
                    return
                self._handle()
            finally:
                _concurrency_release()

        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except Exception:
                length = 0
            if length <= 0:
                return b""
            # 防御：避免异常大 body 把内存打爆（这里只是网关层限制；CLIProxyAPI 也有自己的限制）
            if length > 10 * 1024 * 1024:
                raise ValueError("请求体过大（>10MB），已拒绝")
            return self.rfile.read(length)

        def _proxy_upstream(self, *, body: bytes, cache_tag: str | None = None, capture_stream: bool = False):
            # 复制请求头（剔除 hop-by-hop；并重写 Host）
            req_headers = {}
            for k, v in (self.headers.items() or []):
                lk = str(k or "").lower()
                if lk in _HOP_BY_HOP_HEADERS:
                    continue
                if lk == "host":
                    continue
                # 让 upstream 返回“可缓存的稳定形态”：避免 gzip 等压缩导致不同客户端 Accept-Encoding 互相影响命中率
                if lk == "accept-encoding":
                    continue
                # 网关可能会对 body 做轻度改写（例如归一 reasoning.effort），
                # 若继续透传客户端的 Content-Length，会导致上游读取长度不一致而挂起/报错。
                if lk in ("content-length", "transfer-encoding"):
                    continue
                # body 已被 _sanitize 解压，不再是 gzip；剥离 Content-Encoding 避免上游按压缩方式解读
                if lk == "content-encoding":
                    continue
                # AIProxyHub 自定义控制头不需要转发给 upstream（避免污染其日志/行为）
                if lk.startswith("x-aiproxyhub-"):
                    continue
                req_headers[k] = v
            req_headers["Host"] = f"{upstream_host}:{upstream_port}"
            req_headers["Content-Length"] = str(len(body or b""))

            conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=600)
            try:
                try:
                    conn.request(self.command, self.path, body=body, headers=req_headers)
                    resp = conn.getresponse()
                except Exception as e:
                    # 上游不可用：返回 502，避免网关线程异常导致连接被动断开
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    msg = json.dumps({"error": f"上游不可用：{e}"}, ensure_ascii=False).encode("utf-8")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    try:
                        self.wfile.write(msg)
                    except Exception:
                        pass
                    return 502, [("Content-Type", "application/json")], msg
                status = int(getattr(resp, "status", 502) or 502)
                headers = list(resp.getheaders() or [])
                content_type = ""
                try:
                    for hk, hv in headers:
                        if str(hk).lower() == "content-type":
                            content_type = str(hv)
                            break
                except Exception:
                    pass

                # SSE/长连接：边读边转发（可选捕获用于 stream 缓存）
                if content_type.lower().startswith("text/event-stream"):
                    self.send_response(status)
                    for hk, hv in headers:
                        lk = str(hk or "").lower()
                        if lk in _HOP_BY_HOP_HEADERS:
                            continue
                        if lk in ("content-length", "transfer-encoding"):
                            continue
                        self.send_header(hk, hv)
                    self.send_header("X-AIProxyHub-Cache", str(cache_tag or "BYPASS"))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    cap = bytearray() if bool(capture_stream) else None
                    # 捕获上限：沿用网关缓存的单条上限；超过则停止捕获（仍继续转发）
                    max_body_bytes = int(_gateway_cfg.get("max_body_bytes", 0) or 0)
                    stream_ok = True
                    while True:
                        try:
                            chunk = resp.read(8192)
                        except Exception:
                            stream_ok = False
                            break
                        if not chunk:
                            break
                        if cap is not None:
                            if max_body_bytes <= 0 or (len(cap) + len(chunk) <= max_body_bytes):
                                cap.extend(chunk)
                            else:
                                cap = None
                        try:
                            self.wfile.write(chunk)
                        except Exception:
                            stream_ok = False
                            break
                        try:
                            self.wfile.flush()
                        except Exception:
                            stream_ok = False
                            break
                    # 仅当“确实成功转发完整 SSE”且未超上限时，才把流式内容返回给调用方用于写入缓存
                    if bool(capture_stream) and stream_ok and cap is not None:
                        return status, headers, bytes(cap)
                    return

                data = resp.read() or b""
                # 诊断：对 4xx/5xx 记录摘要，便于定位 “Codex 请求体不兼容导致 400” 等问题（log() 会脱敏）
                try:
                    if int(status) >= 400:
                        try:
                            snip = data.decode("utf-8", errors="replace")[:800]
                        except Exception:
                            snip = ""
                        summary = ""
                        try:
                            ro = json.loads((body or b"").decode("utf-8", errors="replace"))
                            if isinstance(ro, dict):
                                model_s = str(ro.get("model") or "")
                                keys_s = ",".join(sorted([str(k) for k in ro.keys()]))[:500]
                                tool_types = []
                                tools2 = ro.get("tools")
                                if isinstance(tools2, list):
                                    for t in tools2:
                                        if isinstance(t, dict):
                                            tt = str(t.get("type") or "").strip()
                                            if tt:
                                                tool_types.append(tt)
                                tool_types_s = ",".join(sorted(set(tool_types)))[:300]
                                summary = f"model={model_s} keys={keys_s} tool_types={tool_types_s}"
                        except Exception:
                            summary = ""
                        log(f"[GW][HTTP] upstream status={status} path={self.path} ct={content_type} {summary} body={snip}")
                        if int(status) in (429, 403) or "usage_limit" in snip.lower() or "insufficient_quota" in snip.lower():
                            try:
                                _wake_monitor()
                            except Exception:
                                pass
                except Exception:
                    pass
                self.send_response(status)
                for hk, hv in headers:
                    lk = str(hk or "").lower()
                    if lk in _HOP_BY_HOP_HEADERS:
                        continue
                    if lk in ("content-length", "transfer-encoding"):
                        continue
                    self.send_header(hk, hv)
                if cache_tag:
                    self.send_header("X-AIProxyHub-Cache", str(cache_tag))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return status, headers, data
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        def _proxy_models_with_aliases(self):
            """
            /v1/models 代理（buffered）：
            - 透传上游模型列表
            - 在“上游存在基础模型”时，注入 AIProxyHub 约定的别名模型（便于 Codex/其它客户端直接选择）

            说明：
            - 仅对返回 JSON 的 200 响应做 best-effort 注入；失败则原样透传。
            - 只注入“语义稳定”的别名（以及带 gpt- 分隔符的同义别名）：
              - gpt5.X-codex-high（指向 gpt-5.X-codex + reasoning.effort=high）
              - gpt5.X-xhigh（指向 gpt-5.X + reasoning.effort=xhigh）
            """
            # 复制请求头（剔除 hop-by-hop；并重写 Host）
            req_headers = {}
            for k, v in (self.headers.items() or []):
                lk = str(k or "").lower()
                if lk in _HOP_BY_HOP_HEADERS:
                    continue
                if lk == "host":
                    continue
                if lk == "accept-encoding":
                    continue
                if lk in ("content-length", "transfer-encoding"):
                    continue
                if lk.startswith("x-aiproxyhub-"):
                    continue
                req_headers[k] = v
            req_headers["Host"] = f"{upstream_host}:{upstream_port}"

            conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=30)
            try:
                try:
                    conn.request("GET", self.path, headers=req_headers)
                    resp = conn.getresponse()
                except Exception as e:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    msg = json.dumps({"error": f"上游不可用：{e}"}, ensure_ascii=False).encode("utf-8")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    try:
                        self.wfile.write(msg)
                    except Exception:
                        pass
                    return

                status = int(getattr(resp, "status", 502) or 502)
                headers = list(resp.getheaders() or [])
                data = resp.read() or b""

                out = data
                if int(status) == 200 and data:
                    try:
                        obj = json.loads(data.decode("utf-8", errors="replace"))
                        if isinstance(obj, dict) and isinstance(obj.get("data"), list):
                            data_list = obj.get("data") or []
                            existing_ids: set[str] = set()
                            for it in data_list:
                                if isinstance(it, dict) and it.get("id"):
                                    existing_ids.add(str(it["id"]))

                            def _add(mid: str):
                                if not mid or mid in existing_ids:
                                    return
                                data_list.append({"id": mid, "object": "model"})
                                existing_ids.add(mid)

                            # 仅当基础模型存在时才注入别名，避免客户端选择后直接 404/502
                            # 约定：仅对 gpt-5.* 做注入（其它模型不推断其推理档位语义）
                            for base_id in sorted(existing_ids):
                                sid = str(base_id)

                                # gpt-5.X-codex -> gpt5.X-codex-high / gpt-5.X-codex-high
                                m = re.match(r"^gpt-5\.(\d+)-codex$", sid)
                                if m:
                                    minor = str(m.group(1))
                                    _add(f"gpt5.{minor}-codex-high")
                                    _add(f"gpt-5.{minor}-codex-high")
                                    continue

                                # gpt-5.X -> gpt5.X-xhigh / gpt-5.X-xhigh
                                m = re.match(r"^gpt-5\.(\d+)$", sid)
                                if m:
                                    minor = str(m.group(1))
                                    _add(f"gpt5.{minor}-xhigh")
                                    _add(f"gpt-5.{minor}-xhigh")

                            obj["data"] = data_list
                            out = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    except Exception:
                        out = data

                self.send_response(status)
                for hk, hv in headers:
                    lk = str(hk or "").lower()
                    if lk in _HOP_BY_HOP_HEADERS:
                        continue
                    if lk in ("content-length", "transfer-encoding"):
                        continue
                    self.send_header(hk, hv)
                self.send_header("Content-Length", str(len(out or b"")))
                self.end_headers()
                try:
                    self.wfile.write(out or b"")
                except Exception:
                    pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        def _handle_responses_websocket(self, *, auth_header: str):
            """
            /v1/responses WebSocket mode（与 OpenAI 官方“WebSocket mode”一致）：
            - 客户端发送：{"type":"response.create","response":{...}}
            - 服务端返回：与 SSE streaming 相同的 event 模型（逐条 JSON）
            """
            ws_key = str(self.headers.get("Sec-WebSocket-Key", "") or "").strip()
            if not ws_key:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                msg = json.dumps({"error": "缺少 Sec-WebSocket-Key"}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                try:
                    self.wfile.write(msg)
                except Exception:
                    pass
                return

            accept = _ws_accept_key(ws_key)
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            # 若客户端请求了 subprotocol，尽量回显第一个（Codex 当前通常不需要）
            proto = str(self.headers.get("Sec-WebSocket-Protocol", "") or "").strip()
            if proto:
                try:
                    self.send_header("Sec-WebSocket-Protocol", proto.split(",")[0].strip())
                except Exception:
                    pass
            self.end_headers()
            self.close_connection = True

            try:
                log(f"[GW][WS] /v1/responses connected from {self.client_address[0]}:{self.client_address[1]}")
            except Exception:
                pass

            def _send_error(status: int, body_text: str):
                try:
                    _ws_send_text(
                        self.wfile,
                        json.dumps(
                            {"type": "error", "error": {"status": int(status), "message": str(body_text or "")}},
                            ensure_ascii=False,
                        ),
                    )
                except Exception:
                    pass

            while True:
                opcode, payload = _ws_recv_message(self.rfile, self.wfile)
                if opcode is None:
                    break
                # 只处理 text；binary 直接忽略
                if opcode != 0x1:
                    continue
                try:
                    text = payload.decode("utf-8", errors="replace")
                except Exception:
                    continue
                if not str(text or "").strip():
                    continue

                try:
                    msg = json.loads(text)
                except Exception:
                    _send_error(400, "invalid json")
                    continue
                if not isinstance(msg, dict):
                    _send_error(400, "invalid message shape")
                    continue

                mtype = str(msg.get("type") or "").strip()
                if mtype != "response.create":
                    # 当前仅实现 Codex 需要的 response.create（其余事件忽略即可）
                    continue

                # 兼容两种 WS message 形态：
                # 1) 旧形态（本项目早期实现/自测脚本）：{"type":"response.create","response":{...}}
                # 2) Codex(v0.114.0+) 实际形态：{"type":"response.create", ...其余字段与 HTTP /v1/responses body 基本一致...}
                resp_obj = msg.get("response")
                if isinstance(resp_obj, dict):
                    req_obj = dict(resp_obj)
                else:
                    # 去掉 type 字段后，剩余部分就是 /v1/responses 的请求体
                    req_obj = {k: v for k, v in msg.items() if k != "type"}
                    if not req_obj:
                        _send_error(400, "missing response object")
                        continue

                # WebSocket mode 本身不需要 stream/background 字段；但上游 SSE 需要 stream=true
                req_obj.pop("background", None)
                req_obj["stream"] = True

                try:
                    body = json.dumps(req_obj, ensure_ascii=False).encode("utf-8")
                except Exception:
                    _send_error(400, "response object not serializable")
                    continue
                body = _sanitize_responses_body_bytes(body)

                # ---- WebSocket stream 缓存（可选）----
                # 说明：
                # - WS mode 的 payload 本质上仍是 Responses SSE 事件流；这里缓存的是“逐条 JSON 事件（按行分隔）”。
                # - 命中缓存时会快速回放整条事件流（不会逐 token 实时等待上游），因此更适合“重复任务/重复压测”场景。
                cache_ws_enabled = bool(_gateway_cfg.get("cache_enabled", False)) and bool(
                    _gateway_cfg.get("cache_stream_enabled", False)
                )
                cache_key_ws = ""
                # WS singleflight（防止并发冷缓存时 stampede）：仅用于“同一个 WS message”的生命周期
                ws_inflight_key = ""
                ws_inflight_ev = None
                allow_read = False
                allow_write = False

                if cache_ws_enabled:
                    # 与 HTTP 路径保持一致：允许通过 X-AIProxyHub-Cache/Cache-Control 控制 WS 缓存语义
                    cache_ctl = str(self.headers.get("X-AIProxyHub-Cache", "") or "").strip().lower()
                    cache_cc = str(self.headers.get("Cache-Control", "") or "").strip().lower()
                    bypass_read = False
                    no_store = False
                    if cache_ctl in ("bypass", "off", "0"):
                        bypass_read = True
                        no_store = True
                    elif cache_ctl == "no-store" or ("no-store" in cache_cc):
                        no_store = True
                    elif cache_ctl in ("refresh", "no-cache", "revalidate") or ("no-cache" in cache_cc):
                        bypass_read = True

                    allow_read = not bypass_read
                    allow_write = not no_store

                if allow_read or allow_write:
                    try:
                        if allow_read or allow_write:
                            vary = []
                            try:
                                hm = {str(k).lower(): str(v) for k, v in (self.headers.items() or [])}
                                for hn in (_gateway_cfg.get("vary_headers") or []):
                                    ln = str(hn or "").strip().lower()
                                    if not ln:
                                        continue
                                    vary.append((ln, hm.get(ln, "")))
                            except Exception:
                                vary = []

                            cache_key_ws = _cache_key_for_request(
                                "POST",
                                "/v1/responses#ws",
                                _cache_auth_for_key(auth_header),
                                body,
                                vary=vary,
                            )

                            if allow_read:
                                kind, entry = _cache_lookup(cache_key_ws)
                                if kind == "HIT" and entry:
                                    raw = entry.get("body") or b""
                                    try:
                                        text = raw.decode("utf-8", errors="replace")
                                    except Exception:
                                        text = ""
                                    for line in (text.splitlines() or []):
                                        if not str(line or "").strip():
                                            continue
                                        try:
                                            _ws_send_text(self.wfile, line)
                                        except Exception:
                                            break
                                    continue

                            # WS singleflight：与 HTTP 路径对齐，避免多个连接并发相同请求时
                            # 在“冷缓存”阶段重复回源（更省额度、更稳）。仅当允许写入缓存时启用。
                            if allow_read and allow_write and cache_key_ws:
                                ev, is_leader = _cache_inflight_begin(cache_key_ws)
                                if not is_leader:
                                    try:
                                        ev.wait(timeout=90)
                                    except Exception:
                                        pass
                                    k2, e2 = _cache_lookup(cache_key_ws)
                                    if k2 == "HIT" and e2:
                                        raw = e2.get("body") or b""
                                        try:
                                            text = raw.decode("utf-8", errors="replace")
                                        except Exception:
                                            text = ""
                                        for line in (text.splitlines() or []):
                                            if not str(line or "").strip():
                                                continue
                                            try:
                                                _ws_send_text(self.wfile, line)
                                            except Exception:
                                                break
                                        continue
                                else:
                                    ws_inflight_key = cache_key_ws
                                    ws_inflight_ev = ev
                    except Exception:
                        # 任意缓存异常都应降级为直连回源，避免影响 WS 基本可用性
                        allow_read = False
                        allow_write = False
                        cache_key_ws = ""
                        ws_inflight_key = ""
                        ws_inflight_ev = None

                # 透传 Authorization 到 upstream（CLIProxyAPI 会负责鉴权）
                req_headers = {
                    "Host": f"{upstream_host}:{upstream_port}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "Content-Length": str(len(body or b"")),
                }
                if str(auth_header or "").strip():
                    req_headers["Authorization"] = str(auth_header)

                conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=600)
                try:
                    try:
                        conn.request("POST", "/v1/responses", body=body, headers=req_headers)
                        resp = conn.getresponse()
                    except Exception as e:
                        _send_error(502, f"upstream unavailable: {e}")
                        continue

                    status = int(getattr(resp, "status", 502) or 502)
                    content_type = ""
                    try:
                        for hk, hv in (resp.getheaders() or []):
                            if str(hk).lower() == "content-type":
                                content_type = str(hv)
                                break
                    except Exception:
                        content_type = ""

                    if not str(content_type).lower().startswith("text/event-stream"):
                        raw = b""
                        try:
                            raw = resp.read() or b""
                        except Exception:
                            raw = b""
                        # 诊断：记录上游非 SSE 的错误摘要（log() 会做通用脱敏，避免泄露密钥）
                        try:
                            try:
                                snip = raw.decode("utf-8", errors="replace")[:800]
                            except Exception:
                                snip = ""
                            summary = ""
                            try:
                                ro = json.loads((body or b"").decode("utf-8", errors="replace"))
                                if isinstance(ro, dict):
                                    model_s = str(ro.get("model") or "")
                                    keys_s = ",".join(sorted([str(k) for k in ro.keys()]))[:500]
                                    tool_types = []
                                    tools2 = ro.get("tools")
                                    if isinstance(tools2, list):
                                        for t in tools2:
                                            if isinstance(t, dict):
                                                tt = str(t.get("type") or "").strip()
                                                if tt:
                                                    tool_types.append(tt)
                                    tool_types_s = ",".join(sorted(set(tool_types)))[:300]
                                    summary = f"model={model_s} keys={keys_s} tool_types={tool_types_s}"
                            except Exception:
                                summary = ""
                            log(f"[GW][WS] upstream non-sse status={status} ct={content_type} {summary} body={snip}")
                            if int(status) in (429, 403) or "usage_limit" in snip.lower() or "insufficient_quota" in snip.lower():
                                try:
                                    _wake_monitor()
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        _send_error(status, raw.decode("utf-8", errors="replace"))
                        continue

                    # SSE -> WS：逐条转发 data JSON
                    cap = bytearray() if (allow_write and cache_key_ws) else None
                    max_body_bytes = int(_gateway_cfg.get("max_body_bytes", 0) or 0)
                    saw_completed = False
                    saw_failed = False
                    stream_ok = True
                    for data in _iter_sse_data_strings(resp):
                        # 先发给客户端（以“可用性”为主），缓存仅 best-effort
                        try:
                            _ws_send_text(self.wfile, data)
                        except Exception:
                            # 客户端断开/写失败：终止该 response；且不应写入缓存
                            stream_ok = False
                            break

                        if cap is not None:
                            try:
                                b = (str(data or "") + "\n").encode("utf-8")
                            except Exception:
                                b = b""
                            if not b:
                                continue
                            if max_body_bytes > 0 and (len(cap) + len(b) > max_body_bytes):
                                # 超限：停止捕获（仍继续转发）
                                cap = None
                            else:
                                cap.extend(b)

                        # best-effort：判断是否 completed/failed（只缓存 completed）
                        try:
                            obj = json.loads(str(data or ""))
                            if isinstance(obj, dict):
                                t = str(obj.get("type") or "")
                                if t == "response.completed" or t.endswith(".completed"):
                                    saw_completed = True
                                if t == "response.failed" or t.endswith(".failed"):
                                    saw_failed = True
                        except Exception:
                            pass

                    if (
                        allow_write
                        and cache_key_ws
                        and stream_ok
                        and cap is not None
                        and saw_completed
                        and (not saw_failed)
                        and int(status) == 200
                    ):
                        try:
                            _cache_put(cache_key_ws, status=200, headers=[("Content-Type", "application/json")], body=bytes(cap))
                        except Exception:
                            pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    # WS singleflight：无论成功/失败都要释放等待者，避免挂死
                    try:
                        if ws_inflight_key and ws_inflight_ev is not None:
                            _cache_inflight_end(ws_inflight_key, ws_inflight_ev)
                    except Exception:
                        pass

            try:
                _ws_send_close(self.wfile, 1000, "")
            except Exception:
                pass

        def _handle(self):
            global _cache_bypass, _cache_stale_hits, _cache_sie_hits, _cache_served_tokens, _cache_swr_refreshes, _cache_swr_refresh_errors
            try:
                body = self._read_body()
            except Exception as e:
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                msg = json.dumps({"error": f"网关拒绝请求：{e}"}, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return

            path_only = urlsplit(self.path).path
            auth = self.headers.get("Authorization", "") or ""

            # ---- WebSocket Upgrade 说明 ----
            # 管理 UI 里“WebSocket 认证(/v1/ws)”仅控制 /v1/ws 的鉴权（部分上游实现使用），
            # 与 /v1/responses 的 WebSocket mode 无关。
            # 本项目支持 /v1/responses 的 WebSocket mode（Codex 的 responses_websockets_v2 会用到）。
            # 若客户端误向 /v1/ws 发起 Upgrade，则在这里返回清晰错误信息避免误判。
            try:
                upgrade = str(self.headers.get("Upgrade", "") or "").strip().lower()
                connection = str(self.headers.get("Connection", "") or "").strip().lower()
                is_ws_upgrade = ("websocket" in upgrade) or (
                    "upgrade" in connection and "websocket" in upgrade
                )
            except Exception:
                is_ws_upgrade = False

            if is_ws_upgrade and path_only == "/v1/ws":
                self.send_response(400)
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                msg = json.dumps(
                    {
                        "error": (
                            "当前实例不支持 /v1/ws 的 WebSocket Upgrade（该路径仅用于部分上游的 WS 通道）。"
                            "如需让 Codex 使用 WebSocket，请对 /v1/responses 发起 Upgrade，并在 ~/.codex/config.toml 中启用："
                            "model_providers.OpenAI.supports_websockets=true 且 features.responses_websockets_v2=true。"
                            "（若更关注稳定/更低尾延迟，可保持 HTTP/SSE 配置为 false。）"
                        )
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                try:
                    self.wfile.write(msg)
                except Exception:
                    pass
                return

            if is_ws_upgrade and path_only == "/v1/responses":
                self._handle_responses_websocket(auth_header=auth)
                return

            # /v1/models：注入别名模型（best-effort，失败则退回原样透传）
            if (not is_ws_upgrade) and self.command.upper() == "GET" and path_only == "/v1/models":
                self._proxy_models_with_aliases()
                return

            # ---- 请求体归一（与缓存逻辑无关：即使只启用 gateway_enabled 也要生效）----
            # 背景：Codex CLI 会携带 tools 列表；部分网关/代理实现可能不支持某些 tool type（例如 image_generation）。
            # 策略：仅做最小必要改写 —— 移除不被 CLIProxyAPI 接受的 tool 类型，避免 400 直接失败。
            if self.command.upper() == "POST" and path_only == "/v1/responses" and body:
                body = _sanitize_responses_body_bytes(body)

            # 仅对 /v1/responses / /v1/chat/completions（非 stream）启用缓存；其它请求直接透传
            cache_candidate = (
                bool(_gateway_cfg.get("cache_enabled", False))
                and self.command.upper() == "POST"
                and path_only in ("/v1/responses", "/v1/chat/completions")
            )

            if cache_candidate:
                method = self.command
                full_path = self.path

                stream_flag = False
                try:
                    obj = json.loads((body or b"").decode("utf-8"))
                    if isinstance(obj, dict) and obj.get("stream") is True:
                        stream_flag = True
                except Exception:
                    pass

                cache_stream_enabled = bool(_gateway_cfg.get("cache_stream_enabled", False))
                if stream_flag and not cache_stream_enabled:
                    _cache_bypass += 1
                    self._proxy_upstream(body=body)
                    return

                # vary headers：避免不同项目/租户上下文误命中同一缓存
                vary = []
                try:
                    hm = {str(k).lower(): str(v) for k, v in (self.headers.items() or [])}
                    for hn in (_gateway_cfg.get("vary_headers") or []):
                        ln = str(hn or "").strip().lower()
                        if not ln:
                            continue
                        vary.append((ln, hm.get(ln, "")))
                except Exception:
                    vary = []

                key = _cache_key_for_request(method, full_path, _cache_auth_for_key(auth), body, vary=vary)
                cache_ctl = str(self.headers.get("X-AIProxyHub-Cache", "") or "").strip().lower()
                cache_cc = str(self.headers.get("Cache-Control", "") or "").strip().lower()
                bypass_read = False
                no_store = False
                # 约定（与浏览器 Cache-Control 语义保持一致，且对压测/排障更友好）：
                # - bypass/off/0：绕过读取 + 不写入（强制走回源）
                # - no-store：不写入，但允许读取已有缓存（如存在）
                # - refresh/no-cache/revalidate：绕过读取，但允许写入新结果（刷新缓存）
                if cache_ctl in ("bypass", "off", "0"):
                    bypass_read = True
                    no_store = True
                elif cache_ctl == "no-store" or ("no-store" in cache_cc):
                    no_store = True
                elif cache_ctl in ("refresh", "no-cache", "revalidate") or ("no-cache" in cache_cc):
                    bypass_read = True

                def _send_cached(entry: dict, tag: str):
                    data = entry.get("body") or b""
                    headers = entry.get("headers") or []
                    status = int(entry.get("status", 200) or 200)
                    is_sse = False
                    try:
                        for hk, hv in headers:
                            if str(hk or "").lower() == "content-type" and str(hv or "").lower().startswith("text/event-stream"):
                                is_sse = True
                                break
                    except Exception:
                        is_sse = False
                    self.send_response(status)
                    for hk, hv in headers:
                        lk = str(hk or "").lower()
                        if lk in _HOP_BY_HOP_HEADERS:
                            continue
                        if lk in ("content-length", "transfer-encoding"):
                            continue
                        self.send_header(hk, hv)
                    self.send_header("X-AIProxyHub-Cache", str(tag or "HIT"))
                    self.send_header("Content-Length", str(len(data)))
                    if is_sse:
                        # 重要：SSE 客户端（包括 Codex）通常以 EOF 作为“完成”信号。
                        # 缓存回放时我们一次性写入完整 body，因此必须显式 close 连接避免客户端挂起等待后续事件。
                        self.send_header("Connection", "close")
                    self.end_headers()
                    if is_sse:
                        self.close_connection = True
                    try:
                        self.wfile.write(data)
                        try:
                            self.wfile.flush()
                        except Exception:
                            pass
                    except Exception:
                        pass

                def _filtered_upstream_headers() -> dict:
                    # 与 _proxy_upstream 一致：剔除 hop-by-hop、Host、Accept-Encoding、X-AIProxyHub-*
                    req_headers = {}
                    for k, v in (self.headers.items() or []):
                        lk = str(k or "").lower()
                        if lk in _HOP_BY_HOP_HEADERS:
                            continue
                        if lk == "host":
                            continue
                        if lk == "accept-encoding":
                            continue
                        if lk.startswith("x-aiproxyhub-"):
                            continue
                        req_headers[k] = v
                    req_headers["Host"] = f"{upstream_host}:{upstream_port}"
                    return req_headers

                def _fetch_upstream_once(headers_snapshot: dict) -> tuple[int, list, bytes, str]:
                    """
                    仅用于缓存逻辑（SWR/SIE）：
                    - 读取完整 body（非 SSE）
                    - 不直接写回客户端
                    """
                    conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=600)
                    try:
                        conn.request(method, full_path, body=body, headers=headers_snapshot)
                        resp = conn.getresponse()
                        status = int(getattr(resp, "status", 502) or 502)
                        headers = list(resp.getheaders() or [])
                        content_type = ""
                        try:
                            for hk, hv in headers:
                                if str(hk).lower() == "content-type":
                                    content_type = str(hv)
                                    break
                        except Exception:
                            pass
                        if content_type.lower().startswith("text/event-stream"):
                            return status, headers, b"", "text/event-stream"
                        data = resp.read() or b""
                        return status, headers, data, content_type
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass

                if no_store:
                    _cache_bypass += 1
                    self._proxy_upstream(body=body, cache_tag="BYPASS")
                    return

                kind = "MISS"
                entry = None
                if not bypass_read:
                    kind, entry = _cache_lookup(key)

                if kind == "HIT" and entry:
                    _send_cached(entry, "HIT")
                    return

                # stale-while-revalidate：直接返回旧缓存（tag=STALE），同时后台刷新
                if kind == "STALE" and entry:
                    with _cache_lock:
                        _cache_stale_hits += 1
                        try:
                            _cache_served_tokens += int(entry.get("tokens", 0) or 0)
                        except Exception:
                            pass
                    _send_cached(entry, "STALE")

                    # 后台刷新（singleflight）
                    headers_snapshot = _filtered_upstream_headers()
                    ev, is_leader = _cache_inflight_begin(key)
                    if is_leader:
                        with _cache_lock:
                            _cache_swr_refreshes += 1

                        def _refresh():
                            global _cache_swr_refresh_errors
                            try:
                                status, headers, data, ctype = _fetch_upstream_once(headers_snapshot)
                                # 仅缓存成功响应；SSE 不缓存
                                if int(status) == 200 and not str(ctype or "").lower().startswith("text/event-stream"):
                                    _cache_put(key, status=status, headers=headers, body=data)
                            except Exception:
                                with _cache_lock:
                                    _cache_swr_refresh_errors += 1
                            finally:
                                _cache_inflight_end(key, ev)

                        threading.Thread(target=_refresh, daemon=True).start()
                    return

                # stale-if-error：尝试回源；若失败则返回旧缓存（tag=SIE）
                if kind == "SIE" and entry:
                    headers_snapshot = _filtered_upstream_headers()
                    ev, is_leader = _cache_inflight_begin(key)
                    if not is_leader:
                        ev.wait(timeout=90)
                        k2, e2 = _cache_lookup(key)
                        if k2 == "HIT" and e2:
                            _send_cached(e2, "HIT")
                            return
                        # leader 失败/超时：兜底返回旧缓存（仍在 SIE 窗口内才会走到此分支）
                        with _cache_lock:
                            _cache_sie_hits += 1
                            try:
                                _cache_served_tokens += int(entry.get("tokens", 0) or 0)
                            except Exception:
                                pass
                        _send_cached(entry, "SIE")
                        return

                    try:
                        status, headers, data, ctype = _fetch_upstream_once(headers_snapshot)
                        # SSE：_fetch_upstream_once 已完成一次上游请求并丢弃了 SSE body。
                        # 修复：直接使用旧缓存（SIE 语义已保证窗口内有效），不再二次回源。
                        if str(ctype or "").lower().startswith("text/event-stream"):
                            _cache_bypass += 1
                            if entry:
                                _send_cached(entry, "SIE")
                            else:
                                self._proxy_upstream(body=body, cache_tag="BYPASS")
                            return

                        status_i = int(status or 0)
                        is_error = (status_i == 0) or (status_i == 429) or (500 <= status_i <= 599) or (status_i == 502)
                        if status_i == 200:
                            # 回源成功：正常返回并更新缓存
                            self.send_response(status_i)
                            for hk, hv in headers:
                                lk = str(hk or "").lower()
                                if lk in _HOP_BY_HOP_HEADERS:
                                    continue
                                if lk in ("content-length", "transfer-encoding"):
                                    continue
                                self.send_header(hk, hv)
                            self.send_header("X-AIProxyHub-Cache", "MISS")
                            self.send_header("Content-Length", str(len(data or b"")))
                            self.end_headers()
                            self.wfile.write(data or b"")
                            _cache_put(key, status=status_i, headers=headers, body=data or b"")
                            return

                        if is_error:
                            with _cache_lock:
                                _cache_sie_hits += 1
                                try:
                                    _cache_served_tokens += int(entry.get("tokens", 0) or 0)
                                except Exception:
                                    pass
                            _send_cached(entry, "SIE")
                            return

                        # 非“可兜底的错误”：保持 upstream 原样返回（不缓存）
                        self.send_response(status_i or 502)
                        for hk, hv in headers:
                            lk = str(hk or "").lower()
                            if lk in _HOP_BY_HOP_HEADERS:
                                continue
                            if lk in ("content-length", "transfer-encoding"):
                                continue
                            self.send_header(hk, hv)
                        self.send_header("X-AIProxyHub-Cache", "MISS")
                        self.send_header("Content-Length", str(len(data or b"")))
                        self.end_headers()
                        self.wfile.write(data or b"")
                        return
                    except Exception:
                        # 网络异常：可兜底返回旧缓存
                        with _cache_lock:
                            _cache_sie_hits += 1
                            try:
                                _cache_served_tokens += int(entry.get("tokens", 0) or 0)
                            except Exception:
                                pass
                        _send_cached(entry, "SIE")
                        return
                    finally:
                        _cache_inflight_end(key, ev)

                # ===== MISS / REFRESH =====
                ev, is_leader = _cache_inflight_begin(key)
                if not is_leader:
                    # 等待 leader 回源并写入缓存
                    ev.wait(timeout=90)
                    k2, e2 = _cache_lookup(key)
                    if k2 == "HIT" and e2:
                        _send_cached(e2, "HIT")
                        return
                    # leader 失败/超时：降级为普通透传（不再强行等待）
                    _cache_bypass += 1
                    self._proxy_upstream(body=body, cache_tag="BYPASS")
                    return

                try:
                    capture = bool(stream_flag) and bool(_gateway_cfg.get("cache_stream_enabled", False))
                    r = self._proxy_upstream(
                        body=body,
                        cache_tag=("MISS" if bypass_read else "MISS"),
                        capture_stream=capture,
                    )
                    if isinstance(r, tuple) and len(r) == 3 and not no_store:
                        status, headers, data = r
                        # 仅缓存成功响应（避免把 401/429/5xx 缓存住）
                        if int(status) == 200:
                            _cache_put(key, status=status, headers=headers, body=data)
                finally:
                    _cache_inflight_end(key, ev)
                return

            # 默认透传
            self._proxy_upstream(body=body)

    return GatewayHandler


def start_gateway(listen_host: str, listen_port: int, upstream_port: int):
    """启动透明网关（cache-front）。"""
    global gateway_server, gateway_thread, gateway_upstream_port, gateway_listen_host, gateway_listen_port
    with _lock:
        if gateway_server is not None:
            return {"ok": True, "msg": "网关已在运行"}

        gateway_upstream_port = int(upstream_port or 0)
        gateway_listen_host = str(listen_host or "127.0.0.1").strip() or "127.0.0.1"
        gateway_listen_port = int(listen_port or 0)

        handler = _make_gateway_handler("127.0.0.1", gateway_upstream_port)
        # 提升并发下的连接排队能力（backlog），避免短时间突发连接导致拒绝/超时。
        # 注意：request_queue_size 必须在 listen() 之前生效；因此要用子类覆写类属性，
        # 仅在实例化后再赋值通常不会影响已监听 socket 的 backlog。
        class _GatewayServer(http.server.ThreadingHTTPServer):
            request_queue_size = 128
            # Windows 下 http.server.HTTPServer 默认会打开 SO_REUSEADDR，这会允许“多进程同时绑定同一端口”，
            # 造成流量被系统分流到不同实例（表现为偶发 401 / stream 断连 / 缓存命中异常等）。
            #
            # 这里显式关闭 reuse_address，并尽量启用 SO_EXCLUSIVEADDRUSE 防止端口共享。
            allow_reuse_address = False

            def server_bind(self):
                try:
                    if _is_windows() and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                except Exception:
                    pass
                return super().server_bind()

        gateway_server = _GatewayServer((gateway_listen_host, gateway_listen_port), handler)

        t = threading.Thread(target=gateway_server.serve_forever, daemon=True)
        gateway_thread = t
        t.start()

    log(f"[SYS] 透明网关已启动：{gateway_listen_host}:{gateway_listen_port} -> 127.0.0.1:{gateway_upstream_port}")
    return {"ok": True, "msg": "网关已启动"}


def stop_gateway():
    """停止透明网关（如果正在运行）。"""
    global gateway_server, gateway_thread, gateway_upstream_port, gateway_listen_host, gateway_listen_port
    srv = None
    with _lock:
        srv = gateway_server
        gateway_server = None
        gateway_thread = None
        gateway_upstream_port = None
        gateway_listen_host = None
        gateway_listen_port = None

    if srv is None:
        return {"ok": False, "msg": "网关未在运行"}

    try:
        srv.shutdown()
    except Exception:
        pass
    try:
        srv.server_close()
    except Exception:
        pass

    log("[SYS] 透明网关已停止")
    return {"ok": True, "msg": "网关已停止"}


def stream_output(proc, prefix):
    try:
        if proc.stdout is None:
            log(f"[{prefix}] 进程 stdout 不可用")
            return
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            log(f"[{prefix}] {line.rstrip()}")
    except Exception as e:
        log(f"[{prefix}] 输出流异常: {e}")
    finally:
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass


def _watch_process_cleanup(proc, kind: str, config_path: str):
    """等待子进程结束后清理运行时临时配置文件，避免敏感信息长期留在磁盘临时目录。"""
    try:
        proc.wait()
    except Exception:
        return

    # 记录退出（解决“黑框闪退但看不到原因”的问题：至少留下 exit code 线索）
    try:
        rc = int(getattr(proc, "returncode", 0) or 0)
    except Exception:
        rc = 0
    try:
        if kind == "proxy":
            log(f"[SYS] 代理进程已退出 exit={rc}")
    except Exception:
        pass

    _safe_unlink(config_path)

    # 清理全局引用（避免误判“仍在运行”）
    global proxy_process, proxy_config_path
    with _lock:
        if kind == "proxy" and proxy_process is proc:
            proxy_process = None
            if proxy_config_path == config_path:
                proxy_config_path = None


def start_proxy():
    global proxy_process
    global proxy_config_path
    # 说明：start_proxy 会尽量等待 CLIProxyAPI “模型与客户端加载完成”，
    # 避免客户端在启动瞬间调用 /v1/models 却拿到空列表（常见误判：以为“没有模型/无法使用”）。
    # 该等待只在本地短时间内轮询，超时则仍然返回“已启动”。

    s = None
    gw_enabled = False
    internal_port = None
    external_port = None

    with _lock:
        if (proxy_process and proxy_process.poll() is None) or (gateway_server is not None):
            # 幂等：已在运行视为成功，避免 UI 误报红色错误
            return {"ok": True, "msg": "代理已在运行中", "already_running": True, "managed": True}
        s = load_settings()
        ok, msg = preflight("start_proxy", s)
        if not ok:
            return {"ok": False, "msg": msg}

        configured_port = _coerce_port(s.get("proxy_port"), DEFAULT_PROXY_PORT)
        proxy_bind_host = str(s.get("proxy_host", "127.0.0.1") or "127.0.0.1").strip()
        external_port, reassigned_port = _select_proxy_port(configured_port, host=proxy_bind_host)

        # 关键修复：端口上若已存在可用的 CLIProxyAPI（可能由其它 AIProxyHub/旧版本启动），
        # 本实例不再重复启动，避免出现“黑框闪一下就没了”（新进程绑定端口失败后瞬间退出）。
        if _proxy_reachable(external_port):
            return {
                "ok": True,
                "already_running": True,
                "managed": False,
                "msg": (
                    f"检测到端口 {external_port} 已有代理服务在运行（为避免冲突，本次不再重复启动）。"
                    "若你希望应用当前配置（含网关/缓存），请先关闭占用端口的其它 AIProxyHub/cli-proxy-api 进程，或修改端口后重试。"
                ),
            }

        stored_port = _coerce_port(s.get("proxy_port"), 0)
        if reassigned_port or stored_port != external_port:
            previous = s.get("proxy_port")
            s["proxy_port"] = external_port
            try:
                save_settings(s)
            except Exception as exc:
                return {"ok": False, "msg": f"代理端口 {external_port} 可用，但无法保存端口配置：{exc}"}
            if reassigned_port:
                log(f"[SYS] 代理端口 {previous} 已被占用，已自动切换到 {external_port}")
            else:
                log(f"[SYS] 已将无效代理端口 {previous!r} 规范化为 {external_port}")

        # 若启用缓存/网关：对外端口给网关占用；CLIProxyAPI 改为内部端口监听
        gw_enabled = bool(s.get("gateway_enabled", False)) or bool(s.get("cache_enabled", False))
        if gw_enabled:
            _gateway_set_config_from_settings(s)
            _cache_clear()  # 新一轮启动：清空缓存与统计，避免误判
            _ratelimit_reset()  # 同理：旧窗口和在途计数不应跨重启累积

            internal_port = find_free_port(external_port + 1, host="127.0.0.1")

            ss = dict(s)
            ss["proxy_port"] = internal_port
            # 内部端口仅供本机网关访问，避免对外暴露
            ss["proxy_host"] = "127.0.0.1"

            proxy_config_path = RUNTIME_PROXY_CONFIG
            generate_proxy_config(ss, proxy_config_path)
        else:
            proxy_config_path = RUNTIME_PROXY_CONFIG
            generate_proxy_config(s, proxy_config_path)

        exe = _get_proxy_exe_path()
        # Windows EXE（--noconsole）启动 console 子进程时，会弹出一个黑框。
        # 这里默认隐藏 CLIProxyAPI 的控制台窗口，避免用户误以为“程序闪退/异常”。
        creationflags = 0
        if _is_windows() and _is_frozen_exe() and not str(os.getenv("AIPROXYHUB_SHOW_PROXY_CONSOLE", "") or "").strip():
            try:
                creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW"))
            except Exception:
                creationflags = 0
        proxy_process = subprocess.Popen(
            [exe, "-config", proxy_config_path],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        threading.Thread(target=stream_output, args=(proxy_process, "PROXY"), daemon=True).start()
        threading.Thread(
            target=_watch_process_cleanup, args=(proxy_process, "proxy", proxy_config_path), daemon=True
        ).start()

        if gw_enabled:
            try:
                # 网关监听用户配置的 host:proxy_port；内部转发到 internal_port
                start_gateway(
                    str(s.get("proxy_host", "127.0.0.1") or "127.0.0.1"),
                    external_port,
                    internal_port,
                )
            except Exception as e:
                # 网关启动失败：回滚 CLIProxyAPI 子进程，避免“内部占端口但对外不可用”
                try:
                    proxy_process.terminate()
                    proxy_process.wait(timeout=2)
                except Exception:
                    try:
                        proxy_process.kill()
                    except Exception:
                        pass
                proxy_process = None
                _safe_unlink(proxy_config_path)
                proxy_config_path = None
                return {"ok": False, "msg": f"网关启动失败：{e}"}

    # ===== 锁外等待就绪（避免阻塞其它 API/日志线程）=====
    def _get_models_ids(port: int, token: str) -> list[str]:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"http://127.0.0.1:{int(port)}/v1/models",
                headers={"Authorization": f"Bearer {str(token or '')}"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            obj = json.loads(raw or "{}") if raw else {}
            data = obj.get("data") if isinstance(obj, dict) else None
            if not isinstance(data, list):
                return []
            ids = []
            for it in data:
                if isinstance(it, dict) and it.get("id"):
                    ids.append(str(it["id"]))
            return ids
        except Exception:
            return []

    # 先等端口可连通（401/403 也算就绪）
    deadline = time.time() + 20
    while time.time() < deadline:
        # 子进程异常退出（快速失败）
        with _lock:
            pp = proxy_process
        if pp is None or (pp and pp.poll() is not None):
            rc = None
            try:
                rc = int(getattr(proxy_process, "returncode", None)) if proxy_process else None
            except Exception:
                rc = None
            last = ""
            try:
                tail = _tail_log_lines("[PROXY]", 1)
                if tail:
                    last = str(tail[-1])
            except Exception:
                last = ""
            msg = f"代理进程异常退出（exit={rc if rc is not None else '?'}）。请查看「运行日志」中的 [PROXY] 输出。"
            if last:
                msg += f" 最后日志: {last}"
            return {"ok": False, "msg": msg}
        if _proxy_reachable(external_port):
            break
        time.sleep(0.25)

    # 再等模型列表非空（尽力而为）
    primary_key = str((s or {}).get("api_key", "") or "").strip()
    has_auth_files = False
    try:
        if os.path.isdir(AUTH_DIR):
            for name in os.listdir(AUTH_DIR):
                if str(name).lower().endswith(".json"):
                    has_auth_files = True
                    break
    except Exception:
        pass

    # 有大量 auth 文件时，第一次加载可能略慢；给更长一点的等待预算
    models_deadline = time.time() + (20 if has_auth_files else 8)
    models_ids: list[str] = []
    while time.time() < models_deadline:
        if proxy_process is None or (proxy_process and proxy_process.poll() is not None):
            return {"ok": False, "msg": "代理进程异常退出（请查看日志）"}
        models_ids = _get_models_ids(external_port, primary_key) if primary_key else []
        if models_ids:
            break
        time.sleep(0.5)

    log("[SYS] 代理服务已启动")
    if gw_enabled:
        msg = f"代理已启动（网关模式）→ localhost:{external_port}（内部端口 {internal_port}）"
    else:
        msg = f"代理已启动 → localhost:{external_port}"

    if models_ids:
        msg += f"（模型已就绪：{len(models_ids)} 个）"
    else:
        msg += "（模型仍在加载中：稍后再试 /v1/models）"

    return {"ok": True, "msg": msg, "models_count": len(models_ids), "models_head": models_ids[:12]}


def stop_proxy():
    global proxy_process
    global proxy_config_path
    # 如果端口上存在“外部启动的代理”，stop/restart 无法管理，只能提示用户手动处理。
    s = load_settings()
    port = _coerce_port(s.get("proxy_port"), DEFAULT_PROXY_PORT)
    external_running = _proxy_reachable(port)
    proc = None
    cfg = None
    gw_running = False
    with _lock:
        gw_running = gateway_server is not None
        if proxy_process and proxy_process.poll() is None:
            proc = proxy_process
            cfg = proxy_config_path
            # 先清全局引用（避免锁内阻塞等待；同时避免其它请求误判仍在运行）
            proxy_process = None
            proxy_config_path = None
        else:
            # 即便 proxy 子进程不在运行，也尽量清理网关与遗留配置文件
            cfg = proxy_config_path
            proxy_config_path = None
            if not gw_running:
                _safe_unlink(cfg)
                if external_running:
                    return {
                        "ok": False,
                        "external_running": True,
                        "msg": (
                            f"检测到端口 {port} 上已有代理服务在运行，但不是本实例启动，无法停止。"
                            "请关闭其它 AIProxyHub（或结束 cli-proxy-api.exe 进程），或修改端口后重试。"
                        ),
                    }
                # 幂等：未运行视为成功
                return {"ok": True, "msg": "代理未在运行"}

    # 优先停止网关，避免对外继续接受请求
    if gw_running:
        try:
            stop_gateway()
        except Exception:
            pass
        try:
            _cache_clear()
        except Exception:
            pass

    # 锁外做阻塞等待，避免与 stdout 读取线程互相影响
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    _safe_unlink(cfg)
    log("[SYS] 代理服务已停止")
    return {"ok": True, "msg": "代理已停止"}


def restart_proxy():
    """
    重启代理（stop -> start）。

    说明：
    - 若当前未运行，则等价于 start_proxy()
    - 用于“配置已保存但需重启生效”的一键应用
    """
    r1 = stop_proxy()
    if not bool(r1.get("ok", False)):
        # 外部占用端口：本实例无法重启
        if bool(r1.get("external_running", False)):
            return r1
        # 未在运行：直接尝试启动
        r2 = start_proxy()
        if bool(r2.get("ok", False)):
            return {"ok": True, "msg": "代理已启动"}
        return r2

    r2 = start_proxy()
    if bool(r2.get("ok", False)):
        return {"ok": True, "msg": "代理已重启"}
    return {"ok": False, "msg": f"代理已停止，但启动失败：{r2.get('msg','')}"}


def secure_cleanup_outputs():
    """清理未被使用的临时代理配置；不触碰用户的 provider 凭据文件。"""
    try:
        port = _coerce_port(load_settings().get("proxy_port"), DEFAULT_PROXY_PORT)
    except Exception:
        port = 8317
    if proxy_process is not None and proxy_process.poll() is None:
        return {"ok": False, "msg": "代理正在运行，暂不清理运行时配置"}
    if _proxy_reachable(port):
        return {"ok": False, "msg": "检测到代理仍在运行，暂不清理运行时配置"}
    removed = 0
    if os.path.exists(RUNTIME_PROXY_CONFIG):
        _safe_unlink(RUNTIME_PROXY_CONFIG)
        removed = 1
    try:
        if os.path.isdir(RUNTIME_DIR) and not os.listdir(RUNTIME_DIR):
            os.rmdir(RUNTIME_DIR)
    except Exception:
        pass
    msg = f"已清理 {removed} 个过期运行时配置"
    log(f"[SYS] {msg}")
    return {"ok": True, "msg": msg}
def _proxy_reachable(port, timeout=0.6):
    """检查本机代理端口，不能被系统代理或丢包拖慢管理面板。"""
    try:
        import urllib.error
        import urllib.request

        checked_port = _coerce_port(port, 0)
        if not checked_port:
            return False
        # 这是本机健康探测，必须绕过 Clash/Mihomo；否则一个本机空端口会
        # 被外部代理接管，造成误判和每次约 2 秒的 UI 卡顿。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            f"http://127.0.0.1:{checked_port}/v1/models",
            headers={"Connection": "close"},
        )
        with opener.open(request, timeout=max(0.2, min(float(timeout), 5.0))):
            return True
    except urllib.error.HTTPError as e:
        # 未带 API Key 时，CLIProxyAPI 通常会返回 401/403，这代表服务已在运行。
        # 其它 4xx/5xx（例如 404/502）更可能是“不是目标服务”或“内部尚未就绪”，因此不视为可用。
        try:
            code = int(getattr(e, "code", 0) or 0)
        except Exception:
            code = 0
        return code in (401, 403)
    except Exception:
        return False


from launcher_monitor import (
    start_monitor,
    stop_monitor,
    get_monitor_status,
    trigger_monitor_now,
)


def _wake_monitor():
    """Ask the optional read-only monitor to sample immediately."""
    try:
        from launcher_monitor import _monitor_wake

        _monitor_wake.set()
    except Exception:
        pass


# ==================== HTML 前端 ====================
HTML_PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIProxyHub</title>
<style>
:root{--bg:#f6f8fb;--card:#fff;--line:#dbe3ee;--text:#172033;--muted:#64748b;--accent:#4f46e5;--ok:#15803d;--bad:#b91c1c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:18px 24px;background:var(--card);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
h1{font-size:20px;margin:0}h2{font-size:18px;margin:0 0 14px}main{max-width:1180px;margin:auto;padding:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:18px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:18px}
.label{color:var(--muted);font-size:12px}.value{font-size:23px;font-weight:700;margin-top:5px}.ok{color:var(--ok)}.bad{color:var(--bad)}
.toolbar{display:flex;gap:8px;flex-wrap:wrap}.btn{border:1px solid var(--line);background:#fff;border-radius:7px;padding:8px 12px;cursor:pointer}.primary{background:var(--accent);border-color:var(--accent);color:#fff}.danger{color:var(--bad)}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}.field{display:flex;flex-direction:column;gap:5px}.full{grid-column:1/-1}
label{font-size:12px;color:var(--muted)}input,select,textarea{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--text)}.check{display:flex;align-items:center;gap:8px}.check input{width:auto}
.hint{color:var(--muted);font-size:12px}.note{border-left:3px solid var(--bad);padding-left:10px}pre{white-space:pre-wrap;word-break:break-word;background:#111827;color:#e5e7eb;border-radius:7px;padding:12px;min-height:120px;max-height:360px;overflow:auto}
@media(max-width:640px){main{padding:14px}}
</style>
</head>
<body>
<header><h1>AIProxyHub <small id="version"></small></h1><span class="hint">本地优先 · 用户自有凭据 · OpenAI-compatible</span></header>
<main>
<div id="notice" class="hint"></div>
<section class="grid">
  <div class="card"><div class="label">代理</div><div id="proxy" class="value">--</div></div>
  <div class="card"><div class="label">网关</div><div id="gateway" class="value">--</div></div>
  <div class="card"><div class="label">健康监控</div><div id="monitor" class="value">--</div></div>
  <div class="card"><div class="label">缓存命中率</div><div id="cache" class="value">--</div></div>
  <div class="card"><div class="label">用量请求</div><div id="usage" class="value">--</div></div>
</section>
<section class="card"><h2>运行控制</h2><div class="toolbar">
  <button class="btn primary" onclick="post('/api/start_proxy')">启动代理</button><button class="btn" onclick="post('/api/restart_proxy')">重启代理</button><button class="btn danger" onclick="post('/api/stop_proxy')">停止代理</button><button class="btn" onclick="post('/api/cache_clear')">清空缓存</button><button class="btn" onclick="post('/api/secure_cleanup_outputs')">清理过期运行时文件</button>
</div><p class="hint">默认只监听本机；缓存始终按客户端凭据隔离。远程监听必须显式开启并配置独立管理 API Key。</p></section>
<section class="card"><h2>配置</h2><div class="form">
  <div class="field"><label>上游代理（可选）</label><input id="proxy_url" placeholder="http://127.0.0.1:7890"></div><div class="field"><label>监听地址</label><input id="proxy_host" placeholder="127.0.0.1"></div><div class="field"><label>监听端口</label><input id="proxy_port" type="number" min="1" max="65535"></div><div class="field"><label>路由策略</label><select id="routing_strategy"><option value="round-robin">round-robin</option><option value="fill-first">fill-first</option></select></div><div class="field"><label>请求重试次数</label><input id="request_retry" type="number" min="0" max="10"></div>
  <div class="field"><label>管理密码（留空保持）</label><input id="management_password" type="password" autocomplete="new-password"></div><div class="field"><label>客户端 API Key（留空保持）</label><input id="api_key" type="password" autocomplete="new-password"></div><div class="field"><label>管理 API Key（留空保持）</label><input id="admin_api_key" type="password" autocomplete="new-password"></div><div class="field full"><label>额外客户端 API Key（每行一把，留空保持）</label><textarea id="api_keys_extra" rows="3"></textarea></div><div class="field full"><label>缓存 Vary Headers</label><input id="cache_vary_headers"></div>
</div>
<div class="form" style="margin-top:14px"><label class="check"><input id="allow_remote_proxy" type="checkbox">允许远程网关（危险）</label><label class="check"><input id="debug" type="checkbox">调试日志</label><label class="check"><input id="gateway_enabled" type="checkbox">启用透明网关</label><label class="check"><input id="cache_enabled" type="checkbox">启用非流式缓存</label><label class="check"><input id="cache_stream_enabled" type="checkbox">启用流式缓存（谨慎）</label><label class="check"><input id="monitor_enabled" type="checkbox">启用只读健康监控</label></div>
<div class="form" style="margin-top:14px"><div class="field"><label>监控间隔（秒）</label><input id="monitor_interval_seconds" type="number" min="10"></div><div class="field"><label>缓存 TTL（秒）</label><input id="cache_ttl_seconds" type="number" min="1"></div><div class="field"><label>TTL 抖动（秒）</label><input id="cache_ttl_jitter_seconds" type="number" min="0"></div><div class="field"><label>最大缓存条目</label><input id="cache_max_entries" type="number" min="0"></div><div class="field"><label>单条最大 KB</label><input id="cache_max_body_kb" type="number" min="1"></div><div class="field"><label>缓存总上限 MB</label><input id="cache_max_total_mb" type="number" min="0"></div><div class="field"><label>SWR 秒数</label><input id="cache_stale_while_revalidate_seconds" type="number" min="0"></div><div class="field"><label>SIE 秒数</label><input id="cache_stale_if_error_seconds" type="number" min="0"></div><div class="field"><label>每凭据 RPM 上限（0=关闭）</label><input id="gateway_rate_limit_rpm" type="number" min="0"></div><div class="field"><label>全局并发上限（0=关闭）</label><input id="gateway_max_concurrent_requests" type="number" min="0"></div></div>
<p class="hint note">密钥不会由页面回显；落盘使用 Windows DPAPI。不要把 provider 凭据提交到 Issue、日志或仓库。</p><div class="toolbar"><button class="btn primary" onclick="saveSettings()">保存配置</button><button class="btn" onclick="loadSettings()">重新读取</button></div></section>
<section class="card"><h2>健康监控</h2><div class="toolbar"><button class="btn" onclick="post('/api/start_monitor')">启动</button><button class="btn" onclick="post('/api/stop_monitor')">停止</button><button class="btn" onclick="post('/api/trigger_monitor')">立即检查</button></div><pre id="monitor_detail">--</pre></section>
<section class="card"><h2>运行日志</h2><div class="toolbar"><button class="btn" onclick="post('/api/clear_logs')">清空日志</button><button class="btn" onclick="refreshLogs()">刷新</button></div><pre id="logs">--</pre></section>
<section class="card"><h2>关于</h2><p>AIProxyHub 是独立开源项目，用于管理用户已授权的 provider 和 OpenAI-compatible 本地网关。它不创建第三方账户、不读取临时邮箱 OTP、不模拟反滥用挑战、不批量提取消费者 OAuth token，也不负责规避服务限额。</p><p class="hint">项目不隶属于、未获 OpenAI 或其他 provider 赞助/背书。Codex 仅作为维护、测试、文档和审查的辅助工具，所有变更由维护者审核。</p></section>
</main>
<script>
const TOKEN="__API_TOKEN__",headers=()=>({"X-AIProxyHub-Token":TOKEN}),$=id=>document.getElementById(id);
function notice(v){$("notice").textContent=String(v||"")}
async function request(path,opt={}){const h=Object.assign({},headers(),opt.headers||{}),r=await fetch(path,Object.assign({},opt,{headers:h})),d=await r.json().catch(()=>({ok:false,msg:"invalid response"}));if(!r.ok)throw Error(d.msg||("HTTP "+r.status));return d}
async function post(path,body={}){try{const d=await request(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});notice(d.msg||"已完成");await refreshAll();return d}catch(e){notice("失败："+e.message)}}
const normal=["proxy_url","proxy_host","proxy_port","allow_remote_proxy","routing_strategy","request_retry","debug","monitor_enabled","monitor_interval_seconds","gateway_enabled","cache_enabled","cache_stream_enabled","cache_vary_headers","cache_ttl_seconds","cache_ttl_jitter_seconds","cache_max_entries","cache_max_body_kb","cache_max_total_mb","cache_stale_while_revalidate_seconds","cache_stale_if_error_seconds","gateway_rate_limit_rpm","gateway_max_concurrent_requests"],secrets=["management_password","api_key","admin_api_key","api_keys_extra"];
function setField(id,v){const e=$(id);if(!e)return;if(e.type==="checkbox")e.checked=!!v;else e.value=v??""}
async function loadSettings(){try{const s=await request("/api/settings");$("version").textContent="v"+(s.version||"");normal.forEach(id=>setField(id,id==="proxy_url"?s.proxy:s[id]));secrets.forEach(id=>setField(id,""));notice("已读取配置（敏感字段不会回显）")}catch(e){notice("读取失败："+e.message)}}
function collect(){const d={};normal.forEach(id=>{const e=$(id);d[id==="proxy_url"?"proxy":id]=e.type==="checkbox"?e.checked:e.value});secrets.forEach(id=>d[id]=$(id).value);return d}
async function saveSettings(){await post("/api/save",collect());await loadSettings()}
async function refreshStatus(){try{const s=await request("/api/status"),set=(id,t,ok)=>{$(id).textContent=t;$(id).className="value "+(ok?"ok":"bad")};set("proxy",s.proxy?"可达":"未运行",!!s.proxy);set("gateway",s.gateway.running?"运行中":"未启用",!!s.gateway.running);set("monitor",s.monitor.running?"运行中":"未启动",!!s.monitor.running);set("cache",(s.gateway.cache.hit_ratio_pct||0)+"%",true);$("monitor_detail").textContent=JSON.stringify(s.monitor,null,2)}catch(e){}}
async function refreshUsage(){try{const u=await request("/api/usage_summary");$("usage").textContent=u.ok?String(u.total_requests||0):"--"}catch(e){$("usage").textContent="--"}}
async function refreshLogs(){try{const d=await request("/api/logs");$("logs").textContent=(d.lines||[]).join("\n")}catch(e){}}
async function refreshAll(){await Promise.all([refreshStatus(),refreshUsage(),refreshLogs()])}
loadSettings();refreshAll();setInterval(refreshAll,4000);
</script>
</body>
</html>'''


_usage_cache_lock = threading.Lock()
_usage_cache = {"ts": 0.0, "data": {"ok": False, "msg": "尚未查询"}}


def _query_usage_summary():
    """
    通过 CPA 管理 API 查询用量统计摘要（不返回全量明细，避免页面/日志被大量数据拖慢）。

    说明：
    - 数据源：/v0/management/usage
    - 由于该接口可能包含较多历史明细，这里只抽取汇总字段并做短 TTL 缓存。
    """
    now = time.time()
    with _usage_cache_lock:
        if _usage_cache.get("data") and now - float(_usage_cache.get("ts", 0) or 0) < 5:
            return _usage_cache["data"]

    s = load_settings()
    port = _coerce_port(s.get("proxy_port"), DEFAULT_PROXY_PORT)
    mgmt_pw = str(s.get("management_password", "") or "")
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v0/management/usage",
            headers={"Authorization": f"Bearer {mgmt_pw}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw or "{}") if raw else {}
        u = data.get("usage") or {}
        out = {
            "ok": True,
            "failed_requests": int(data.get("failed_requests", 0) or 0),
            "total_requests": int(u.get("total_requests", 0) or 0),
            "success_count": int(u.get("success_count", 0) or 0),
            "failure_count": int(u.get("failure_count", 0) or 0),
            "total_tokens": int(u.get("total_tokens", 0) or 0),
            "requests_by_day": u.get("requests_by_day", {}) or {},
            "tokens_by_day": u.get("tokens_by_day", {}) or {},
        }
    except Exception as e:
        out = {"ok": False, "msg": f"无法读取使用统计: {e}"}

    with _usage_cache_lock:
        _usage_cache["ts"] = now
        _usage_cache["data"] = out
    return out


def _build_status_response() -> dict:
    """构造状态 JSON，供 /api/status 和 /api/ext/status 共用。"""
    s = load_settings()
    gw_enabled = bool(s.get("gateway_enabled", False)) or bool(s.get("cache_enabled", False))
    port = _coerce_port(s.get("proxy_port"), DEFAULT_PROXY_PORT)
    proxy_reachable = _proxy_reachable(port)
    proxy_alive = proxy_process is not None and proxy_process.poll() is None
    gw_alive = gateway_server is not None
    proxy_managed = bool(proxy_alive or gw_alive)
    proxy_external = bool(proxy_reachable and not proxy_managed)
    proxy_ok = bool(proxy_reachable)
    return {
        "version": APP_VERSION,
        "proxy": proxy_ok,
        "proxy_managed": proxy_managed,
        "proxy_external": proxy_external,
        "port": port,
        "paths": {
            "root": str(ROOT),
            "settings_file": str(SETTINGS_FILE),
            "data_dir": str(DATA_DIR),
            "runtime_dir": str(RUNTIME_DIR),
        },
        "monitor": get_monitor_status(),
        "gateway": {
            "enabled": gw_enabled,
            "running": gw_alive,
            "listen_host": gateway_listen_host,
            "listen_port": gateway_listen_port,
            "upstream_port": gateway_upstream_port,
            "cache": _cache_stats(),
            "limits": _ratelimit_stats(),
        },
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _read_json_body(self):
        """Read a bounded JSON body so a management request cannot exhaust memory."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            self._json({"ok": False, "msg": "Invalid Content-Length"}, 400)
            return None
        if length < 0 or length > 1024 * 1024:
            self._json({"ok": False, "msg": "Request body too large"}, 413)
            return None
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"ok": False, "msg": "Invalid JSON body"}, 400)
            return None
        if not isinstance(value, dict):
            self._json({"ok": False, "msg": "JSON body must be an object"}, 400)
            return None
        return value

    def _auth_ext(self):
        """
        验证外部 API（/api/ext/*）请求的 Bearer token。

        - 默认兼容历史行为：使用 settings.api_key
        - 若 settings.admin_api_key 非空：仅接受 admin_api_key（推荐与客户端 key 分离）
        """
        auth = self.headers.get("Authorization", "")
        parts = auth.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        token = parts[1].strip()
        s = load_settings()
        admin = str(s.get("admin_api_key", "") or "").strip()
        expected = admin if admin else str(s.get("api_key", "") or "")
        if not token or not expected:
            return False
        return secrets.compare_digest(token, expected)

    def _is_local_request(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "localhost")

    def _auth_read(self):
        """Allow local UI reads; require the management bearer token remotely."""
        return self._is_local_request() or self._auth_ext()

    def _html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_GET(self):
        if self.path.startswith("/api/") and not self._auth_read():
            self._json({"ok": False, "msg": "Unauthorized"}, 401)
            return
        if self.path == "/" and not self._auth_read():
            self._json({"ok": False, "msg": "Unauthorized"}, 401)
            return
        if self.path == "/":
            self._html(
                HTML_PAGE.replace("__API_TOKEN__", _UI_API_TOKEN).replace("__APP_VERSION__", APP_VERSION)
            )
        elif self.path == "/api/settings":
            self._json(get_settings_public())
        elif self.path == "/api/status":
            self._json(_build_status_response())
        elif self.path == "/api/usage_summary":
            self._json(_query_usage_summary())
        elif self.path == "/api/logs":
            with _log_lock:
                lines_copy = list(log_lines)[-200:]
            self._json({"lines": lines_copy})
        # ===== 外部 API（Bearer api_key 认证，无需页面 CSRF token） =====
        elif self.path.startswith("/api/ext/"):
            if not self._auth_ext():
                self._json({"ok": False, "msg": "Unauthorized"}, 401)
                return
            ep = self.path[9:]  # strip "/api/ext/"
            if ep == "status":
                resp = _build_status_response()
                resp["ok"] = True
                self._json(resp)
            elif ep == "usage_summary":
                self._json(_query_usage_summary())
            elif ep == "cache_stats":
                self._json({"ok": True, "cache": _cache_stats(), "gateway_running": gateway_server is not None})
            elif ep == "logs":
                with _log_lock:
                    lines_copy = list(log_lines)[-200:]
                self._json({"ok": True, "lines": lines_copy})
            elif ep == "monitor_status":
                self._json(get_monitor_status())
            else:
                self._json({"ok": False, "msg": "Unknown endpoint"}, 404)
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            p = self.path
            # ===== 外部 API（Bearer api_key 认证） =====
            if p.startswith("/api/ext/"):
                if not self._auth_ext():
                    self._json({"ok": False, "msg": "Unauthorized"}, 401)
                    return
                body = self._read_json_body()
                if body is None:
                    return
                ep = p[9:]
                if ep == "start_proxy":
                    self._json(start_proxy())
                elif ep == "stop_proxy":
                    self._json(stop_proxy())
                elif ep == "restart_proxy":
                    self._json(restart_proxy())
                elif ep == "start_monitor":
                    self._json(start_monitor())
                elif ep == "stop_monitor":
                    self._json(stop_monitor())
                elif ep == "monitor_status":
                    self._json(get_monitor_status())
                elif ep == "trigger_monitor":
                    self._json(trigger_monitor_now())
                elif ep == "cache_clear":
                    _cache_clear()
                    self._json({"ok": True, "msg": "缓存已清空", "cache": _cache_stats()})
                else:
                    self._json({"ok": False, "msg": "Unknown endpoint"}, 404)
                return
            # CSRF/误触保护：所有写操作必须带一次性 token（页面内置，第三方站点无法随意构造）
            if not self._is_local_request():
                self._json({"ok": False, "msg": "Local UI writes are disabled remotely"}, 403)
                return
            if self.headers.get("X-AIProxyHub-Token", "") != _UI_API_TOKEN:
                self._json({"ok": False, "msg": "Forbidden"}, 403)
                return
            body = self._read_json_body()
            if body is None:
                return
            p = self.path
            if p == "/api/save":
                before = load_settings()
                after = save_settings(body)
                changed = {k for k in PROXY_RESTART_KEYS if before.get(k) != after.get(k)}
                proxy_running = (proxy_process is not None and proxy_process.poll() is None) or (gateway_server is not None)
                restart_required = bool(proxy_running and changed)
                log("[SYS] 配置已保存")
                msg = "配置已保存"
                # 按需启停后台监控（无需重启代理即可生效）
                try:
                    mb = bool(before.get("monitor_enabled", False))
                    ma = bool(after.get("monitor_enabled", False))
                    if mb != ma:
                        if ma:
                            rr = start_monitor()
                            if rr.get("ok"):
                                msg += "（监控已启动）"
                            else:
                                msg += "（监控启动失败）"
                        else:
                            stop_monitor()
                            msg += "（监控已停止）"
                except Exception:
                    pass
                if restart_required:
                    msg += "（代理正在运行，点击“重启代理”生效）"
                self._json({
                    "ok": True,
                    "msg": msg,
                    "settings": get_settings_public(),
                    "restart_required": restart_required,
                    "restart_keys": sorted(list(changed)),
                })
            elif p == "/api/start_proxy":
                self._json(start_proxy())
            elif p == "/api/stop_proxy":
                self._json(stop_proxy())
            elif p == "/api/restart_proxy":
                self._json(restart_proxy())
            elif p == "/api/clear_logs":
                with _log_lock:
                    log_lines.clear()
                self._json({"ok": True, "msg": "日志已清空"})
            elif p == "/api/start_monitor":
                self._json(start_monitor())
            elif p == "/api/stop_monitor":
                self._json(stop_monitor())
            elif p == "/api/trigger_monitor":
                self._json(trigger_monitor_now())
            elif p == "/api/secure_cleanup_outputs":
                self._json(secure_cleanup_outputs())
            elif p == "/api/cache_clear":
                _cache_clear()
                self._json({"ok": True, "msg": "缓存已清空", "cache": _cache_stats()})
            else:
                self.send_error(404)
        except Exception as e:
            log(f"[ERR] POST {self.path}: {e}")
            try:
                self._json({"ok": False, "msg": str(e)}, 500)
            except Exception:
                pass


def find_free_port(preferred, host: str = "127.0.0.1"):
    preferred = _coerce_port(preferred, 0)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            s.bind((host, 0))
            return s.getsockname()[1]


def _select_proxy_port(preferred, host: str = "127.0.0.1") -> tuple[int, bool]:
    """Choose the configured proxy port or a free replacement.

    The boolean reports whether the configured port had to be replaced. A
    reachable service is deliberately returned as-is so start_proxy can remain
    idempotent for a proxy started by another authorized local process.
    """
    configured = _coerce_port(preferred, DEFAULT_PROXY_PORT)
    if _proxy_reachable(configured) or _is_port_free_local(configured, host=host):
        return configured, False
    next_preferred = configured + 1 if configured < 65535 else 0
    selected = find_free_port(next_preferred, host=host)
    if not selected:
        raise OSError(f"无法为代理找到可用端口（首选 {configured}）")
    return selected, True


def _normalize_launcher_host(host: str) -> str:
    h = str(host or "").strip()
    if not h:
        return LAUNCHER_HOST
    if h.lower() == "localhost":
        return "127.0.0.1"
    return h


def main(argv=None):
    parser = argparse.ArgumentParser(prog="AIProxyHub", add_help=True)
    parser.add_argument("--host", default=LAUNCHER_HOST, help="launcher 监听地址（默认仅本机 127.0.0.1）")
    parser.add_argument("--port", type=int, default=LAUNCHER_PORT, help="launcher 监听端口（默认 9090）")
    parser.add_argument("--strict-port", action="store_true", help="端口被占用时直接失败（默认会自动选择空闲端口）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许绑定到非本机回环地址（危险：会暴露管理面板/API，请确保强口令与网络隔离）",
    )
    parser.add_argument("--version", action="version", version=f"AIProxyHub {APP_VERSION}")

    args = parser.parse_args(argv)

    host = _normalize_launcher_host(args.host)

    # 默认只允许本机回环；需要显式 allow-remote 才允许 0.0.0.0/局域网 IP。
    if host not in ("127.0.0.1", "localhost") and not args.allow_remote:
        raise SystemExit("安全拦截：launcher 默认仅允许绑定 127.0.0.1；如需非本机监听，请加 --allow-remote")

    # Windows 安装版/EXE：默认启用单实例，避免用户误开多个面板导致“启动代理黑框闪现/CPA 面板异常”等问题。
    # 说明：按 ROOT（AIPROXYHUB_HOME）分区；不同隔离目录仍可并存（用于冒烟/测试）。
    if _is_frozen_exe():
        if not _acquire_single_instance_mutex():
            # 尽量找到已运行实例的真实端口（端口可能被占用后自动递增）
            base_port = int(args.port or LAUNCHER_PORT)
            preferred = [_read_launcher_port_file(), base_port, int(LAUNCHER_PORT)]
            try:
                preferred += list(range(int(LAUNCHER_PORT), int(LAUNCHER_PORT) + 12))
            except Exception:
                pass
            existing_port = _discover_existing_launcher_port(preferred) or base_port
            url = f"http://localhost:{existing_port}"
            try:
                webbrowser.open(url)
            except Exception:
                pass
            _windows_message_box(
                f"AIProxyHub 已在运行。\n\n已尝试为你打开：\n{url}\n\n如仍异常，请先关闭重复的 AIProxyHub 进程后再试。"
            )
            raise SystemExit(0)

    s = load_settings()  # 启动即触发 DPAPI 自动迁移

    for lf_name, lf_path in [("config.yaml", PROXY_CONFIG)]:
        if os.path.exists(lf_path):
            bak = lf_path + ".bak"
            try:
                os.replace(lf_path, bak)
                os.remove(bak)
                log(f"[SYS] 已将历史明文配置 {lf_name} 迁移并安全删除（当前版本使用临时目录）")
            except Exception as e:
                log(f"[WARN] 无法迁移 {lf_name}: {e}；建议手动删除以降低泄露风险")

    # 兜底：清理上次异常退出可能残留的运行时临时配置（避免敏感配置长期留在临时目录）
    try:
        proxy_port = _coerce_port((s or {}).get("proxy_port"), DEFAULT_PROXY_PORT)
        if not _proxy_reachable(proxy_port):
            _safe_unlink(RUNTIME_PROXY_CONFIG)
    except Exception:
        pass

    requested_launcher_port = _coerce_port(args.port, LAUNCHER_PORT)

    # 提升并发下的连接排队能力（backlog）。对本地面板/External API 也更稳健。
    # 同上：request_queue_size 需要在 listen() 之前生效，因此用子类覆写类属性。
    class _LauncherServer(http.server.ThreadingHTTPServer):
        request_queue_size = 128
        # 同 _GatewayServer：避免 Windows 下 SO_REUSEADDR 造成“多实例共享同一端口”。
        allow_reuse_address = False

        def server_bind(self):
            try:
                if _is_windows() and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except Exception:
                pass
            return super().server_bind()

    server = None
    port = requested_launcher_port
    bind_error = None
    for attempt in range(12):
        if not args.strict_port and attempt:
            port = find_free_port(0, host=host)
        elif not args.strict_port:
            port = find_free_port(port, host=host)
        try:
            server = _LauncherServer((host, port), Handler)
            break
        except OSError as exc:
            bind_error = exc
            if args.strict_port:
                raise SystemExit(f"管理端口 {port} 无法监听：{exc}。请换用 --port 或关闭占用进程。") from exc
    if server is None:
        raise SystemExit(f"管理面板连续尝试 12 个端口仍无法监听：{bind_error}")
    # 若用户启用了自动监控，则随 launcher 启动（不依赖面板操作/External API）。
    try:
        if bool((s or {}).get("monitor_enabled", False)):
            start_monitor()
    except Exception:
        pass

    # 绑定到 0.0.0.0 时，本机仍可用 localhost 访问；远端请使用机器 IP。
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = f"http://{display_host}:{port}"
    print(f"\n  AIProxyHub 管理面板 → {url}\n")
    _write_launcher_port_file(port)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭...")
        try:
            stop_monitor()
        except Exception:
            pass
        try:
            stop_proxy()
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
