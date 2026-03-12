#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AIProxyHub 发布包（Windows EXE）冒烟测试脚本

目标：
1) 从 release zip 解压 EXE
2) 以隔离的 AIPROXYHUB_HOME 启动 EXE（避免污染本机真实配置）
3) 验证 External API / Proxy / CPA Usage 的关键链路可用

注意：
- 本脚本默认仅使用 Python 标准库（无需额外依赖）
- 不会打印 API Key / 管理密钥明文
- 若本机没有任何可用 auth 文件（~/.cli-proxy-api），/v1/responses 可能会失败，这属于“未注册账号”的预期情形
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


# ----------------------------
# DPAPI helper（仅用于读取 settings.json 中 dpapi:... 的管理密钥 / api_key）
# ----------------------------

def _is_windows() -> bool:
    return os.name == "nt"


def _dpapi_decrypt_bytes(data: bytes) -> bytes:
    if not _is_windows():
        raise RuntimeError("DPAPI 仅支持 Windows")

    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

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

    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def maybe_dpapi_decrypt(value: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    if not value.startswith("dpapi:"):
        return value
    if not _is_windows():
        return ""
    b64 = value[len("dpapi:") :]
    try:
        raw = base64.b64decode(b64)
        dec = _dpapi_decrypt_bytes(raw)
        return dec.decode("utf-8", errors="replace")
    except Exception:
        return ""


# ----------------------------
# HTTP helpers
# ----------------------------


@dataclass
class HttpResp:
    status: int
    body: bytes
    headers: dict

    def json(self):
        return json.loads(self.body.decode("utf-8", errors="replace") or "{}")


def http_request(url: str, *, method: str = "GET", headers: dict | None = None, json_body: dict | None = None, timeout=10) -> HttpResp:
    data = None
    req_headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return HttpResp(status=getattr(r, "status", 200), body=body, headers=dict(r.headers.items()))
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return HttpResp(status=int(getattr(e, "code", 0) or 0), body=body, headers=dict(getattr(e, "headers", {}) or {}))
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        # 连接被拒绝/未监听/读取超时等网络层错误：返回 status=0 交由上层重试
        return HttpResp(status=0, body=b"", headers={})


def wait_for_http_ok(url: str, *, headers: dict | None = None, timeout_s: int = 30, interval_s: float = 0.5) -> HttpResp:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = http_request(url, headers=headers, timeout=5)
        if 200 <= last.status < 300:
            return last
        time.sleep(interval_s)
    raise TimeoutError(f"等待服务就绪超时: {url} (last_status={getattr(last,'status',None)})")


def find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def body_contains_unknown_provider(resp: HttpResp | None) -> bool:
    if resp is None:
        return False
    try:
        t = (resp.body or b"").decode("utf-8", errors="replace").lower()
        return "unknown provider" in t
    except Exception:
        return False


# ----------------------------
# Load test (std lib only)
# ----------------------------


def _percentile_ms(values_s: list[float], pct: float) -> float:
    if not values_s:
        return 0.0
    v = sorted(float(x) for x in values_s if x is not None)
    if not v:
        return 0.0
    pct = max(0.0, min(1.0, float(pct)))
    idx = int(round((len(v) - 1) * pct))
    idx = max(0, min(len(v) - 1, idx))
    return round(v[idx] * 1000.0, 1)


def run_rps_load_test(
    *,
    url: str,
    headers: dict,
    json_body: dict,
    rps: int,
    duration_s: int,
    timeout: int = 30,
) -> dict:
    """
    简易并发压测（尽量模拟固定 RPS 发送）。

    设计目标：
    - 仅验证“网关/EXE 是否能稳定扛住一定的请求速率”
    - 默认建议在启用缓存池后压测（避免真实消耗账号额度）
    """
    rps = int(rps or 0)
    duration_s = int(duration_s or 0)
    if rps <= 0 or duration_s <= 0:
        return {"ok": False, "msg": "rps/duration_s 参数不合法"}

    total = int(rps * duration_s)
    max_workers = max(8, min(256, rps * 4))
    lat_s: list[float] = []
    ok_count = 0
    err_count = 0
    cache_hit = 0
    cache_miss = 0
    start = time.perf_counter()

    def _one() -> tuple[int, float, str]:
        t0 = time.perf_counter()
        r = http_request(url, method="POST", headers=headers, json_body=json_body, timeout=timeout)
        dt = time.perf_counter() - t0
        h = {str(k).lower(): str(v) for k, v in (r.headers or {}).items()}
        cache_tag = str(h.get("x-aiproxyhub-cache", "") or "")
        return int(r.status or 0), dt, cache_tag

    futures = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i in range(total):
            target = start + (i / float(rps))
            now = time.perf_counter()
            if target > now:
                time.sleep(target - now)
            futures.append(ex.submit(_one))

        for fu in as_completed(futures):
            try:
                status, dt, cache_tag = fu.result()
                lat_s.append(float(dt))
                if status == 200:
                    ok_count += 1
                else:
                    err_count += 1
                if str(cache_tag).upper() == "HIT":
                    cache_hit += 1
                elif cache_tag:
                    cache_miss += 1
            except Exception:
                err_count += 1

    elapsed = max(0.001, time.perf_counter() - start)
    achieved_rps = round(ok_count / elapsed, 2)

    return {
        "ok": True,
        "target_rps": int(rps),
        "duration_s": int(duration_s),
        "total_requests": int(total),
        "ok_count": int(ok_count),
        "err_count": int(err_count),
        "achieved_rps": achieved_rps,
        "p50_ms": _percentile_ms(lat_s, 0.50),
        "p95_ms": _percentile_ms(lat_s, 0.95),
        "max_ms": _percentile_ms(lat_s, 1.0),
        "cache_hit": int(cache_hit),
        "cache_other": int(cache_miss),
        "max_workers": int(max_workers),
    }


# ----------------------------
# Smoke flow
# ----------------------------


def pick_default_zip(project_root: Path) -> Path:
    release_dir = project_root / "release"
    zips = sorted(release_dir.glob("AIProxyHub-*-win64.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not zips:
        raise FileNotFoundError(f"未找到发布包: {release_dir}\\AIProxyHub-*-win64.zip")
    return zips[0]


def extract_zip(zip_path: Path, dst_dir: Path) -> list[Path]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    out = []
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dst_dir)
        for n in z.namelist():
            if n.endswith("/"):
                continue
            out.append(dst_dir / n)
    return out


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def mask_secret(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:2] + ("*" * (len(s) - 6)) + s[-4:]


def main() -> int:
    parser = argparse.ArgumentParser(description="AIProxyHub 发布包 EXE 冒烟测试（Windows）")
    parser.add_argument("--zip", default="", help="release zip 路径（默认取 release/ 下最新的 AIProxyHub-*-win64.zip）")
    parser.add_argument("--launcher-port", type=int, default=0, help="launcher 端口（默认自动找空闲端口）")
    parser.add_argument("--proxy-port", type=int, default=0, help="proxy 端口（默认自动找空闲端口）")
    # onefile EXE 首次启动需要解包到临时目录，某些机器/杀软环境下可能超过 60s
    parser.add_argument("--timeout", type=int, default=180, help="整体等待超时（秒）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（默认会保留，方便排查；该开关仅影响输出提示）")
    parser.add_argument("--require-responses", action="store_true", help="强制要求 /v1/responses 成功（无可用账号时会失败）")
    parser.add_argument("--test-cache", action="store_true", help="启用透明网关+缓存池并验证缓存命中（需要 /v1/responses 成功）")
    parser.add_argument("--share-cache-across-api-keys", action="store_true", help="在缓存测试时开启“跨 API Key 共享缓存”（仅可信环境）")
    parser.add_argument("--load-rps", type=int, default=0, help="并发压测目标 RPS（建议搭配 --test-cache；0 表示不测试）")
    parser.add_argument("--load-seconds", type=int, default=5, help="并发压测持续秒数（默认 5）")
    parser.add_argument("--test-register-worker", action="store_true", help="验证 EXE 内置 register-worker 模式可执行（不进行真实注册）")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    zip_path = Path(args.zip).resolve() if args.zip else pick_default_zip(project_root)
    if not zip_path.exists():
        raise FileNotFoundError(str(zip_path))

    work_dir = Path(tempfile.mkdtemp(prefix="aiproxyhub_smoke_"))
    extract_dir = work_dir / "extract"
    home_dir = work_dir / "home"
    home_dir.mkdir(parents=True, exist_ok=True)

    extracted = extract_zip(zip_path, extract_dir)
    exe_path = extract_dir / "AIProxyHub.exe"
    if not exe_path.exists():
        # onedir 情况：zip 里可能是 dist/AIProxyHub/AIProxyHub.exe
        candidates = [p for p in extracted if p.name.lower() == "aiproxyhub.exe"]
        if candidates:
            exe_path = candidates[0]
        else:
            raise FileNotFoundError("zip 内未找到 AIProxyHub.exe")

    launcher_port = int(args.launcher_port or 0) or find_free_port()
    proxy_port = int(args.proxy_port or 0) or find_free_port()

    # 只写入非敏感字段；敏感字段由首次启动自动生成并 DPAPI 加密落盘
    write_json(
        home_dir / "settings.json",
        {
            "proxy_port": proxy_port,
            "proxy_host": "127.0.0.1",
            # 代理地址：保持默认值可能更符合真实使用场景；此处不强行覆盖
        },
    )

    env = dict(os.environ)
    env["AIPROXYHUB_HOME"] = str(home_dir)
    # 避免自动打开浏览器（CI/脚本场景）
    env["AIPROXYHUB_NO_BROWSER"] = "1"

    # 启动 EXE
    cmd = [str(exe_path), "--host", "127.0.0.1", "--port", str(launcher_port), "--strict-port", "--no-browser"]
    proc = subprocess.Popen(cmd, cwd=str(exe_path.parent), env=env)

    launcher_base = f"http://127.0.0.1:{launcher_port}"
    proxy_base = f"http://127.0.0.1:{proxy_port}"

    try:
        # 1) launcher ready
        wait_for_http_ok(f"{launcher_base}/api/status", timeout_s=int(args.timeout))

        # 2) settings 已写入（dpapi 加密）
        settings = read_json(home_dir / "settings.json")
        api_key = maybe_dpapi_decrypt(settings.get("api_key", ""))
        mgmt_key = maybe_dpapi_decrypt(settings.get("management_password", ""))
        if not api_key or not mgmt_key:
            raise RuntimeError("未能从 settings.json 解密 api_key/management_password（可能未写入或 DPAPI 失败）")

        # 可选：启用缓存池（跨账号节省额度）
        # - 仅改写非敏感字段（不触碰 dpapi:...）
        # - 让 start_proxy 直接以“透明网关 + 缓存”模式启动
        if args.test_cache:
            settings["gateway_enabled"] = True
            settings["cache_enabled"] = True
            settings["cache_shared_across_api_keys"] = bool(getattr(args, "share_cache_across_api_keys", False))
            settings["cache_ttl_seconds"] = int(settings.get("cache_ttl_seconds") or 3600)
            settings["cache_max_entries"] = int(settings.get("cache_max_entries") or 200)
            settings["cache_max_body_kb"] = int(settings.get("cache_max_body_kb") or 512)
            write_json(home_dir / "settings.json", settings)

        # 3) ext API auth guard（无 header 应返回 401）
        r0 = http_request(f"{launcher_base}/api/ext/status", timeout=10)
        if r0.status != 401:
            raise RuntimeError(f"ext API 未按预期拒绝未鉴权请求（期望 401，实际 {r0.status}）")

        auth = {"Authorization": f"Bearer {api_key}"}

        # 4) ext status OK
        r1 = http_request(f"{launcher_base}/api/ext/status", headers=auth, timeout=10)
        if r1.status != 200:
            raise RuntimeError(f"/api/ext/status 失败: {r1.status} {r1.body[:200]!r}")

        # 5) start proxy
        r2 = http_request(f"{launcher_base}/api/ext/start_proxy", method="POST", headers=auth, timeout=30)
        if r2.status != 200:
            raise RuntimeError(f"/api/ext/start_proxy 失败: {r2.status} {r2.body[:200]!r}")

        # 5.5) register 预检：隔离 settings 默认不包含 DuckMail token，应当 fail-safe 返回 ok=false（不启动真实注册）
        rr = http_request(f"{launcher_base}/api/ext/register", method="POST", headers=auth, timeout=20)
        if rr.status != 200:
            raise RuntimeError(f"/api/ext/register 失败: {rr.status} {rr.body[:200]!r}")
        rrj = rr.json()
        if bool(rrj.get("ok", False)):
            raise RuntimeError(f"/api/ext/register 在未配置 DuckMail Token 时不应 ok=true: {rr.body[:300]!r}")
        if "DuckMail" not in str(rrj.get("msg", "") or ""):
            raise RuntimeError(f"/api/ext/register 返回 msg 不包含 DuckMail（预期为缺少 token 提示）: {rr.body[:300]!r}")

        # 5.6) register-worker 兼容性（PyInstaller frozen 下不能依赖 python -c）
        # 这里不做真实注册，只验证：
        # - AIProxyHub.exe 能以 --register-worker 启动
        # - 能成功导入 register.chatgpt_register（exit code=0）
        register_worker_ok = False
        if bool(getattr(args, "test_register_worker", False)):
            reg_cfg = home_dir / "register.runtime.json"
            write_json(reg_cfg, {"duckmail_bearer": ""})
            cmd2 = [
                str(exe_path),
                "--register-worker",
                "--register-config",
                str(reg_cfg),
                "--total-accounts",
                "0",
                "--max-workers",
                "1",
            ]
            rproc = subprocess.run(cmd2, cwd=str(exe_path.parent), env=env, timeout=60)
            if int(getattr(rproc, "returncode", 0) or 0) != 0:
                raise RuntimeError(f"register-worker 失败：exit={getattr(rproc,'returncode',None)} cmd={cmd2}")
            register_worker_ok = True

        # 6) proxy ready + models 列表（用于选择一个更稳的推理模型，避免某些 alias 偶发不可用）
        rm = wait_for_http_ok(f"{proxy_base}/v1/models", headers={"Authorization": f"Bearer {api_key}"}, timeout_s=int(args.timeout))
        mj = rm.json()
        ids = []
        try:
            for it in (mj.get("data") or []):
                if isinstance(it, dict) and it.get("id"):
                    ids.append(str(it["id"]))
        except Exception:
            ids = []
        models_count = int(len(ids))
        # 避免输出过大：仅给出一个样本列表，便于快速确认“模型目录可用”
        models_sample = list(ids[:20])
        prefer = [
            "gpt-5.2-codex",
            "gpt-5.1-codex",
            "gpt-5-codex",
            "gpt-5-codex-mini",
            "gpt-5.2",
            "gpt-5.1",
            "gpt-5",
        ]
        infer_model = next((m for m in prefer if m in ids), (prefer[0] if prefer else "gpt-5-codex-mini"))

        # 6.5) usage summary（通过 AIProxyHub 聚合查询 CPA usage，避免 UI 不显示时无法排障）
        ru = http_request(f"{launcher_base}/api/ext/usage_summary", headers=auth, timeout=20)
        if ru.status != 200:
            raise RuntimeError(f"/api/ext/usage_summary 失败: {ru.status} {ru.body[:200]!r}")
        ruj = ru.json()
        if not bool(ruj.get("ok", False)):
            raise RuntimeError(f"/api/ext/usage_summary 返回 ok=false: {ru.body[:300]!r}")

        # 7) management.html 可访问（CPA UI）
        r3 = http_request(f"{proxy_base}/management.html", timeout=10)
        if r3.status != 200:
            raise RuntimeError(f"/management.html 不可访问: {r3.status}")

        # 8) usage before
        usage_auth = {"Authorization": f"Bearer {mgmt_key}"}
        u0 = http_request(f"{proxy_base}/v0/management/usage", headers=usage_auth, timeout=10)
        if u0.status != 200:
            raise RuntimeError(f"/v0/management/usage 失败: {u0.status}")
        u0j = u0.json()
        before = int(((u0j.get("usage") or {}).get("total_requests") or 0))

        # 9) responses（可选强制）
        # 某些环境下 alias 模型可能偶发不可用，这里做一次“模型回退”以提高冒烟稳定性：
        # - 优先 prefer 列表
        # - 失败则尝试 models 列表中的其它 id
        resp_ok = False
        r4 = None
        tried = []
        candidates = list(prefer) + [m for m in ids if m not in prefer]
        infer_deadline = time.time() + min(max(int(args.timeout), 20), 90)
        while time.time() < infer_deadline and not resp_ok:
            round_all_unknown = True
            for m in candidates:
                tried.append(m)
                infer_model = m
                r4 = http_request(
                    f"{proxy_base}/v1/responses",
                    method="POST",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json_body={"model": infer_model, "input": "ping"},
                    timeout=60,
                )
                if r4.status == 200:
                    resp_ok = True
                    break
                if not body_contains_unknown_provider(r4):
                    round_all_unknown = False
            if resp_ok:
                break
            if round_all_unknown:
                # 可能是 proxy 刚启动，provider/账号池仍在加载；稍等重试
                time.sleep(2)
                continue
            break
        if not resp_ok and args.require_responses:
            raise RuntimeError(
                f"/v1/responses 失败（require-responses=true）: last_status={getattr(r4,'status',None)} tried={tried} body={getattr(r4,'body',b'')[:500]!r}"
            )

        # 9.5) cache test（可选）
        cache_hit = False
        chat_cache_hit = False
        chat_test_ran = False
        load_result = {"ok": False, "msg": "未执行"}
        if args.test_cache and resp_ok:
            r4b = http_request(
                f"{proxy_base}/v1/responses",
                method="POST",
                headers={"Authorization": f"Bearer {api_key}"},
                json_body={"model": infer_model, "input": "ping"},
                timeout=60,
            )
            if r4b.status != 200:
                raise RuntimeError(f"缓存测试二次 /v1/responses 失败: {r4b.status} {r4b.body[:200]!r}")
            # 网关命中会附带该 header
            h = {str(k).lower(): str(v) for k, v in (r4b.headers or {}).items()}
            cache_hit = (h.get("x-aiproxyhub-cache", "").upper() == "HIT")
            if not cache_hit:
                raise RuntimeError(f"缓存未命中（期望 X-AIProxyHub-Cache=HIT，实际 {r4b.headers.get('X-AIProxyHub-Cache')!r}）")

            # best-effort：验证 /v1/chat/completions 也可命中缓存（不同客户端常用该端点）
            rc1 = http_request(
                f"{proxy_base}/v1/chat/completions",
                method="POST",
                headers={"Authorization": f"Bearer {api_key}"},
                json_body={"model": infer_model, "messages": [{"role": "user", "content": "ping"}], "stream": False},
                timeout=60,
            )
            if rc1.status == 200:
                chat_test_ran = True
                rc2 = http_request(
                    f"{proxy_base}/v1/chat/completions",
                    method="POST",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json_body={"model": infer_model, "messages": [{"role": "user", "content": "ping"}], "stream": False},
                    timeout=60,
                )
                if rc2.status == 200:
                    hc = {str(k).lower(): str(v) for k, v in (rc2.headers or {}).items()}
                    chat_cache_hit = (hc.get("x-aiproxyhub-cache", "").upper() == "HIT")

            # 安全回归：开启“跨 API Key 共享缓存”时，未鉴权请求不应通过缓存返回 200
            if bool(getattr(args, "share_cache_across_api_keys", False)):
                r_bad = http_request(
                    f"{proxy_base}/v1/responses",
                    method="POST",
                    headers={},  # 故意不带 Authorization
                    json_body={"model": infer_model, "input": "ping"},
                    timeout=30,
                )
                if r_bad.status == 200:
                    hb = {str(k).lower(): str(v) for k, v in (r_bad.headers or {}).items()}
                    raise RuntimeError(
                        f"安全回归失败：开启共享缓存时，未鉴权请求不应返回 200（x-aiproxyhub-cache={hb.get('x-aiproxyhub-cache')!r}）"
                    )

            # 轻量并发压测：优先压测“缓存 HIT”路径（不消耗额度、也更能代表网关吞吐能力）
            if int(getattr(args, "load_rps", 0) or 0) > 0:
                load_result = run_rps_load_test(
                    url=f"{proxy_base}/v1/responses",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json_body={"model": infer_model, "input": "ping"},
                    rps=int(getattr(args, "load_rps", 0) or 0),
                    duration_s=int(getattr(args, "load_seconds", 5) or 5),
                    timeout=30,
                )

        # 10) usage after（若 responses 成功：无缓存=+1；开启缓存且命中=仍应 +1）
        u1 = http_request(f"{proxy_base}/v0/management/usage", headers=usage_auth, timeout=10)
        if u1.status != 200:
            raise RuntimeError(f"/v0/management/usage(2) 失败: {u1.status}")
        u1j = u1.json()
        after = int(((u1j.get("usage") or {}).get("total_requests") or 0))
        after_tokens = int(((u1j.get("usage") or {}).get("total_tokens") or 0))
        after_success = int(((u1j.get("usage") or {}).get("success_count") or 0))
        after_failure = int(((u1j.get("usage") or {}).get("failure_count") or 0))
        if resp_ok and after < before + 1:
            raise RuntimeError(f"usage 未按预期增长（before={before}, after={after}）")
        if args.test_cache and resp_ok and cache_hit:
            # 期望只计入“每个端点 1 次回源”（第二次应命中缓存，不应增加 usage）
            expected_increase = 1 + (1 if chat_test_ran else 0)
            if after > before + expected_increase:
                raise RuntimeError(
                    f"开启缓存后 usage 增长过多（期望只计入 {expected_increase} 次回源；before={before}, after={after}）"
                )

        # 11) quota summary（仅验证接口可用，不输出全量明细）
        rq = http_request(f"{launcher_base}/api/ext/quota", headers=auth, timeout=30)
        if rq.status != 200:
            raise RuntimeError(f"/api/ext/quota 失败: {rq.status} {rq.body[:200]!r}")
        rqj = rq.json()
        quota_out = {
            "ok": bool(rqj.get("ok", False)),
            "total": int(rqj.get("total", 0) or 0),
            "active": int(rqj.get("active", 0) or 0),
            "pct": int(rqj.get("pct", 0) or 0),
            "below_threshold": bool(rqj.get("below_threshold", False)),
        }

        # 输出：只打印端点、端口、以及脱敏 key（便于回放）
        print(json.dumps(
            {
                "ok": True,
                "zip": str(zip_path),
                "work_dir": str(work_dir),
                "launcher_base": launcher_base,
                "proxy_base": proxy_base,
                "api_key_masked": mask_secret(api_key),
                "management_key_masked": mask_secret(mgmt_key),
                "models_count": models_count,
                "models_sample": models_sample,
                "infer_model": infer_model,
                "usage_total_requests": after,
                "usage_total_tokens": after_tokens,
                "usage_success_count": after_success,
                "usage_failure_count": after_failure,
                "responses_ok": bool(resp_ok),
                "cache_test_enabled": bool(args.test_cache),
                "cache_shared_across_api_keys": bool(getattr(args, "share_cache_across_api_keys", False)),
                "cache_hit": bool(cache_hit),
                "chat_cache_hit": bool(chat_cache_hit),
                "load_test": load_result,
                "register_worker_ok": bool(register_worker_ok),
                "quota": quota_out,
            },
            ensure_ascii=False,
        ))
        return 0
    finally:
        # 尽量温和关闭 proxy，避免残留进程；即使失败也继续终止 launcher 进程
        try:
            settings = read_json(home_dir / "settings.json")
            api_key = maybe_dpapi_decrypt(settings.get("api_key", ""))
            if api_key:
                http_request(f"http://127.0.0.1:{launcher_port}/api/ext/stop_proxy", method="POST", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
        except Exception:
            pass

        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        # 默认不做强制清理：保留 work_dir 便于排查（可由用户自行删除）
        if args.keep:
            print(json.dumps({"kept_work_dir": str(work_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
