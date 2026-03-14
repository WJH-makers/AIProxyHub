"""
AIProxyHub - 一体化管理平台
CLIProxyAPI + ChatGPT 批量注册 深度整合
"""

import http.server
import http.client
import argparse
import base64
import ctypes
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
from datetime import datetime, timezone
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

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        portable_flag = os.path.join(exe_dir, "AIProxyHub.portable")
        if os.path.exists(portable_flag) and _dir_is_writable(exe_dir):
            return exe_dir
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "AIProxyHub")

    return SOURCE_DIR


BUNDLE_DIR = _get_bundle_dir()
ROOT = _get_app_root()
SETTINGS_FILE = os.path.join(ROOT, "settings.json")
# 兼容旧版本：历史上会在项目目录生成明文 config.yaml / register/config.json。
# 新版本默认改为运行时在临时目录生成，尽量不在项目目录常驻敏感配置。
PROXY_CONFIG = os.path.join(ROOT, "config.yaml")  # legacy (不再默认写入)
REGISTER_CONFIG = os.path.join(ROOT, "register", "config.json")  # legacy (不再默认写入)
AUTH_DIR = os.path.expanduser("~/.cli-proxy-api")
DATA_DIR = os.path.join(ROOT, "data")
APP_VERSION = "1.2.8"
LAUNCHER_HOST = "127.0.0.1"
LAUNCHER_PORT = 9090

os.makedirs(DATA_DIR, exist_ok=True)


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

RUNTIME_DIR = os.path.join(tempfile.gettempdir(), "AIProxyHub")
RUNTIME_PROXY_CONFIG = os.path.join(RUNTIME_DIR, "cli-proxy-api.runtime.yaml")
RUNTIME_REGISTER_CONFIG = os.path.join(RUNTIME_DIR, "register.runtime.json")

SECRET_FIELDS = ("duckmail_token", "management_password", "api_key", "admin_api_key", "api_keys_extra")
# 这些配置变更会影响 proxy（cli-proxy-api.exe 或透明网关）的行为；若代理正在运行，需要重启后生效。
PROXY_RESTART_KEYS = {
    "proxy",
    "proxy_port",
    "proxy_host",
    "routing_strategy",
    "request_retry",
    "quota_switch_project",
    "quota_switch_preview",
    "debug",
    "management_password",
    "api_key",
    "api_keys_extra",
    # 网关/缓存池
    "gateway_enabled",
    "cache_enabled",
    # 是否对 stream=true 的 SSE/WS 做缓存（命中时“快速回放”）；开关变化需要重启网关生效
    "cache_stream_enabled",
    "cache_shared_across_api_keys",
    "cache_vary_headers",
    "cache_ttl_seconds",
    "cache_max_entries",
    "cache_max_body_kb",
    "cache_max_total_mb",
    "cache_ttl_jitter_seconds",
    "cache_stale_while_revalidate_seconds",
    "cache_stale_if_error_seconds",
}

proxy_process = None
register_process = None
proxy_config_path = None
register_config_path = None
MAX_LOG = 800
log_lines = deque(maxlen=MAX_LOG)
_log_lock = threading.Lock()
_lock = threading.RLock()
autopilot_cancel = threading.Event()
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
    if not _is_frozen_exe():
        return
    try:
        os.makedirs(ROOT, exist_ok=True)
        with open(_launcher_port_file(), "w", encoding="utf-8") as f:
            f.write(str(int(port)))
    except Exception:
        pass


def _read_launcher_port_file() -> int:
    try:
        with open(_launcher_port_file(), "r", encoding="utf-8") as f:
            v = f.read().strip()
        return int(v)
    except Exception:
        return 0


def _probe_launcher_port(port: int) -> bool:
    """检查指定端口是否存在正在运行的 launcher（用于单实例场景下的“打开已运行窗口”）。"""
    try:
        import urllib.request
        import urllib.error

        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/api/status", timeout=0.35) as r:
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

# ===== 透明网关（cache-front）+ 缓存池（跨账号节省额度）=====
# 说明：
# - 默认关闭（gateway_enabled/cache_enabled 均为 False）
# - 开启后：网关占用对外端口(proxy_port)，CLIProxyAPI 改为内部端口监听
# - 网关对 /v1/responses / /v1/chat/completions（非 stream）可做去重缓存；命中缓存不会消耗账号配额
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
    "share_across_api_keys": False,
}


_REDACT_PATTERNS = [
    # DuckMail token
    (re.compile(r"\bdk_[A-Za-z0-9]{8,}\b"), "dk_***"),
    # OpenAI-style key (用户也可能用别的前缀，但先覆盖最常见的)
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"), "sk-***"),
    # JWT（常见 token 形态，防止异常栈/日志中泄露）
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "jwt_***"),
    # 明确字段（注册脚本/日志中常见）
    (re.compile(r"(ChatGPT密码\s*:\s*)(\S+)", re.IGNORECASE), r"\1***"),
    (re.compile(r"(邮箱密码\s*:\s*)(\S+)", re.IGNORECASE), r"\1***"),
    (re.compile(r"(API Key\s*:\s*)(\S+)", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Key\s*:\s*)(\S+)", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Authorization\s*:\s*Bearer\s+)(\S+)", re.IGNORECASE), r"\1***"),
]


def _redact_text(text: str) -> str:
    if text is None:
        return ""
    out = str(text)
    for pat, repl in _REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


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
        "duckmail_token": "",
        "proxy": "http://127.0.0.1:7890",
        "total_accounts": 5,
        "max_workers": 3,
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
        "proxy_port": 8317,
        "proxy_host": "127.0.0.1",
        "routing_strategy": "round-robin",
        "request_retry": 3,
        "quota_switch_project": True,
        "quota_switch_preview": True,
        "debug": False,
        # ===== 自动监控（可用率阈值 / 额度用完自动清理）=====
        # 说明：
        # - monitor_enabled=false 时不会启动后台线程（更安全，避免误删/误触发注册）
        # - 开启后会定期调用 CPA 管理 API（/v0/management/auth-files）查询账号状态
        # - 若启用“额度为零自动删除”，会删除被判定为额度耗尽的账号文件（AUTH_DIR/*.json）
        "monitor_enabled": False,
        # 检查间隔（秒）
        "monitor_interval_seconds": 60,
        # 可用率阈值（%）；低于该阈值时才会触发自动注册（若开启）
        "monitor_low_available_threshold_pct": 20,
        # 是否在可用率过低时自动触发注册
        "monitor_auto_register_enabled": True,
        # 是否自动清理“额度为零”的账号
        "monitor_prune_zero_quota_enabled": True,
        # 更安全：仅删除 CPA 标记为 usage_limit_reached 的账号（避免误删临时不可用账号）
        "monitor_prune_only_usage_limit_reached": True,
        # 安全护栏：至少保留 N 个账号文件，避免全删空导致服务不可用
        "monitor_min_keep_accounts": 1,
        # Dry-run：只记录候选与统计，不执行实际删除
        "monitor_dry_run": False,
        # ===== 安全默认：尽量减少敏感信息落盘 =====
        # 是否在 registered_accounts.txt 中保存账号/邮箱密码（不推荐）
        "store_passwords": False,
        # 是否生成 ak.txt / rk.txt 明文 token 文件（不推荐）
        "write_ak_rk": False,
        # 是否保留 token_json 文件（不推荐；默认上传成功后即清理）
        "keep_token_json": False,
        # 上传失败时是否保留 token_json（调试用；更安全的默认是 False）
        "keep_token_json_on_fail": False,
        # ===== 缓存池 / 网关（高级，默认关闭） =====
        # 当开启网关模式时：AIProxyHub 会在“对外端口(proxy_port)”启动一个轻量网关，
        # 并把 CLIProxyAPI 启动在内部端口（自动分配），以便在不改 Base URL 的前提下
        # 实现“响应缓存/多账号缓存池”等增强能力。
        "gateway_enabled": False,
        # 是否启用响应缓存（仅网关模式生效；默认关闭以避免意外缓存敏感提示词/结果）
        "cache_enabled": False,
        # 是否允许对 stream=true 的响应做缓存（SSE/WS）。
        # - 默认关闭：保持“流式传输只做转发、不落盘/不缓存”的直觉语义
        # - 开启后：对完全相同的请求可直接从缓存“快速回放”，显著降低重复任务的端到端耗时
        # 注意：这会把流式响应内容写入内存缓存池；仅建议在可信环境/明确接受缓存语义时开启。
        "cache_stream_enabled": False,
        # 是否跨不同客户端 API Key 共享缓存（默认更安全：按 API Key 隔离）
        # - False: cache key 包含 Authorization header（不同调用方互不影响）
        # - True:  cache key 不包含 Authorization header（适合“同一团队/同一批任务”共享缓存节省额度）
        "cache_shared_across_api_keys": False,
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
    }


def load_settings():
    """
    读取 settings.json（若存在），并将敏感字段解密到内存中。

    注意：settings.json 中敏感字段会以 dpapi:... 形式存储（Windows DPAPI）。
    """
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

    # 解密敏感字段（内存中保持明文，落盘保持加密）
    needs_migrate = False
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

    兼容行为：当 UI 传入敏感字段为空字符串时，表示“保持不变”。
    """
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


def reveal_secret(name: str):
    """显式揭示敏感字段（仅在用户主动点击“显示/复制”时调用）。"""
    if name not in SECRET_FIELDS:
        return {"ok": False, "msg": "不支持的字段"}
    s = load_settings()
    return {"ok": True, "value": s.get(name, "")}


def _ensure_runtime_dir():
    os.makedirs(RUNTIME_DIR, exist_ok=True)


def _safe_unlink(path: str):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except FileNotFoundError:
        # 竞态条件：其它清理线程可能已删除该文件；无需告警
        return
    except Exception as e:
        log(f"[WARN] 删除临时文件失败: {path} - {e}")


def _is_windows():
    return os.name == "nt"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_encrypt_bytes(data: bytes) -> bytes:
    if not _is_windows():
        raise RuntimeError("DPAPI 仅支持 Windows")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL

    in_buf = ctypes.create_string_buffer(data, len(data))
    in_blob = _DATA_BLOB(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    if not crypt32.CryptProtectData(ctypes.byref(in_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_decrypt_bytes(data: bytes) -> bytes:
    if not _is_windows():
        raise RuntimeError("DPAPI 仅支持 Windows")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,  # ppszDataDescr（不需要时传 None）
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    in_buf = ctypes.create_string_buffer(data, len(data))
    in_blob = _DATA_BLOB(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _maybe_dpapi_encrypt(value: str) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    if value.startswith("dpapi:"):
        return value
    if not _is_windows():
        # 非 Windows：回退为明文（但本项目主要运行在 Windows）
        return value or ""
    enc = _dpapi_encrypt_bytes(value.encode("utf-8"))
    return "dpapi:" + base64.b64encode(enc).decode("ascii")


def _maybe_dpapi_decrypt(value: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    if not value.startswith("dpapi:"):
        return value
    if not _is_windows():
        return ""
    b64 = value[len("dpapi:"):]
    try:
        raw = base64.b64decode(b64)
        dec = _dpapi_decrypt_bytes(raw)
        return dec.decode("utf-8", errors="replace")
    except Exception:
        return ""


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
  switch-project: {str(s.get('quota_switch_project', True)).lower()}
  switch-preview-model: {str(s.get('quota_switch_preview', True)).lower()}

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
    - name: "gpt-5.1-codex-mini"
      alias: "gpt-5.2-codex"

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


def generate_register_config(s, path: str = None):
    """生成注册脚本配置（运行时临时生成，默认写入临时目录）。"""
    _ensure_runtime_dir()
    path = path or RUNTIME_REGISTER_CONFIG
    # 默认更安全：token_json 放到临时目录；仅当用户显式要求保留时才写到 data/ 目录便于查看
    token_dir = os.path.join(RUNTIME_DIR, "codex_tokens")
    if bool(s.get("keep_token_json")) or bool(s.get("keep_token_json_on_fail")):
        token_dir = os.path.join(DATA_DIR, "codex_tokens")
    cfg = {
        "total_accounts": s["total_accounts"],
        "duckmail_api_base": "https://api.duckmail.sbs",
        "duckmail_bearer": s["duckmail_token"],
        "proxy": s["proxy"],
        "output_file": os.path.join(DATA_DIR, "registered_accounts.txt"),
        "enable_oauth": True,
        "oauth_required": True,
        "oauth_issuer": "https://auth.openai.com",
        "oauth_client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "oauth_redirect_uri": "http://localhost:1455/auth/callback",
        "ak_file": os.path.join(DATA_DIR, "ak.txt"),
        "rk_file": os.path.join(DATA_DIR, "rk.txt"),
        "token_json_dir": token_dir,
        "upload_api_url": f"http://localhost:{s['proxy_port']}/v0/management/auth-files",
        "upload_api_token": s["management_password"],
        # 安全输出开关（注册脚本会读取）
        "store_passwords": bool(s.get("store_passwords", False)),
        "write_ak_rk": bool(s.get("write_ak_rk", False)),
        "keep_token_json": bool(s.get("keep_token_json", False)),
        "keep_token_json_on_fail": bool(s.get("keep_token_json_on_fail", False)),
        "upload_api_verify": True,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _check_proxy_url_format(proxy_url: str):
    if not proxy_url:
        return True, ""
    if re.match(r"^(http|https|socks5)://", str(proxy_url).strip(), re.IGNORECASE):
        return True, ""
    return False, "代理地址格式不正确：应以 http:// / https:// / socks5:// 开头"


def _is_port_free_local(port: int):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", int(port)))
            return True
    except Exception:
        return False


def preflight(kind: str, s: dict):
    """最小预检：在真正启动/注册前，先把常见坑拦在门外。"""
    # 代理地址格式
    ok, msg = _check_proxy_url_format(s.get("proxy", ""))
    if not ok:
        return False, msg

    # proxy 相关
    if kind in ("start_proxy", "autopilot"):
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
            weak_mgmt = _is_weak_secret(
                s.get("management_password", ""),
                min_len=12,
                legacy_values=(LEGACY_DEFAULT_MANAGEMENT_PASSWORD,),
            )
            client_keys = _get_all_client_api_keys(s)
            weak_client_key = any(_is_weak_secret(k, min_len=16) for k in (client_keys or []))
            admin_key = str(s.get("admin_api_key", "") or "").strip()
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

        port = int(s.get("proxy_port", 8317) or 8317)
        # 如果已有服务可连通，认为 OK；否则检查端口是否可用
        if not _proxy_reachable(port) and not _is_port_free_local(port):
            return False, f"端口 {port} 已被占用，请修改“监听端口”或先停止占用进程"

    # register/autopilot 相关
    if kind in ("register", "autopilot"):
        if not str(s.get("duckmail_token", "")).strip():
            return False, "请先在“配置”页填写 DuckMail API Token"
        try:
            import curl_cffi  # noqa: F401
        except Exception:
            return False, "缺少依赖 curl_cffi：请使用“启动.bat”启动（会自动安装依赖）"

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
    share = bool(s.get("cache_shared_across_api_keys", False))
    # 重要：透明网关在“缓存命中”时会短路 upstream（不再触发 CLIProxyAPI 的鉴权逻辑）。
    # 因此网关自身必须知道“哪些客户端 API Key 是合法的”，以避免 share_across_api_keys=True 时
    # 出现“未授权请求也能命中缓存”的安全问题。
    expected_api_keys = set(_get_all_client_api_keys(s) or [])
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
        "share_across_api_keys": share,
        "expected_api_keys": expected_api_keys,
        "vary_headers": vary_headers,
    }


def _extract_bearer_token(auth_header: str) -> str:
    """从 Authorization 头中提取 Bearer token；不符合格式时返回空字符串。"""
    parts = str(auth_header or "").split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def _token_allowed(token: str, allow: set[str]) -> bool:
    """常量时间对比（逐个 compare_digest），避免 set membership 的时序差异。"""
    t = str(token or "").strip()
    if not t:
        return False
    for k in (allow or set()):
        try:
            if secrets.compare_digest(t, str(k or "")):
                return True
        except Exception:
            continue
    return False


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
            "share_across_api_keys": bool(_gateway_cfg.get("share_across_api_keys", False)),
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
    """
    缓存 key 的“调用方隔离维度”。

    - 默认（share_across_api_keys=False）：把 Authorization header 纳入缓存 key，
      避免不同调用方读到彼此的缓存结果（更安全）。
    - 开启共享（share_across_api_keys=True）：忽略 Authorization header，使不同 API Key
      也能命中同一份缓存（适合“可信团队内共享缓存节省额度”的场景）。
    """
    if bool(_gateway_cfg.get("share_across_api_keys", False)):
        return ""
    return str(auth_header or "")


def _cache_key_for_request(method: str, path: str, auth: str, body_bytes: bytes, *, vary: list[tuple[str, str]] | None = None) -> str:
    """
    生成缓存 key：SHA256(method + path + auth + vary_headers + canonical_json(body)).

    设计目标：
    - 默认按 API Key 隔离（auth 进入 key），避免不同调用方“读到别人的缓存”
    - 如需跨 API Key 共享缓存：由 _cache_auth_for_key 决定是否忽略 auth
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
            self._handle()

        def do_POST(self):
            self._handle()

        def do_PUT(self):
            self._handle()

        def do_PATCH(self):
            self._handle()

        def do_DELETE(self):
            self._handle()

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

                resp_obj = msg.get("response")
                if not isinstance(resp_obj, dict):
                    _send_error(400, "missing response object")
                    continue

                # WebSocket mode 本身不需要 stream/background 字段；但上游 SSE 需要 stream=true
                req_obj = dict(resp_obj)
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
                        # 安全护栏：当开启“跨 API Key 共享缓存”时，必须先校验 token，避免未授权命中缓存
                        if bool(_gateway_cfg.get("share_across_api_keys", False)):
                            expected = _gateway_cfg.get("expected_api_keys") or set()
                            if expected:
                                token = _extract_bearer_token(auth_header)
                                if not _token_allowed(token, expected):
                                    allow_read = False
                                    allow_write = False

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
                # 安全护栏：当开启“跨 API Key 共享缓存”时，缓存 key 不包含 Authorization，
                # 若不额外校验，会导致“未授权请求也能命中缓存”。
                if bool(_gateway_cfg.get("share_across_api_keys", False)):
                    expected = _gateway_cfg.get("expected_api_keys") or set()
                    if expected:
                        token = _extract_bearer_token(auth)
                        if not _token_allowed(token, expected):
                            _cache_bypass += 1
                            # 交由 upstream 返回标准 401/403（并避免误返回缓存内容）
                            self._proxy_upstream(body=body)
                            return

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
                        # SSE：按原逻辑透传（不启用 SIE）
                        if str(ctype or "").lower().startswith("text/event-stream"):
                            _cache_bypass += 1
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
        elif kind == "register":
            log(f"[SYS] 注册进程已退出 exit={rc}")
    except Exception:
        pass

    _safe_unlink(config_path)

    # 清理全局引用（避免误判“仍在运行”）
    global proxy_process, register_process, proxy_config_path, register_config_path
    with _lock:
        if kind == "proxy" and proxy_process is proc:
            proxy_process = None
            if proxy_config_path == config_path:
                proxy_config_path = None
        if kind == "register" and register_process is proc:
            register_process = None
            if register_config_path == config_path:
                register_config_path = None


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

        external_port = int(s.get("proxy_port", 8317) or 8317)

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

        # 若启用缓存/网关：对外端口给网关占用；CLIProxyAPI 改为内部端口监听
        gw_enabled = bool(s.get("gateway_enabled", False)) or bool(s.get("cache_enabled", False))
        if gw_enabled:
            _gateway_set_config_from_settings(s)
            _cache_clear()  # 新一轮启动：清空缓存与统计，避免误判

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
        if proxy_process is None or (proxy_process and proxy_process.poll() is not None):
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
    port = int(s.get("proxy_port", 8317) or 8317)
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


def run_register():
    global register_process
    global register_config_path
    with _lock:
        if register_process and register_process.poll() is None:
            return {"ok": False, "msg": "注册任务正在运行中"}
        s = load_settings()
        ok, msg = preflight("register", s)
        if not ok:
            return {"ok": False, "msg": msg}

        # 注册流程依赖代理管理 API 上传 token，强制要求“对外端口”可用（网关模式下 proxy_process 存在并不代表外部可达）
        if not _proxy_reachable(int(s.get("proxy_port", 8317) or 8317)):
            return {"ok": False, "msg": "代理未运行：请先启动代理（或使用“一键全流程”）"}

        register_config_path = RUNTIME_REGISTER_CONFIG
        generate_register_config(s, register_config_path)

        total = s["total_accounts"]
        workers = s.get("max_workers", 3)
        proxy_addr = s["proxy"]
        output = os.path.join(DATA_DIR, "registered_accounts.txt")
        env = os.environ.copy()
        env["AIPROXYHUB_REGISTER_CONFIG"] = register_config_path
        env["PYTHONUNBUFFERED"] = "1"

        # 兼容 PyInstaller：frozen 时 sys.executable=AIProxyHub.exe（无 python -c），
        # 因此使用“同一入口二次启动”的 register-worker 模式来执行注册任务。
        script_path = os.path.abspath(__file__)
        cmd = [sys.executable]
        if not getattr(sys, "frozen", False):
            cmd.append(script_path)
        cmd += [
            "--register-worker",
            "--total-accounts", str(total),
            "--max-workers", str(workers),
            "--proxy", str(proxy_addr or ""),
            "--output-file", str(output or ""),
        ]
        register_process = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env,
        )
        threading.Thread(target=stream_output, args=(register_process, "REG"), daemon=True).start()
        threading.Thread(target=_watch_process_cleanup, args=(register_process, "register", register_config_path), daemon=True).start()
        log(f"[SYS] 开始注册 {total} 个账号（并发 {workers}）")
        return {"ok": True, "msg": f"开始注册 {total} 个账号"}


def stop_register():
    """停止注册任务（如果正在进行）。"""
    global register_process
    global register_config_path
    proc = None
    cfg = None
    with _lock:
        if register_process and register_process.poll() is None:
            proc = register_process
            cfg = register_config_path
            register_process = None
            register_config_path = None
        else:
            # 即便没在运行，也清理遗留运行时配置文件
            cfg = register_config_path
            register_config_path = None
            _safe_unlink(cfg)
            return {"ok": False, "msg": "注册任务未在运行"}

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)

    _safe_unlink(cfg)
    log("[SYS] 注册任务已停止")
    return {"ok": True, "msg": "注册任务已停止"}


def get_accounts():
    accounts = []
    if os.path.isdir(AUTH_DIR):
        for f in sorted(os.listdir(AUTH_DIR)):
            if f.endswith(".json"):
                fp = os.path.join(AUTH_DIR, f)
                try:
                    with open(fp, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    accounts.append({
                        "file": f,
                        "email": f.replace(".json", ""),
                        "type": data.get("type", "unknown"),
                        "size": os.path.getsize(fp),
                        "mtime": int(os.path.getmtime(fp)),
                    })
                except Exception as e:
                    accounts.append({"file": f, "email": f.replace(".json", ""), "type": "error", "error": str(e)})
    return accounts


def delete_account(filename):
    # 安全防护：防止路径穿越删除任意文件（launcher 无鉴权但仅绑定 127.0.0.1）
    if not filename or not isinstance(filename, str):
        return {"ok": False, "msg": "文件名为空"}
    if "/" in filename or "\\" in filename or filename.startswith(".") or ".." in filename:
        return {"ok": False, "msg": "非法文件名"}
    if not filename.endswith(".json"):
        return {"ok": False, "msg": "非法文件类型"}

    fp = os.path.join(AUTH_DIR, filename)
    abs_fp = os.path.normcase(os.path.abspath(fp))
    abs_dir = os.path.normcase(os.path.abspath(AUTH_DIR))
    if not abs_fp.startswith(abs_dir + os.sep):
        return {"ok": False, "msg": "非法路径"}
    if os.path.exists(fp):
        os.remove(fp)
        log(f"[SYS] 已删除账号: {filename}")
        return {"ok": True, "msg": f"已删除 {filename}"}
    return {"ok": False, "msg": "文件不存在"}


def get_data_files():
    result = {}
    for name in ["registered_accounts.txt", "ak.txt", "rk.txt"]:
        fp = os.path.join(DATA_DIR, name)
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            # Web UI 默认脱敏展示（文件本体仍保留在磁盘，必要时用户可自行打开查看）
            if name == "registered_accounts.txt":
                lines = []
                for line in content.splitlines():
                    parts = line.split("----")
                    if len(parts) >= 4:
                        # email----chatgpt_pwd----email_pwd----oauth=...
                        parts[1] = "***"
                        parts[2] = "***"
                        line = "----".join(parts)
                    lines.append(line)
                content = "\n".join(lines)
            else:
                # ak.txt / rk.txt：逐行脱敏
                masked = []
                for line in content.splitlines():
                    t = line.strip()
                    if not t:
                        masked.append("")
                    elif len(t) <= 12:
                        masked.append("***")
                    else:
                        masked.append(t[:6] + "..." + t[-4:])
                content = "\n".join(masked)
            result[name] = content
        else:
            result[name] = ""
    return result


def secure_cleanup_outputs():
    """
    安全清理 data 目录中“可选敏感输出”：
    - registered_accounts.txt：脱敏覆盖（不删除，保留账号列表）
    - ak.txt / rk.txt：删除（明文 token）
    - codex_tokens/*.json：删除（token_json 临时文件/调试产物）
    - %TEMP%\\AIProxyHub\\*.runtime.*：删除（异常退出可能残留；若相关进程仍在运行则跳过）
    """
    removed = []
    redacted_lines = 0
    removed_token_json = 0

    # 1) registered_accounts.txt 脱敏覆盖（避免历史文件残留密码）
    reg_fp = os.path.join(DATA_DIR, "registered_accounts.txt")
    if os.path.exists(reg_fp):
        try:
            with open(reg_fp, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            lines = []
            for line in content.splitlines():
                parts = line.split("----")
                if len(parts) >= 4:
                    if parts[1] != "***" or parts[2] != "***":
                        redacted_lines += 1
                    parts[1] = "***"
                    parts[2] = "***"
                    line = "----".join(parts)
                lines.append(line)
            with open(reg_fp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if content.endswith("\n") else ""))
        except Exception as e:
            return {"ok": False, "msg": f"脱敏 registered_accounts.txt 失败: {e}"}

    # 2) 删除明文 token 文件
    for name in ("ak.txt", "rk.txt"):
        fp = os.path.join(DATA_DIR, name)
        if os.path.exists(fp):
            try:
                os.remove(fp)
                removed.append(name)
            except Exception as e:
                return {"ok": False, "msg": f"删除 {name} 失败: {e}"}

    # 3) 删除 token_json 目录（data 与 temp 都清理，避免残留）
    for token_dir in (os.path.join(DATA_DIR, "codex_tokens"), os.path.join(RUNTIME_DIR, "codex_tokens")):
        if not os.path.isdir(token_dir):
            continue
        try:
            for root, _, files in os.walk(token_dir):
                for fn in files:
                    if fn.endswith(".json"):
                        try:
                            os.remove(os.path.join(root, fn))
                            removed_token_json += 1
                        except Exception:
                            pass
            # 尝试删除空目录（自底向上）
            for root, dirs, _files in os.walk(token_dir, topdown=False):
                for d in dirs:
                    p = os.path.join(root, d)
                    try:
                        os.rmdir(p)
                    except Exception:
                        pass
            try:
                os.rmdir(token_dir)
            except Exception:
                pass
        except Exception:
            pass

    # 4) 删除运行时临时配置（避免异常退出导致敏感配置长期留在临时目录）
    runtime_skipped = []
    # 注意：proxy 可能是“外部进程”在运行（不一定由本 launcher 启动）。
    # 为避免误删其运行时配置，这里额外用端口连通性作为护栏。
    try:
        proxy_port = int(load_settings().get("proxy_port", 8317) or 8317)
    except Exception:
        proxy_port = 8317
    runtime_targets = [
        ("cli-proxy-api.runtime.yaml", RUNTIME_PROXY_CONFIG, proxy_process),
        ("register.runtime.json", RUNTIME_REGISTER_CONFIG, register_process),
        # 历史/测试残留：可能包含 duckmail token 等敏感字段
        ("_test_register_cfg.json", os.path.join(RUNTIME_DIR, "_test_register_cfg.json"), register_process),
    ]
    for label, path, proc in runtime_targets:
        try:
            running = proc is not None and proc.poll() is None
        except Exception:
            running = False
        if label == "cli-proxy-api.runtime.yaml":
            try:
                if _proxy_reachable(proxy_port):
                    running = True
            except Exception:
                pass
        if running:
            runtime_skipped.append(label)
            continue
        if path and os.path.exists(path):
            try:
                os.remove(path)
                removed.append(label)
            except Exception:
                pass

    # 尝试删除空的运行时目录
    try:
        if os.path.isdir(RUNTIME_DIR) and not os.listdir(RUNTIME_DIR):
            os.rmdir(RUNTIME_DIR)
    except Exception:
        pass

    extra = ""
    if runtime_skipped:
        extra = f"（跳过 {len(runtime_skipped)} 个运行时配置：{', '.join(runtime_skipped)} 仍在使用）"
    msg = f"已清理：脱敏 {redacted_lines} 行、删除 {len(removed)} 个文件、删除 {removed_token_json} 个 token_json{extra}"
    log(f"[SYS] 安全清理输出完成：{msg}")
    return {"ok": True, "msg": msg}


# --- 自动驾驶状态 ---
autopilot_state = {"phase": "idle", "msg": ""}  # idle / proxy / cleanup / register / done / error


def cleanup_accounts():
    """清理无效账号：过期/disabled/JSON损坏/缺少关键字段"""
    def _parse_expired(value):
        if value is None:
            return None
        if isinstance(value, (int, float)) and value > 0:
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except Exception:
                return None
        s = str(value).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    removed = []
    kept = 0
    if not os.path.isdir(AUTH_DIR):
        return {"ok": False, "msg": "认证目录不存在"}
    now = datetime.now(timezone.utc)
    for f in os.listdir(AUTH_DIR):
        if not f.endswith(".json"):
            continue
        fp = os.path.join(AUTH_DIR, f)
        invalid_reason = None
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("disabled"):
                invalid_reason = "disabled"
            elif not data.get("access_token") and not data.get("refresh_token"):
                invalid_reason = "缺少token"
            elif data.get("expired"):
                exp_str = str(data["expired"])
                exp_dt = _parse_expired(data.get("expired"))
                # 更安全：无法解析 expired 时不删（避免误伤有效账号）
                if exp_dt:
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if exp_dt < now:
                        invalid_reason = f"已过期({exp_str[:19]})"
        except (json.JSONDecodeError, Exception):
            invalid_reason = "JSON损坏"
        if invalid_reason:
            os.remove(fp)
            removed.append(f"{f} ({invalid_reason})")
            log(f"[SYS] 清理无效账号: {f} - {invalid_reason}")
        else:
            kept += 1
    if removed:
        return {"ok": True, "msg": f"已清理 {len(removed)} 个无效账号，保留 {kept} 个"}
    return {"ok": True, "msg": f"没有无效账号，全部 {kept} 个均有效"}


def _proxy_reachable(port):
    """检查代理端口是否可连通（主要用于等待代理就绪/端口占用预判）。"""
    try:
        import urllib.request, urllib.error
        urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2)
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


def auto_pilot():
    """自动驾驶：启动代理 → 等就绪 → 注册 → 完成"""
    global autopilot_state
    s = load_settings()
    ok, msg = preflight("autopilot", s)
    if not ok:
        autopilot_state = {"phase": "error", "msg": msg}
        log(f"[AUTO] 预检失败，自动流程终止: {msg}")
        return

    # 阶段1：启动代理
    autopilot_state = {"phase": "proxy", "msg": "正在启动代理服务..."}
    log("[AUTO] ▶ 阶段1：启动代理服务")
    if autopilot_cancel.is_set():
        autopilot_state = {"phase": "idle", "msg": "已取消"}
        log("[AUTO] 已取消")
        return

    # 检查代理是否已在运行
    already_running = proxy_process is not None and proxy_process.poll() is None
    if not already_running:
        # 也检查端口是否被外部进程占用
        already_running = _proxy_reachable(s["proxy_port"])

    if already_running:
        log("[AUTO] 检测到代理已在运行，跳过启动")
    else:
        r = start_proxy()
        if not r["ok"]:
            autopilot_state = {"phase": "error", "msg": r["msg"]}
            return
        # 等代理就绪
        ready = False
        for i in range(20):
            if autopilot_cancel.is_set():
                autopilot_state = {"phase": "idle", "msg": "已取消"}
                log("[AUTO] 已取消")
                return
            time.sleep(1)
            if proxy_process and proxy_process.poll() is not None:
                autopilot_state = {"phase": "error", "msg": "代理进程异常退出"}
                log("[AUTO] 代理进程异常退出")
                return
            try:
                if _proxy_reachable(s["proxy_port"]):
                    ready = True
                    break
            except Exception:
                pass
        if not ready:
            autopilot_state = {"phase": "error", "msg": "代理启动超时"}
            log("[AUTO] 代理启动超时")
            return

    # 阶段2：清理无效账号
    autopilot_state = {"phase": "cleanup", "msg": "正在清理无效账号..."}
    log("[AUTO] ▶ 阶段2：清理无效账号")
    cleanup_accounts()
    if autopilot_cancel.is_set():
        autopilot_state = {"phase": "idle", "msg": "已取消"}
        log("[AUTO] 已取消")
        return

    # 阶段3：注册
    autopilot_state = {"phase": "register", "msg": f"正在注册 {s['total_accounts']} 个账号..."}
    log("[AUTO] ▶ 阶段3：开始批量注册")
    rr = run_register()
    if not rr.get("ok"):
        autopilot_state = {"phase": "error", "msg": rr.get("msg", "注册启动失败")}
        log(f"[AUTO] 注册启动失败: {autopilot_state['msg']}")
        return

    # 等注册完成
    while register_process and register_process.poll() is None:
        if autopilot_cancel.is_set():
            stop_register()
            autopilot_state = {"phase": "idle", "msg": "已取消"}
            log("[AUTO] 已取消")
            return
        time.sleep(2)

    n = len(get_accounts())
    autopilot_state = {"phase": "done", "msg": f"全部就绪！{n} 个账号在线"}
    log(f"[AUTO] ✓ 全流程完成，{n} 个账号可用")
    log(f"[AUTO] ✓ API: http://localhost:{s['proxy_port']}/v1  Key: {s['api_key']}")


def start_auto_pilot():
    """后台线程启动自动驾驶"""
    global autopilot_state
    with _lock:
        if autopilot_state["phase"] not in ("idle", "done", "error"):
            return {"ok": False, "msg": "自动流程正在运行中"}
        autopilot_cancel.clear()
        autopilot_state = {"phase": "proxy", "msg": "正在启动..."}
        t = threading.Thread(target=auto_pilot, daemon=True)
        t.start()
        return {"ok": True, "msg": "自动流程已启动"}


def stop_auto_pilot():
    """请求停止自动流程（尽力而为：会终止注册子进程，但默认不强制停止代理）。"""
    global autopilot_state
    autopilot_cancel.set()
    stop_register()
    with _lock:
        if autopilot_state.get("phase") not in ("idle", "done", "error"):
            autopilot_state = {"phase": "idle", "msg": "已取消"}
    log("[AUTO] 已请求停止自动流程")
    return {"ok": True, "msg": "已请求停止自动流程"}


# --- 自动监控：配额不足自动注册，额度用完自动删除 ---
_monitor_running = False
_monitor_cancel = threading.Event()
_monitor_stats_lock = threading.Lock()
_monitor_stats = {
    "last_run_ts": 0.0,
    "last_ok": False,
    "last_msg": "",
    "last_total": 0,
    "last_active": 0,
    "last_pct": 0,
    "last_candidates": 0,
    "last_deleted": 0,
}


def _monitor_get_cfg() -> dict:
    """
    读取并规范化监控配置（避免异常值导致过快轮询/全删空等风险）。

    说明：这些字段非敏感，可安全用于 UI 展示/日志（但仍避免输出任何 key 明文）。
    """
    s = load_settings()
    interval = int(s.get("monitor_interval_seconds", 60) or 60)
    # 防抖：避免过低间隔造成 CPA 管理 API 压力/日志刷屏
    interval = max(10, min(interval, 24 * 3600))

    threshold = int(s.get("monitor_low_available_threshold_pct", 20) or 20)
    threshold = max(0, min(threshold, 100))

    min_keep = int(s.get("monitor_min_keep_accounts", 1) or 0)
    min_keep = max(0, min_keep)

    return {
        "enabled": bool(s.get("monitor_enabled", False)),
        "interval_seconds": interval,
        "low_available_threshold_pct": threshold,
        "auto_register_enabled": bool(s.get("monitor_auto_register_enabled", True)),
        "prune_zero_quota_enabled": bool(s.get("monitor_prune_zero_quota_enabled", True)),
        "prune_only_usage_limit_reached": bool(s.get("monitor_prune_only_usage_limit_reached", True)),
        "min_keep_accounts": min_keep,
        "dry_run": bool(s.get("monitor_dry_run", False)),
    }


def _is_usage_limit_reached_account(acc: dict) -> bool:
    """
    判定该账号是否属于“额度为零/用量上限触发”的不可用类型。

    CPA 的返回字段可能随版本变化：这里做宽松匹配，仅在明确命中时返回 True。
    """
    t = str(acc.get("error_type", "") or "").lower()
    c = str(acc.get("error_code", "") or "").lower()
    st = str(acc.get("status", "") or "").lower()
    sm = str(acc.get("status_message", "") or "").lower()
    hay = " ".join([t, c, st, sm])
    return ("usage_limit" in hay) or ("insufficient_quota" in hay) or ("quota_exceeded" in hay)


def _monitor_prune_zero_quota(q: dict, cfg: dict) -> dict:
    """
    基于 _query_quota() 的结果，删除“额度为零”的账号文件。
    返回：{ok, candidates, deleted, msg}
    """
    if not cfg.get("prune_zero_quota_enabled"):
        return {"ok": True, "candidates": 0, "deleted": 0, "msg": "prune disabled"}
    if not (q or {}).get("ok"):
        return {"ok": False, "candidates": 0, "deleted": 0, "msg": "quota query failed"}

    candidates: list[str] = []
    for acc in q.get("accounts", []) or []:
        if acc.get("available", True):
            continue
        if cfg.get("prune_only_usage_limit_reached", True) and not _is_usage_limit_reached_account(acc):
            continue
        fn = str(acc.get("file", "") or "").strip()
        if not fn:
            # 兜底：历史兼容（email.json）
            email = str(acc.get("email", "") or "").strip()
            if email:
                fn = email if email.endswith(".json") else (email + ".json")
        if fn:
            candidates.append(fn)

    candidates = list(dict.fromkeys(candidates))  # 去重且保持顺序
    if not candidates:
        return {"ok": True, "candidates": 0, "deleted": 0, "msg": "no candidates"}

    try:
        current_total = len(get_accounts())
    except Exception:
        # 极端兜底：无法统计时，不启用 min_keep 限制
        current_total = 0

    min_keep = int(cfg.get("min_keep_accounts", 0) or 0)
    if current_total > 0 and min_keep > 0:
        max_delete = max(0, current_total - min_keep)
        if max_delete <= 0:
            return {"ok": True, "candidates": len(candidates), "deleted": 0, "msg": f"min_keep={min_keep} blocks delete"}
        candidates = candidates[:max_delete]

    if cfg.get("dry_run"):
        return {"ok": True, "candidates": len(candidates), "deleted": 0, "msg": "dry-run"}

    deleted = 0
    for fn in candidates:
        r = delete_account(fn)
        if r.get("ok"):
            deleted += 1
    return {"ok": True, "candidates": len(candidates), "deleted": deleted, "msg": "done"}


def _monitor_loop():
    """后台监控线程：定期检查配额；可选：自动注册 / 自动清理额度为零账号。"""
    global _monitor_running
    cfg0 = _monitor_get_cfg()
    log(
        "[MONITOR] 自动监控已启动"
        f"（阈值 {cfg0.get('low_available_threshold_pct')}%，间隔 {cfg0.get('interval_seconds')}s，"
        f"自动注册={'开' if cfg0.get('auto_register_enabled') else '关'}，"
        f"自动清理={'开' if cfg0.get('prune_zero_quota_enabled') else '关'}"
        f"{'，dry-run' if cfg0.get('dry_run') else ''}）"
    )
    while not _monitor_cancel.is_set():
        cfg = _monitor_get_cfg()
        try:
            q = _query_quota()
            if not q.get("ok"):
                with _monitor_stats_lock:
                    _monitor_stats.update({
                        "last_run_ts": time.time(),
                        "last_ok": False,
                        "last_msg": str(q.get("msg", "quota query failed") or ""),
                        "last_candidates": 0,
                        "last_deleted": 0,
                    })
                _monitor_cancel.wait(int(cfg.get("interval_seconds", 60) or 60))
                continue

            total = q["total"]
            active = q["active"]
            pct = q["pct"]
            log(f"[MONITOR] 配额检查: {active}/{total} 可用 ({pct}%)")

            # 删除已耗尽额度的账号（默认：仅 usage_limit_reached）
            prune = _monitor_prune_zero_quota(q, cfg)
            if prune.get("ok") and prune.get("candidates", 0) and not cfg.get("dry_run"):
                log(f"[MONITOR] 已删除 {prune.get('deleted', 0)} 个额度为零的账号（候选 {prune.get('candidates', 0)}）")
            elif prune.get("ok") and prune.get("candidates", 0) and cfg.get("dry_run"):
                log(f"[MONITOR] dry-run：发现 {prune.get('candidates', 0)} 个额度为零候选账号（未删除）")

            # 低于阈值 且当前没有注册任务 → 自动注册（可开关）
            threshold = int(cfg.get("low_available_threshold_pct", 20) or 20)
            if cfg.get("auto_register_enabled") and pct < threshold and not (register_process and register_process.poll() is None):
                s = load_settings()
                batch = int(s.get("total_accounts", 50))
                log(
                    f"[MONITOR] 可用率 {pct}% < {threshold}%，"
                    f"自动启动注册 {batch} 个账号（并发 {s.get('max_workers', 5)}）"
                )
                r = run_register()
                log(f"[MONITOR] 注册触发结果: {r.get('msg', '?')}")

            with _monitor_stats_lock:
                _monitor_stats.update({
                    "last_run_ts": time.time(),
                    "last_ok": True,
                    "last_msg": "",
                    "last_total": int(total or 0),
                    "last_active": int(active or 0),
                    "last_pct": int(pct or 0),
                    "last_candidates": int(prune.get("candidates", 0) or 0),
                    "last_deleted": int(prune.get("deleted", 0) or 0),
                })
        except Exception as e:
            log(f"[MONITOR] 监控异常: {e}")

        _monitor_cancel.wait(int(cfg.get("interval_seconds", 60) or 60))

    _monitor_running = False
    log("[MONITOR] 自动监控已停止")


def start_monitor():
    """启动自动监控线程。"""
    global _monitor_running
    with _lock:
        if _monitor_running:
            return {"ok": False, "msg": "监控已在运行中"}
        _monitor_cancel.clear()
        _monitor_running = True
        t = threading.Thread(target=_monitor_loop, daemon=True)
        t.start()
        cfg = _monitor_get_cfg()
        return {
            "ok": True,
            "msg": (
                "自动监控已启动"
                f"（阈值 {cfg.get('low_available_threshold_pct')}%，间隔 {cfg.get('interval_seconds')}s，"
                f"自动清理={'开' if cfg.get('prune_zero_quota_enabled') else '关'}"
                f"{'，dry-run' if cfg.get('dry_run') else ''}）"
            ),
        }


def stop_monitor():
    """停止自动监控线程。"""
    global _monitor_running
    _monitor_cancel.set()
    stop_register()
    _monitor_running = False
    log("[MONITOR] 已请求停止监控")
    return {"ok": True, "msg": "监控已停止"}


def get_monitor_status():
    cfg = _monitor_get_cfg()
    with _monitor_stats_lock:
        st = dict(_monitor_stats)
    return {"ok": True, "running": _monitor_running, "cfg": cfg, "stats": st}


# ==================== HTML 前端 ====================
HTML_PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIProxyHub</title>
<style>
:root{--bg:#f5f7fa;--card:#ffffff;--border:#e2e8f0;--accent:#6366f1;--accent2:#4f46e5;--green:#16a34a;--red:#dc2626;--orange:#d97706;--text:#1e293b;--dim:#64748b;--sidebar-w:220px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);display:flex;min-height:100vh}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:3px}

/* Sidebar */
.sidebar{width:var(--sidebar-w);background:var(--card);border-right:1px solid var(--border);display:flex;flex-direction:column;position:fixed;height:100vh;z-index:10}
.logo{padding:24px 20px;font-size:20px;font-weight:700;background:linear-gradient(135deg,var(--accent),#7c3aed);-webkit-background-clip:text;-webkit-text-fill-color:transparent;border-bottom:1px solid var(--border)}
.nav{flex:1;padding:12px 0}
.nav a{display:flex;align-items:center;gap:10px;padding:11px 20px;color:var(--dim);text-decoration:none;font-size:14px;transition:all .15s;border-left:3px solid transparent;cursor:pointer}
.nav a:hover{color:var(--text);background:rgba(99,102,241,.06)}
.nav a.active{color:var(--accent2);border-left-color:var(--accent);background:rgba(99,102,241,.08)}
.nav a svg{width:18px;height:18px;flex-shrink:0}
.sidebar-footer{padding:16px 20px;border-top:1px solid var(--border);font-size:11px;color:var(--dim)}

/* Main */
.main{margin-left:var(--sidebar-w);flex:1;padding:28px 32px;min-height:100vh}
.page{display:none}.page.active{display:block}
.page-title{font-size:22px;font-weight:600;margin-bottom:20px}

/* Cards */
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:22px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.card-title{font-size:14px;color:var(--dim);margin-bottom:14px;display:flex;align-items:center;gap:8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}

/* Stats row */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.stat-label{font-size:12px;color:var(--dim);margin-bottom:6px}
.stat-value{font-size:26px;font-weight:700}
.stat-value.on{color:var(--green)}.stat-value.off{color:var(--dim)}.stat-value.num{color:var(--accent2)}

/* Form */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.form-grid.three{grid-template-columns:1fr 1fr 1fr}
.form-group{display:flex;flex-direction:column;gap:5px}
.form-group.full{grid-column:1/-1}
.form-group label{font-size:12px;color:var(--dim);font-weight:500}
.form-group input,.form-group select{padding:9px 12px;background:#f8fafc;border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;outline:none;transition:border .2s}
.form-group input:focus,.form-group select:focus{border-color:var(--accent)}
.form-group .hint{font-size:11px;color:#94a3b8}
.form-group input[type=checkbox]{width:16px;height:16px}
.check-row{display:flex;align-items:center;gap:8px;padding:4px 0}
.check-row label{font-size:13px;color:var(--text)}

/* Buttons */
.btn{padding:9px 20px;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px}
.btn:hover{filter:brightness(1.15);transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn-primary{background:var(--accent);color:#fff}
.btn-green{background:var(--green);color:#fff}
.btn-red{background:var(--red);color:#fff}
.btn-orange{background:var(--orange);color:#fff}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--dim)}
.btn-sm{padding:6px 14px;font-size:12px}
.btn-row{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}

/* Log */
.log-box{background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:14px;height:400px;overflow-y:auto;font-family:'Cascadia Code','Fira Code',Consolas,monospace;font-size:12px;line-height:1.8}
.log-box .t{color:#94a3b8}.log-box .sys{color:var(--accent2)}.log-box .proxy{color:#0284c7}.log-box .reg{color:#7c3aed}.log-box .err{color:var(--red)}.log-box .ok{color:var(--green)}

/* Table */
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;padding:10px 12px;color:var(--dim);font-weight:500;font-size:12px;border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.5px}
.tbl td{padding:10px 12px;border-bottom:1px solid var(--border)}
.tbl tr:hover td{background:rgba(99,102,241,.04)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.badge-green{background:rgba(22,163,74,.1);color:var(--green)}
.badge-blue{background:rgba(99,102,241,.1);color:var(--accent2)}

/* Toast */
.toast{position:fixed;top:20px;right:20px;padding:12px 22px;border-radius:10px;color:#fff;font-size:13px;font-weight:500;opacity:0;transition:opacity .3s;z-index:999;pointer-events:none}
.toast.show{opacity:1}.toast.ok{background:var(--green)}.toast.err{background:var(--red)}

/* Info banner */
.info-banner{background:linear-gradient(135deg,rgba(99,102,241,.06),rgba(168,85,247,.06));border:1px solid rgba(99,102,241,.2);border-radius:10px;padding:16px 20px;margin-top:14px;font-size:13px;line-height:1.8}
.info-banner code{background:rgba(99,102,241,.1);padding:2px 8px;border-radius:4px;color:var(--accent2);font-family:monospace}
.info-banner a{color:var(--accent2);text-decoration:none;font-weight:600}
.info-banner a:hover{text-decoration:underline}

/* Empty state */
.empty{text-align:center;padding:40px;color:var(--dim);font-size:14px}
/* Autopilot progress */
.ap-bar{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 22px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.ap-steps{display:flex;gap:0;align-items:center;margin-top:12px}
.ap-step{flex:1;text-align:center;position:relative;font-size:12px;color:var(--dim);padding-top:28px}
.ap-step::before{content:'';position:absolute;top:8px;left:50%;width:14px;height:14px;border-radius:50%;background:#e2e8f0;transform:translateX(-50%);transition:all .3s}
.ap-step::after{content:'';position:absolute;top:14px;left:0;right:0;height:2px;background:#e2e8f0;z-index:0}
.ap-step:first-child::after{left:50%}.ap-step:last-child::after{right:50%}
.ap-step.active::before{background:var(--accent);box-shadow:0 0 8px rgba(99,102,241,.3)}
.ap-step.done::before{background:var(--green);box-shadow:0 0 6px rgba(22,163,74,.25)}
.ap-step.err::before{background:var(--red)}
.ap-step.active,.ap-step.done{color:var(--text)}
</style>
</head>
<body>

<div class="sidebar">
  <div class="logo">AIProxyHub</div>
  <nav class="nav">
    <a data-page="dashboard" class="active">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
      仪表盘
    </a>
    <a data-page="settings">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.32 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      配置
    </a>
    <a data-page="register">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg>
      批量注册
    </a>
    <a data-page="accounts">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
      账号管理
    </a>
    <a data-page="logs">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
      运行日志
    </a>
  </nav>
  <div class="sidebar-footer">
    CLIProxyAPI v6.8.51<br>AIProxyHub v__APP_VERSION__
  </div>
</div>

<div class="main">

<!-- ====== Dashboard ====== -->
<div class="page active" id="pg-dashboard">
  <h1 class="page-title">仪表盘</h1>

  <div class="ap-bar" id="ap-bar">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div><b style="font-size:14px" id="ap-title">一键全流程</b><br><span style="font-size:12px;color:var(--dim)" id="ap-msg">点击下方按钮启动自动流程</span></div>
      <button class="btn btn-primary" id="ap-btn" onclick="doAutoPilot()">一键启动</button>
    </div>
    <div class="ap-steps">
      <div class="ap-step" id="ap-s1">启动代理</div>
      <div class="ap-step" id="ap-s2">清理无效</div>
      <div class="ap-step" id="ap-s3">批量注册</div>
      <div class="ap-step" id="ap-s4">就绪可用</div>
    </div>
  </div>

  <div class="stats">
    <div class="stat"><div class="stat-label">代理服务</div><div class="stat-value" id="d-proxy">--</div></div>
    <div class="stat"><div class="stat-label">注册任务</div><div class="stat-value" id="d-reg">--</div></div>
    <div class="stat"><div class="stat-label">已注册账号</div><div class="stat-value num" id="d-count">--</div></div>
    <div class="stat"><div class="stat-label">代理端口</div><div class="stat-value num" id="d-port">--</div></div>
    <div class="stat"><div class="stat-label">总请求数</div><div class="stat-value num" id="d-usage-req">--</div></div>
    <div class="stat"><div class="stat-label">总 Tokens</div><div class="stat-value num" id="d-usage-tokens">--</div></div>
    <div class="stat"><div class="stat-label">缓存命中</div><div class="stat-value num" id="d-cache-hit">--</div></div>
    <div class="stat"><div class="stat-label">命中率</div><div class="stat-value num" id="d-cache-ratio">--</div></div>
  </div>
  <div class="card">
    <div class="card-title">手动操作</div>
    <div class="btn-row">
      <button class="btn btn-green" onclick="api('start_proxy')">启动代理</button>
      <button class="btn btn-ghost" onclick="api('restart_proxy')">重启代理</button>
      <button class="btn btn-red" onclick="api('stop_proxy')">停止代理</button>
      <button class="btn btn-primary" onclick="api('register')">开始注册</button>
      <button class="btn btn-red" onclick="api('stop_register')">停止注册</button>
      <button class="btn btn-orange" onclick="api('cleanup')">清理无效账号</button>
      <button class="btn btn-ghost" onclick="api('stop_autopilot')">停止一键流程</button>
      <button class="btn btn-ghost" onclick="api('cache_clear')">清空缓存</button>
      <button class="btn btn-ghost" onclick="openPanel()">CPA 管理面板</button>
      <button class="btn btn-ghost" onclick="openUsage()">CPA 使用统计</button>
    </div>
  </div>
  <div class="info-banner" id="api-info" style="display:none">
    <b>接入信息</b><br>
    API Base URL: <code id="i-url"></code><br>
    API Key: <code id="i-key"></code> <button class="btn btn-ghost btn-sm" id="btn-copy-key" onclick="copyApiKey()">复制</button><br>
    管理密码: <code id="i-mgmt"></code> <button class="btn btn-ghost btn-sm" id="btn-copy-mgmt" onclick="copyManagementPassword()">复制</button><br>
    管理面板: <a id="i-panel" href="#" target="_blank">打开 →</a>
  </div>
</div>

<!-- ====== Settings ====== -->
<div class="page" id="pg-settings">
  <h1 class="page-title">配置</h1>
  <div class="card">
    <div class="card-title">注册相关</div>
    <div class="form-grid">
      <div class="form-group full"><label>DuckMail API Token</label><input id="s-duckmail_token" type="password" placeholder="dk_xxxxxxxx..."><div class="hint">从 duckmail.sbs 获取（已保存时留空表示不变）</div></div>
      <div class="form-group"><label>代理地址</label><input id="s-proxy" placeholder="http://127.0.0.1:7890"></div>
      <div class="form-group"><label>注册数量</label><input id="s-total_accounts" type="number"></div>
      <div class="form-group"><label>并发数</label><input id="s-max_workers" type="number"></div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">代理服务</div>
    <div class="form-grid three">
      <div class="form-group"><label>监听地址</label><input id="s-proxy_host" placeholder="127.0.0.1"></div>
      <div class="form-group"><label>监听端口</label><input id="s-proxy_port" type="number"></div>
      <div class="form-group"><label>路由策略</label>
        <select id="s-routing_strategy"><option value="round-robin">round-robin（轮询）</option><option value="fill-first">fill-first（填满优先）</option></select>
      </div>
    </div>
    <div class="form-grid" style="margin-top:14px">
      <div class="form-group"><label>管理密码</label><input id="s-management_password" type="password"><div class="hint">CPA 管理 API + 注册上传共用（已保存时留空表示不变）</div></div>
      <div class="form-group"><label>API Key</label><input id="s-api_key"><div class="hint">客户端连接代理用的密钥（已保存时留空表示不变）</div></div>
      <div class="form-group"><label>请求重试次数</label><input id="s-request_retry" type="number"></div>
    </div>
    <div style="margin-top:14px;display:flex;gap:20px">
      <div class="check-row"><input type="checkbox" id="s-quota_switch_project"><label for="s-quota_switch_project">配额超限自动切换账号</label></div>
      <div class="check-row"><input type="checkbox" id="s-quota_switch_preview"><label for="s-quota_switch_preview">配额超限切换预览模型</label></div>
      <div class="check-row"><input type="checkbox" id="s-debug"><label for="s-debug">调试日志</label></div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">账号自动维护（额度为零自动删除）</div>
    <div style="font-size:12px;color:var(--dim);line-height:1.9;margin-bottom:10px">
      启用后：AIProxyHub 会在后台定期调用 CPA 管理 API 查询账号状态，并按规则自动清理“额度为零”的账号文件（<code>~/.cli-proxy-api/*.json</code>）。<br>
      建议首次先开启 <b>Dry-run</b> 观察候选数量，确认无误后再关闭 Dry-run 执行真实删除。
    </div>
    <div class="check-row"><input type="checkbox" id="s-monitor_enabled"><label for="s-monitor_enabled">启用后台监控（保存后自动生效）</label></div>
    <div class="check-row"><input type="checkbox" id="s-monitor_prune_zero_quota_enabled"><label for="s-monitor_prune_zero_quota_enabled">额度为零自动删除</label></div>
    <div class="check-row"><input type="checkbox" id="s-monitor_prune_only_usage_limit_reached"><label for="s-monitor_prune_only_usage_limit_reached">仅删除 <code>usage_limit_reached</code> 类型（更安全）</label></div>
    <div class="check-row"><input type="checkbox" id="s-monitor_auto_register_enabled"><label for="s-monitor_auto_register_enabled">可用率过低自动注册新账号</label></div>
    <div class="check-row"><input type="checkbox" id="s-monitor_dry_run"><label for="s-monitor_dry_run">Dry-run（只统计/不删除）</label></div>
    <div class="form-grid three" style="margin-top:12px">
      <div class="form-group"><label>检查间隔（秒）</label><input id="s-monitor_interval_seconds" type="number"></div>
      <div class="form-group"><label>可用率阈值（%）</label><input id="s-monitor_low_available_threshold_pct" type="number"></div>
      <div class="form-group"><label>最少保留账号数</label><input id="s-monitor_min_keep_accounts" type="number"></div>
    </div>
    <div class="hint" style="margin-top:10px">
      注意：删除账号会直接移除对应的 <code>.json</code> 文件；若你希望“额度重置后继续使用”，请关闭自动删除或开启“仅删除 usage_limit_reached”。<br>
      该功能不会输出任何 API Key 明文；若看到异常删除，请立即关闭监控并在日志中定位原因。
    </div>
  </div>
  <div class="card">
    <div class="card-title">安全输出（高级）</div>
    <div style="font-size:12px;color:var(--dim);line-height:1.9;margin-bottom:10px">
      默认更安全：不把密码/token 明文写到 data 目录。仅在你明确需要“落盘保存”时再开启以下选项。
    </div>
    <div class="check-row"><input type="checkbox" id="s-store_passwords"><label for="s-store_passwords">在 registered_accounts.txt 中保存 ChatGPT/邮箱密码（不推荐）</label></div>
    <div class="check-row"><input type="checkbox" id="s-write_ak_rk"><label for="s-write_ak_rk">生成 ak.txt / rk.txt 明文 token（不推荐）</label></div>
    <div class="check-row"><input type="checkbox" id="s-keep_token_json"><label for="s-keep_token_json">保留 token_json 文件（不推荐）</label></div>
    <div class="check-row"><input type="checkbox" id="s-keep_token_json_on_fail"><label for="s-keep_token_json_on_fail">上传失败时保留 token_json（调试用）</label></div>
    <div class="hint" style="margin-top:10px">
      提示：token_json 默认写到 %TEMP%\AIProxyHub\codex_tokens 并在上传成功后自动清理；开启“保留”后会写入 data\codex_tokens 便于查看。
    </div>
  </div>
  <div class="card">
    <div class="card-title">缓存池（跨账号节省额度）</div>
    <div style="font-size:12px;color:var(--dim);line-height:1.9;margin-bottom:10px">
      开启后：AIProxyHub 会在 <code>监听端口</code> 启动透明网关，CLIProxyAPI 改为内部端口监听；对 <code>/v1/responses</code> / <code>/v1/chat/completions</code> 做去重缓存。缓存命中不会消耗账号额度。<br>
      默认仅缓存非 stream；如启用 <code>stream 缓存</code>，则会把 SSE/WS 事件流写入内存并在命中时快速回放（更适合重复任务/压测）。
    </div>
	    <div class="check-row"><input type="checkbox" id="s-gateway_enabled"><label for="s-gateway_enabled">启用透明网关（不改 Base URL）</label></div>
	    <div class="check-row"><input type="checkbox" id="s-cache_enabled"><label for="s-cache_enabled">启用响应缓存（非 stream）</label></div>
	    <div class="check-row"><input type="checkbox" id="s-cache_stream_enabled"><label for="s-cache_stream_enabled">启用 stream 缓存（SSE/WS，命中时快速回放）</label></div>
	    <div class="check-row"><input type="checkbox" id="s-cache_shared_across_api_keys"><label for="s-cache_shared_across_api_keys">跨 API Key 共享缓存（仅可信环境）</label></div>
	    <div class="form-grid three" style="margin-top:12px">
	      <div class="form-group"><label>缓存 TTL（秒）</label><input id="s-cache_ttl_seconds" type="number"></div>
	      <div class="form-group"><label>TTL 抖动（秒）</label><input id="s-cache_ttl_jitter_seconds" type="number"></div>
	      <div class="form-group"><label>总内存上限（MB）</label><input id="s-cache_max_total_mb" type="number"></div>
	    </div>
	    <div class="form-grid three" style="margin-top:12px">
	      <div class="form-group"><label>最大条目数</label><input id="s-cache_max_entries" type="number"></div>
	      <div class="form-group"><label>单条最大大小（KB）</label><input id="s-cache_max_body_kb" type="number"></div>
	      <div class="form-group"><label>SWR（秒）</label><input id="s-cache_stale_while_revalidate_seconds" type="number"></div>
	    </div>
	    <div class="form-grid three" style="margin-top:12px">
	      <div class="form-group"><label>SIE（秒）</label><input id="s-cache_stale_if_error_seconds" type="number"></div>
	      <div class="form-group full"><label>Vary Headers（逗号分隔）</label><input id="s-cache_vary_headers" type="text"></div>
	    </div>
    <div class="hint" style="margin-top:10px">
      注意：修改该配置后需要重启代理才会生效；如代理正在运行，可在仪表盘点击「重启代理」立即应用（会短暂中断连接）。<br>
      请求级控制：<code>X-AIProxyHub-Cache: bypass</code> 绕过读写；<code>X-AIProxyHub-Cache: refresh</code> 绕过读取并回源更新；<code>X-AIProxyHub-Cache: no-store</code> 不写入（允许读）。
    </div>
  </div>
  <div class="btn-row">
    <button class="btn btn-primary" onclick="saveSettings()">保存配置</button>
    <button class="btn btn-ghost" onclick="loadSettings()">重置</button>
  </div>
  <div class="card" style="margin-top:18px">
    <div class="card-title" style="cursor:pointer;user-select:none" onclick="toggleExample()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M9 18l6-6-6-6"/></svg>
      配置示例与使用说明
      <span style="margin-left:auto;font-size:11px;font-weight:400;color:var(--dim)">点击展开</span>
    </div>
    <div id="config-example" style="display:none">
      <div style="font-size:13px;line-height:2;color:var(--text)">
        <p style="margin-bottom:12px;color:var(--dim)">以下是推荐的配置填写方式：</p>
        <table style="width:100%;font-size:13px;border-collapse:collapse">
          <thead><tr style="border-bottom:2px solid var(--border)"><th style="text-align:left;padding:8px 10px;color:var(--dim);font-size:12px">字段</th><th style="text-align:left;padding:8px 10px;color:var(--dim);font-size:12px">推荐值</th><th style="text-align:left;padding:8px 10px;color:var(--dim);font-size:12px">说明</th></tr></thead>
          <tbody>
            <tr style="border-bottom:1px solid var(--border)"><td style="padding:8px 10px;font-weight:500">DuckMail Token</td><td style="padding:8px 10px"><code style="background:rgba(99,102,241,.08);padding:2px 6px;border-radius:4px;font-size:12px">dk_xxxxxxxx...</code></td><td style="padding:8px 10px;color:var(--dim)">在 duckmail.sbs 注册后获取，用于批量注册邮箱</td></tr>
            <tr style="border-bottom:1px solid var(--border)"><td style="padding:8px 10px;font-weight:500">代理地址</td><td style="padding:8px 10px"><code style="background:rgba(99,102,241,.08);padding:2px 6px;border-radius:4px;font-size:12px">http://127.0.0.1:7890</code></td><td style="padding:8px 10px;color:var(--dim)">本机 Clash/v2ray 代理地址，需能访问 OpenAI</td></tr>
            <tr style="border-bottom:1px solid var(--border)"><td style="padding:8px 10px;font-weight:500">注册数量</td><td style="padding:8px 10px"><code style="background:rgba(99,102,241,.08);padding:2px 6px;border-radius:4px;font-size:12px">5 ~ 20</code></td><td style="padding:8px 10px;color:var(--dim)">建议首次 5 个测试，稳定后可增加</td></tr>
            <tr style="border-bottom:1px solid var(--border)"><td style="padding:8px 10px;font-weight:500">并发数</td><td style="padding:8px 10px"><code style="background:rgba(99,102,241,.08);padding:2px 6px;border-radius:4px;font-size:12px">3 ~ 5</code></td><td style="padding:8px 10px;color:var(--dim)">过高可能触发风控，建议 3</td></tr>
            <tr style="border-bottom:1px solid var(--border)"><td style="padding:8px 10px;font-weight:500">监听端口</td><td style="padding:8px 10px"><code style="background:rgba(99,102,241,.08);padding:2px 6px;border-radius:4px;font-size:12px">8317</code></td><td style="padding:8px 10px;color:var(--dim)">代理服务端口，客户端连接用</td></tr>
            <tr style="border-bottom:1px solid var(--border)"><td style="padding:8px 10px;font-weight:500">管理密码</td><td style="padding:8px 10px"><code style="background:rgba(99,102,241,.08);padding:2px 6px;border-radius:4px;font-size:12px">自定义强密码</code></td><td style="padding:8px 10px;color:var(--dim)">CPA 管理面板 + 注册上传共用密码</td></tr>
            <tr><td style="padding:8px 10px;font-weight:500">API Key</td><td style="padding:8px 10px"><code style="background:rgba(99,102,241,.08);padding:2px 6px;border-radius:4px;font-size:12px">aiph_xxxxx...</code></td><td style="padding:8px 10px;color:var(--dim)">客户端访问代理的密钥（首次启动会自动生成强随机值，也可自定义）</td></tr>
          </tbody>
        </table>
        <div style="margin-top:16px;padding:14px 18px;background:rgba(99,102,241,.04);border:1px solid rgba(99,102,241,.12);border-radius:8px;font-size:12px;line-height:2">
          <b style="color:var(--accent2)">快速上手流程：</b><br>
          1. 填写上方配置并保存<br>
          2. 回到「仪表盘」点击「一键启动」<br>
          3. 等待自动完成：启动代理 → 清理无效 → 批量注册<br>
          4. 完成后使用以下信息接入 Claude Code / Cursor 等工具：<br>
          <div style="margin:8px 0 0 16px;padding:10px 14px;background:var(--card);border:1px solid var(--border);border-radius:6px;font-family:monospace">
            API Base: <code style="color:var(--accent2)">http://localhost:8317/v1</code><br>
            API Key: <code style="color:var(--accent2)">你设置的 API Key</code>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ====== Register ====== -->
<div class="page" id="pg-register">
  <h1 class="page-title">批量注册</h1>
  <div class="stats">
    <div class="stat"><div class="stat-label">代理服务</div><div class="stat-value" id="r-proxy">--</div></div>
    <div class="stat"><div class="stat-label">注册任务</div><div class="stat-value" id="r-reg">--</div></div>
  </div>
  <div class="card">
    <div class="card-title">操作</div>
    <p style="font-size:13px;color:var(--dim);margin-bottom:14px">点击「开始注册」将使用配置页的参数，自动注册 ChatGPT 账号并将 OAuth Token 上传到代理服务。需要先启动代理。</p>
    <div class="btn-row">
      <button class="btn btn-green" onclick="api('start_proxy')">启动代理</button>
      <button class="btn btn-primary" onclick="api('register')">开始注册</button>
      <button class="btn btn-orange" onclick="doOneClick()">一键全流程</button>
      <button class="btn btn-red" onclick="api('stop_register')">停止注册</button>
      <button class="btn btn-ghost" onclick="api('stop_autopilot')">停止一键流程</button>
    </div>
  </div>
  <div class="card">
    <div class="card-title">实时日志</div>
    <div class="log-box" id="reg-log"></div>
  </div>
</div>

<!-- ====== Accounts ====== -->
<div class="page" id="pg-accounts">
  <h1 class="page-title">账号管理</h1>
  <div class="card">
    <div class="card-title">已注册账号 <span style="margin-left:auto;font-size:12px;color:var(--dim)" id="acc-dir-hint"></span></div>
    <div class="btn-row" style="margin-top:0;margin-bottom:14px">
      <button class="btn btn-orange btn-sm" onclick="doCleanup()">清理无效账号</button>
      <button class="btn btn-ghost btn-sm" onclick="refreshAccounts()">刷新列表</button>
    </div>
    <div id="acc-table"></div>
  </div>
  <div class="card">
    <div class="card-title">输出文件</div>
    <div class="btn-row" style="margin-top:0;margin-bottom:12px">
      <button class="btn btn-ghost btn-sm" onclick="secureCleanupOutputs()">安全清理敏感输出</button>
    </div>
    <div class="form-grid">
      <div class="form-group"><label>registered_accounts.txt</label><textarea id="df-registered_accounts.txt" rows="4" style="width:100%;background:#f8fafc;border:1px solid var(--border);border-radius:8px;color:var(--text);padding:8px;font-size:12px;font-family:monospace;resize:vertical" readonly></textarea></div>
      <div class="form-group"><label>ak.txt</label><textarea id="df-ak.txt" rows="4" style="width:100%;background:#f8fafc;border:1px solid var(--border);border-radius:8px;color:var(--text);padding:8px;font-size:12px;font-family:monospace;resize:vertical" readonly></textarea></div>
    </div>
  </div>
</div>

<!-- ====== Logs ====== -->
<div class="page" id="pg-logs">
  <h1 class="page-title">运行日志</h1>
  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
      <span class="card-title" style="margin:0">全部日志</span>
      <button class="btn btn-ghost btn-sm" onclick="clearLogs()">清空</button>
    </div>
    <div class="log-box" id="all-log" style="border:none;border-radius:0;height:520px"></div>
  </div>
</div>

</div><!-- main -->

<div class="toast" id="toast"></div>

<script>
const API_TOKEN="__API_TOKEN__";
const $=id=>document.getElementById(id);
function esc(s){const d=document.createElement('div');d.textContent=String(s);return d.innerHTML}
const fields=[
  'duckmail_token','proxy','total_accounts','max_workers','management_password','api_key',
  'proxy_port','proxy_host','routing_strategy','request_retry',
  // monitor
  'monitor_interval_seconds','monitor_low_available_threshold_pct','monitor_min_keep_accounts',
  // cache pool
  'cache_vary_headers',
  'cache_ttl_seconds','cache_ttl_jitter_seconds',
  'cache_max_entries','cache_max_body_kb','cache_max_total_mb',
  'cache_stale_while_revalidate_seconds','cache_stale_if_error_seconds'
];
const checks=[
  'quota_switch_project','quota_switch_preview','debug',
  // monitor
  'monitor_enabled','monitor_prune_zero_quota_enabled','monitor_prune_only_usage_limit_reached','monitor_auto_register_enabled','monitor_dry_run',
  // outputs
  'store_passwords','write_ak_rk','keep_token_json','keep_token_json_on_fail',
  // gateway/cache
  'gateway_enabled','cache_enabled','cache_stream_enabled','cache_shared_across_api_keys'
];
const intFields=new Set([
  'total_accounts','max_workers','proxy_port','request_retry',
  // monitor
  'monitor_interval_seconds','monitor_low_available_threshold_pct','monitor_min_keep_accounts',
  'cache_ttl_seconds','cache_ttl_jitter_seconds',
  'cache_max_entries','cache_max_body_kb','cache_max_total_mb',
  'cache_stale_while_revalidate_seconds','cache_stale_if_error_seconds'
]);
const secretFields=new Set(['duckmail_token','management_password','api_key']);
let settingsCache=null;
let usageTick=0;
let lastStatus=null;

/* Navigation */
document.querySelectorAll('.nav a').forEach(a=>{
  a.onclick=()=>{
    document.querySelectorAll('.nav a').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));
    a.classList.add('active');
    $('pg-'+a.dataset.page).classList.add('active');
    if(a.dataset.page==='accounts')refreshAccounts();
  };
});

/* Toast */
function toast(msg,ok=true){
  const t=$('toast');t.textContent=msg;t.className='toast show '+(ok?'ok':'err');
  setTimeout(()=>t.className='toast',2500);
}

/* API */
async function api(path,body){
  try{
    const r=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-AIProxyHub-Token':API_TOKEN},body:body?JSON.stringify(body):undefined});
    const d=await r.json();
    if(d && d.msg!==undefined) toast(d.msg,d.ok);
    setTimeout(refreshStatus,800);
    return d;
  }catch(e){toast('请求失败: '+e.message,false);return{ok:false}}
}

/* Settings */
async function loadSettings(){
  const r=await(await fetch('/api/settings')).json();
  settingsCache=r;
  fields.forEach(f=>{
    const el=$('s-'+f);
    if(!el) return;
    if(secretFields.has(f)){
      el.value='';
      const isSet=!!r[f+'_set'];
      if(isSet) el.placeholder='已保存（留空不变）';
    }else{
      el.value=r[f]??'';
    }
  });
  checks.forEach(f=>{const el=$('s-'+f);if(el)el.checked=!!r[f]});
}
function getSettings(){
  const o={};
  fields.forEach(f=>{const v=$('s-'+f).value;o[f]=intFields.has(f)?parseInt(v)||0:v});
  checks.forEach(f=>{o[f]=$('s-'+f).checked});
  return o;
}
async function saveSettings(){
  const d=await api('save',getSettings());
  if(d.ok){
    if(d.settings) settingsCache=d.settings;
    await loadSettings();
    showApiInfo();
  }
}

async function refreshUsageSummary(){
  try{
    const u=await(await fetch('/api/usage_summary')).json();
    if(u && u.ok){
      $('d-usage-req').textContent=String(u.total_requests??0);
      $('d-usage-tokens').textContent=String(u.total_tokens??0);
      $('d-usage-req').title='';
      $('d-usage-tokens').title='';
    }else{
      $('d-usage-req').textContent='--';
      $('d-usage-tokens').textContent='--';
      const hint=(u && u.msg)?String(u.msg):'';
      $('d-usage-req').title=hint;
      $('d-usage-tokens').title=hint;
    }
  }catch(e){
    $('d-usage-req').textContent='--';
    $('d-usage-tokens').textContent='--';
    const hint='无法读取使用统计: '+(e && e.message?e.message:String(e));
    $('d-usage-req').title=hint;
    $('d-usage-tokens').title=hint;
  }
}

/* Status */
async function refreshStatus(){
  try{
    const r=await(await fetch('/api/status')).json();
    lastStatus=r;
    const on=v=>v?'运行中':'已停止';
    const cls=v=>v?'on':'off';
    const proxyText=r.proxy?(r.proxy_external?'运行中（外部）':'运行中'):'已停止';
    const proxyTitle=r.proxy_external?'检测到代理端口已有外部服务在运行（本实例无法停止/重启该代理）。':''; 
    $('d-proxy').textContent=proxyText;$('d-proxy').className='stat-value '+cls(r.proxy);$('d-proxy').title=proxyTitle;
    $('d-reg').textContent=r.register?'进行中':'空闲';$('d-reg').className='stat-value '+(r.register?'on':'off');
    $('d-count').textContent=r.accounts;
    $('d-port').textContent=r.port;
    usageTick++;
    if(usageTick % 3 === 1) refreshUsageSummary(); // 降低请求频率，避免频繁拉取 CPA usage 明细
    // 缓存池（网关）统计
    try{
      const g=r.gateway||{};
      const c=(g.cache||{});
      if(g.running){
        $('d-cache-hit').textContent=(c.enabled?String(c.hits??0):'关闭');
        $('d-cache-ratio').textContent=(c.enabled?String((c.hit_ratio_pct??0))+'%':'关闭');
      }else{
        $('d-cache-hit').textContent='--';
        $('d-cache-ratio').textContent='--';
      }
    }catch(e){}
    $('r-proxy').textContent=proxyText;$('r-proxy').className='stat-value '+cls(r.proxy);$('r-proxy').title=proxyTitle;
    $('r-reg').textContent=r.register?'进行中':'空闲';$('r-reg').className='stat-value '+(r.register?'on':'off');
    if(r.proxy)showApiInfo();
    updateAutopilot(r.autopilot);
  }catch(e){}
}

/* Logs */
async function refreshLogs(){
  try{
    const r=await(await fetch('/api/logs')).json();
    const render=(box,lines)=>{
      box.innerHTML=lines.map(l=>{
        let c='';
        if(l.includes('[SYS]'))c='sys';
        else if(l.includes('[PROXY]'))c='proxy';
        else if(l.includes('[REG]'))c='reg';
        if(/error|fail|❌/i.test(l))c='err';
        if(/success|成功|✅|OK\]/i.test(l))c='ok';
        const safe=l.replace(/</g,'&lt;');
        return '<div class="'+c+'">'+safe+'</div>';
      }).join('');
      box.scrollTop=box.scrollHeight;
    };
    render($('all-log'),r.lines);
    render($('reg-log'),r.lines.filter(l=>l.includes('[REG]')||l.includes('[SYS]')));
  }catch(e){}
}
function clearLogs(){api('clear_logs');$('all-log').innerHTML='';$('reg-log').innerHTML=''}

/* Accounts */
async function refreshAccounts(){
  try{
    const r=await(await fetch('/api/accounts')).json();
    $('acc-dir-hint').textContent='目录: ~/.cli-proxy-api/ ('+r.accounts.length+' 个)';
    if(!r.accounts.length){
      $('acc-table').innerHTML='<div class="empty">暂无账号，请先注册</div>';
    }else{
      let h='<table class="tbl"><thead><tr><th>邮箱</th><th>类型</th><th>时间</th><th>操作</th></tr></thead><tbody>';
      r.accounts.forEach(a=>{
        const t=a.mtime?new Date(a.mtime*1000).toLocaleString('zh-CN'):'';
        h+=`<tr><td>${esc(a.email)}</td><td><span class="badge badge-blue">${esc(a.type||'-')}</span></td><td style="color:var(--dim);font-size:12px">${esc(t)}</td><td><button class="btn btn-red btn-sm" onclick="delAcc('${esc(a.file)}')">删除</button></td></tr>`;
      });
      h+='</tbody></table>';
      $('acc-table').innerHTML=h;
    }
    const df=await(await fetch('/api/data_files')).json();
    for(const[k,v]of Object.entries(df)){const el=$('df-'+k);if(el)el.value=v}
  }catch(e){}
}
async function delAcc(f){if(confirm('确认删除 '+f+' ?')){await api('delete_account',{file:f});refreshAccounts()}}
async function secureCleanupOutputs(){
  if(confirm('将删除 data/ak.txt、data/rk.txt，并对 registered_accounts.txt 进行脱敏覆盖，同时清理 codex_tokens/*.json。继续？')){
    await api('secure_cleanup_outputs');
    refreshAccounts();
  }
}

/* Helpers */
function showApiInfo(){
  const port=$('s-proxy_port')?.value||8317;
  const key=$('s-api_key')?.value||'';
  const mgmt=$('s-management_password')?.value||'';
  $('i-url').textContent='http://localhost:'+port+'/v1';
  const keySet=!!(settingsCache && settingsCache['api_key_set']);
  $('i-key').textContent=key || (keySet?'已设置（点击复制）':'');
  $('btn-copy-key').style.display=(key || keySet)?'inline-flex':'none';
  const mgmtSet=!!(settingsCache && settingsCache['management_password_set']);
  $('i-mgmt').textContent=mgmt || (mgmtSet?'已设置（点击复制）':'');
  $('btn-copy-mgmt').style.display=(mgmt || mgmtSet)?'inline-flex':'none';
  $('i-panel').href='http://localhost:'+port+'/management.html';
  $('api-info').style.display='block';
}
function openPanel(){
  if(lastStatus && !lastStatus.proxy){toast('代理未运行：请先点击「启动代理」',false);return;}
  const port=$('s-proxy_port')?.value||8317;
  window.open('http://localhost:'+port+'/management.html','_blank');
}
function openUsage(){
  if(lastStatus && !lastStatus.proxy){toast('代理未运行：请先点击「启动代理」',false);return;}
  const port=$('s-proxy_port')?.value||8317;
  window.open('http://localhost:'+port+'/management.html#/usage','_blank');
}
async function doAutoPilot(){
  $('ap-btn').disabled=true;$('ap-btn').textContent='运行中...';
  await api('autopilot');
}
async function doOneClick(){return doAutoPilot();}
async function doCleanup(){await api('cleanup');refreshAccounts()}

async function copyToClipboard(text){
  try{
    await navigator.clipboard.writeText(text);
  }catch(e){
    const ta=document.createElement('textarea');
    ta.value=text;document.body.appendChild(ta);
    ta.select();document.execCommand('copy');
    document.body.removeChild(ta);
  }
}

async function copyApiKey(){
  let key=$('s-api_key')?.value||'';
  if(!key){
    const d=await api('reveal_secret',{name:'api_key'});
    if(d && d.ok) key=d.value||'';
  }
  if(!key){toast('API Key 未设置',false);return;}
  await copyToClipboard(key);
  toast('API Key 已复制',true);
}

async function copyManagementPassword(){
  let v=$('s-management_password')?.value||'';
  if(!v){
    const d=await api('reveal_secret',{name:'management_password'});
    if(d && d.ok) v=d.value||'';
  }
  if(!v){toast('管理密码未设置',false);return;}
  await copyToClipboard(v);
  toast('管理密码已复制',true);
}

/* Autopilot UI */
const apMap={idle:0,proxy:1,cleanup:2,register:3,done:4,error:-1};
function updateAutopilot(ap){
  if(!ap)return;
  const phase=ap.phase||'idle';
  const idx=apMap[phase]??0;
  ['ap-s1','ap-s2','ap-s3','ap-s4'].forEach((id,i)=>{
    const el=$(id);el.className='ap-step';
    if(phase==='error'){}
    else if(i+1<idx)el.classList.add('done');
    else if(i+1===idx)el.classList.add('active');
  });
  if(phase==='done'){['ap-s1','ap-s2','ap-s3','ap-s4'].forEach(id=>$(id).className='ap-step done')}
  if(phase==='error'){$('ap-s1').className='ap-step err'}
  $('ap-msg').textContent=ap.msg||'';
  $('ap-title').textContent=phase==='done'?'全部就绪':phase==='error'?'流程异常':'一键全流程';
  if(phase==='done'||phase==='error'||phase==='idle'){$('ap-btn').disabled=false;$('ap-btn').textContent='一键启动'}
  if(phase==='done')showApiInfo();
}

function toggleExample(){
  const el=$('config-example');
  el.style.display=el.style.display==='none'?'block':'none';
}

/* Init */
loadSettings();
refreshStatus();
refreshLogs();
let statusTimer=setInterval(refreshStatus,4000);
let logTimer=setInterval(refreshLogs,3000);
let accTimer=setInterval(()=>{const pg=$('pg-accounts');if(pg.classList.contains('active'))refreshAccounts()},8000);
function stopPolling(){clearInterval(statusTimer);clearInterval(logTimer);clearInterval(accTimer)}
function startPolling(){statusTimer=setInterval(refreshStatus,4000);logTimer=setInterval(refreshLogs,3000);accTimer=setInterval(()=>{const pg=$('pg-accounts');if(pg.classList.contains('active'))refreshAccounts()},8000)}
document.addEventListener('visibilitychange',()=>{document.hidden?stopPolling():startPolling()});
</script>
</body>
</html>'''


def _query_quota():
    """通过 CPA 管理 API 查询所有账号的配额状态。"""
    s = load_settings()
    port = int(s.get("proxy_port", 8317) or 8317)
    mgmt_pw = s.get("management_password", "")
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v0/management/auth-files",
            headers={"Authorization": f"Bearer {mgmt_pw}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return {"ok": False, "msg": f"无法连接 CPA 管理 API: {e}"}

    files = data.get("files", data) if isinstance(data, dict) else data
    if not isinstance(files, list):
        return {"ok": False, "msg": "CPA 返回格式异常"}

    now = time.time()
    results = []
    active = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        unavailable = item.get("unavailable", False)
        if not unavailable:
            active += 1
        reset_info = {}
        status_message = str(item.get("status_message", "") or "")
        err_type = ""
        err_code = ""
        try:
            sm = json.loads(status_message or "{}") if status_message else {}
            err = (sm.get("error") or {}) if isinstance(sm, dict) else {}
            if isinstance(err, dict):
                err_type = str(err.get("type", "") or err.get("name", "") or "")
                err_code = str(err.get("code", "") or "")
            if err.get("resets_at"):
                reset_info = {"resets_at": err["resets_at"], "hours_left": round((err["resets_at"] - now) / 3600, 1)}
        except Exception:
            pass
        results.append({
            "email": item.get("email", ""),
            # 优先使用 CPA 提供的文件名（更稳健）；缺省时由调用方回退到 email.json
            "file": item.get("file", "") or item.get("filename", "") or "",
            "available": not unavailable,
            "status": item.get("status", ""),
            # 返回错误类型/码便于“额度为零”判定（不包含任何密钥明文）
            "error_type": err_type,
            "error_code": err_code,
            # 兜底：保留原始 status_message（可能为空/非 JSON），用于宽松匹配
            "status_message": status_message,
            **reset_info,
        })

    total = len(results)
    pct = round(active / total * 100) if total else 0
    return {"ok": True, "total": total, "active": active, "pct": pct, "below_threshold": pct < 25, "accounts": results}


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
    port = int(s.get("proxy_port", 8317) or 8317)
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


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

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

    def _html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_GET(self):
        if self.path == "/":
            self._html(
                HTML_PAGE.replace("__API_TOKEN__", _UI_API_TOKEN).replace("__APP_VERSION__", APP_VERSION)
            )
        elif self.path == "/api/settings":
            self._json(get_settings_public())
        elif self.path == "/api/status":
            s = load_settings()
            gw_enabled = bool(s.get("gateway_enabled", False)) or bool(s.get("cache_enabled", False))
            port = int(s.get("proxy_port", 8317) or 8317)
            proxy_reachable = _proxy_reachable(port)
            proxy_alive = proxy_process is not None and proxy_process.poll() is None
            gw_alive = gateway_server is not None
            proxy_managed = bool(proxy_alive or gw_alive)
            proxy_external = bool(proxy_reachable and not proxy_managed)
            # 以“端口可用性”为准：避免出现“代理其实已在运行，但因为不是本实例启动而显示已停止”的误判
            proxy_ok = bool(proxy_reachable)
            self._json({
                "proxy": proxy_ok,
                "proxy_managed": proxy_managed,
                "proxy_external": proxy_external,
                "register": register_process is not None and register_process.poll() is None,
                "accounts": len(get_accounts()),
                "port": port,
                "paths": {
                    "root": str(ROOT),
                    "settings_file": str(SETTINGS_FILE),
                    "data_dir": str(DATA_DIR),
                    "runtime_dir": str(RUNTIME_DIR),
                },
                "autopilot": autopilot_state,
                "monitor": get_monitor_status(),
                "gateway": {
                    "enabled": gw_enabled,
                    "running": gw_alive,
                    "listen_host": gateway_listen_host,
                    "listen_port": gateway_listen_port,
                    "upstream_port": gateway_upstream_port,
                    "cache": _cache_stats(),
                },
            })
        elif self.path == "/api/usage_summary":
            self._json(_query_usage_summary())
        elif self.path == "/api/logs":
            with _log_lock:
                lines_copy = list(log_lines)[-200:]
            self._json({"lines": lines_copy})
        elif self.path == "/api/accounts":
            self._json({"accounts": get_accounts()})
        elif self.path == "/api/data_files":
            self._json(get_data_files())
        # ===== 外部 API（Bearer api_key 认证，无需页面 CSRF token） =====
        elif self.path.startswith("/api/ext/"):
            if not self._auth_ext():
                self._json({"ok": False, "msg": "Unauthorized"}, 401)
                return
            ep = self.path[9:]  # strip "/api/ext/"
            if ep == "status":
                s = load_settings()
                gw_enabled = bool(s.get("gateway_enabled", False)) or bool(s.get("cache_enabled", False))
                port = int(s.get("proxy_port", 8317) or 8317)
                proxy_reachable = _proxy_reachable(port)
                proxy_alive = proxy_process is not None and proxy_process.poll() is None
                gw_alive = gateway_server is not None
                proxy_managed = bool(proxy_alive or gw_alive)
                proxy_external = bool(proxy_reachable and not proxy_managed)
                proxy_ok = bool(proxy_reachable)
                self._json({
                    "ok": True,
                    "proxy": proxy_ok,
                    "proxy_managed": proxy_managed,
                    "proxy_external": proxy_external,
                    "register": register_process is not None and register_process.poll() is None,
                    "accounts": len(get_accounts()),
                    "port": port,
                    "paths": {
                        "root": str(ROOT),
                        "settings_file": str(SETTINGS_FILE),
                        "data_dir": str(DATA_DIR),
                        "runtime_dir": str(RUNTIME_DIR),
                    },
                    "autopilot": autopilot_state,
                    "monitor": get_monitor_status(),
                    "gateway": {
                        "enabled": gw_enabled,
                        "running": gw_alive,
                        "listen_host": gateway_listen_host,
                        "listen_port": gateway_listen_port,
                        "upstream_port": gateway_upstream_port,
                        "cache": _cache_stats(),
                    },
                })
            elif ep == "quota":
                self._json(_query_quota())
            elif ep == "usage_summary":
                self._json(_query_usage_summary())
            elif ep == "cache_stats":
                self._json({"ok": True, "cache": _cache_stats(), "gateway_running": gateway_server is not None})
            elif ep == "logs":
                with _log_lock:
                    lines_copy = list(log_lines)[-200:]
                self._json({"ok": True, "lines": lines_copy})
            elif ep == "accounts":
                self._json({"ok": True, "accounts": get_accounts()})
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
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                ep = p[9:]
                if ep == "register":
                    self._json(run_register())
                elif ep == "stop_register":
                    self._json(stop_register())
                elif ep == "start_proxy":
                    self._json(start_proxy())
                elif ep == "stop_proxy":
                    self._json(stop_proxy())
                elif ep == "restart_proxy":
                    self._json(restart_proxy())
                elif ep == "autopilot":
                    self._json(start_auto_pilot())
                elif ep == "stop_autopilot":
                    self._json(stop_auto_pilot())
                elif ep == "cleanup":
                    self._json(cleanup_accounts())
                elif ep == "start_monitor":
                    self._json(start_monitor())
                elif ep == "stop_monitor":
                    self._json(stop_monitor())
                elif ep == "monitor_status":
                    self._json(get_monitor_status())
                elif ep == "cache_clear":
                    _cache_clear()
                    self._json({"ok": True, "msg": "缓存已清空", "cache": _cache_stats()})
                else:
                    self._json({"ok": False, "msg": "Unknown endpoint"}, 404)
                return
            # CSRF/误触保护：所有写操作必须带一次性 token（页面内置，第三方站点无法随意构造）
            if self.headers.get("X-AIProxyHub-Token", "") != _UI_API_TOKEN:
                self._json({"ok": False, "msg": "Forbidden"}, 403)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
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
            elif p == "/api/register":
                self._json(run_register())
            elif p == "/api/stop_register":
                self._json(stop_register())
            elif p == "/api/delete_account":
                self._json(delete_account(body.get("file", "")))
            elif p == "/api/clear_logs":
                with _log_lock:
                    log_lines.clear()
                self._json({"ok": True, "msg": "日志已清空"})
            elif p == "/api/cleanup":
                self._json(cleanup_accounts())
            elif p == "/api/autopilot":
                self._json(start_auto_pilot())
            elif p == "/api/stop_autopilot":
                self._json(stop_auto_pilot())
            elif p == "/api/reveal_secret":
                self._json(reveal_secret(body.get("name", "")))
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            s.bind((host, 0))
            return s.getsockname()[1]


def _normalize_launcher_host(host: str) -> str:
    h = str(host or "").strip()
    if not h:
        return LAUNCHER_HOST
    if h.lower() == "localhost":
        return "127.0.0.1"
    return h


def _run_register_worker(args) -> int:
    """
    在“register-worker”模式下执行批量注册任务。

    设计目标：支持 PyInstaller 单文件 EXE 通过“二次启动自身进程”来运行注册，
    以保留 launcher 原有的“可终止子进程”语义（stop_register）。

    依赖：AIPROXYHUB_REGISTER_CONFIG 指向运行时 register.runtime.json。
    """
    cfg = str(getattr(args, "register_config", "") or os.getenv("AIPROXYHUB_REGISTER_CONFIG", "") or "").strip()
    if cfg:
        os.environ["AIPROXYHUB_REGISTER_CONFIG"] = cfg

    if not str(os.getenv("AIPROXYHUB_REGISTER_CONFIG", "") or "").strip():
        print("❌ 缺少 AIPROXYHUB_REGISTER_CONFIG（注册运行时配置路径），无法执行注册任务。")
        return 2

    try:
        from register.chatgpt_register import run_batch  # 延迟导入，避免影响 launcher 常规启动速度
    except Exception as e:
        print(f"❌ 导入注册模块失败：{e}")
        return 2

    total = int(getattr(args, "total_accounts", 0) or 0)
    workers = int(getattr(args, "max_workers", 1) or 1)
    proxy = getattr(args, "proxy", None)
    output_file = str(getattr(args, "output_file", "") or os.path.join(DATA_DIR, "registered_accounts.txt"))
    try:
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
    except Exception:
        pass

    run_batch(total_accounts=total, output_file=output_file, max_workers=workers, proxy=proxy)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="AIProxyHub", add_help=True)
    # register-worker：用于让 launcher 自身在子进程里执行注册（兼容 PyInstaller）
    parser.add_argument("--register-worker", action="store_true", help="内部模式：执行批量注册并退出")
    parser.add_argument("--register-config", default="", help="可选：覆盖 AIPROXYHUB_REGISTER_CONFIG（运行时注册配置路径）")
    parser.add_argument("--total-accounts", type=int, default=0, help="register-worker：注册数量")
    parser.add_argument("--max-workers", type=int, default=0, help="register-worker：并发数")
    parser.add_argument("--proxy", default="", help="register-worker：代理地址（http(s)/socks5）")
    parser.add_argument("--output-file", default="", help="register-worker：输出文件路径")

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

    # 子进程注册模式：直接执行注册并退出（不启动 HTTP server、不做 allow-remote 校验）
    if getattr(args, "register_worker", False):
        raise SystemExit(_run_register_worker(args))

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

    for lf_name, lf_path in [("config.yaml", PROXY_CONFIG), ("register/config.json", REGISTER_CONFIG)]:
        if os.path.exists(lf_path):
            bak = lf_path + ".bak"
            try:
                os.replace(lf_path, bak)
                log(f"[SYS] 已将历史明文配置 {lf_name} 重命名为 .bak（当前版本使用临时目录）")
            except Exception as e:
                log(f"[WARN] 无法迁移 {lf_name}: {e}；建议手动删除以降低泄露风险")

    # 兜底：清理上次异常退出可能残留的运行时临时配置（避免敏感配置长期留在临时目录）
    try:
        proxy_port = int((s or {}).get("proxy_port", 8317) or 8317)
        if not _proxy_reachable(proxy_port):
            _safe_unlink(RUNTIME_PROXY_CONFIG)
        _safe_unlink(RUNTIME_REGISTER_CONFIG)
        _safe_unlink(os.path.join(RUNTIME_DIR, "_test_register_cfg.json"))
    except Exception:
        pass

    port = int(args.port or LAUNCHER_PORT)
    if not args.strict_port:
        port = find_free_port(port, host=host)

    # 提升并发下的连接排队能力（backlog）。对本地面板/External API 也更稳健。
    # 同上：request_queue_size 需要在 listen() 之前生效，因此用子类覆写类属性。
    class _LauncherServer(http.server.ThreadingHTTPServer):
        request_queue_size = 128

    server = _LauncherServer((host, port), Handler)
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
            stop_auto_pilot()
        except Exception:
            pass
        try:
            stop_register()
        except Exception:
            pass
        try:
            stop_proxy()
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
