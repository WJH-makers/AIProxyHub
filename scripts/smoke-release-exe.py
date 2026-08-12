"""Smoke-test the launcher inside a Windows release archive.

This test is intentionally provider-independent: it verifies that the bundled
launcher starts, protects the external management API, exposes a redacted
settings view, and reports the clean runtime shape. It never contacts an AI
provider and never uses a real credential.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


REQUIRED_RELEASE_FILES = {
    "LICENSE",
    "NOTICE.md",
    "THIRD_PARTY_NOTICES.md",
    "SECURITY.md",
    "PRIVACY.md",
    "MAINTAINERS.md",
    "CHANGELOG.md",
    "使用指南.md",
    "API.md",
    "cli-proxy-api.provenance.json",
}
FORBIDDEN_RELEASE_MARKERS = (
    "duckmail.sbs",
    "sentinel.openai",
    "register-worker",
    "register/",
    "registered_accounts",
    "codex_tokens/",
    "ak.txt",
    "rk.txt",
    "protocol_keygen",
    "chatgpt_register.py",
)


def http_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 5) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            data = {"raw": body}
        return int(exc.code), data


def http_text(url: str, *, timeout: float = 5) -> tuple[int, str, dict[str, str]]:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            return int(response.status), response.read().decode("utf-8", errors="replace"), headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        headers = {str(k).lower(): str(v) for k, v in exc.headers.items()}
        return int(exc.code), body, headers


def wait_for_status(base: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            status, data = http_json(f"{base}/api/status", timeout=3)
            if status == 200:
                return data
            last_error = f"HTTP {status}: {data}"
        except Exception as exc:  # startup race is expected
            last_error = str(exc)
        time.sleep(0.25)
    raise TimeoutError(f"launcher did not become ready: {last_error}")


def find_exe(root: Path) -> Path:
    candidates = [root / "AIProxyHub.exe", *root.rglob("AIProxyHub.exe")]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("release archive does not contain AIProxyHub.exe")


def stop_process_tree(process: subprocess.Popen) -> None:
    """Stop the exact smoke process and descendants so test ports cannot leak."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="AIProxyHub Windows release launcher smoke test")
    parser.add_argument("--zip", required=True, help="release zip path")
    parser.add_argument("--timeout", type=float, default=120, help="startup timeout in seconds")
    parser.add_argument("--keep", action="store_true", help="keep the temporary extraction directory")
    args = parser.parse_args()

    archive = Path(args.zip).resolve()
    if not archive.is_file():
        raise FileNotFoundError(str(archive))

    work_dir = Path(tempfile.mkdtemp(prefix="aiproxyhub_smoke_"))
    extract_dir = work_dir / "extract"
    home_dir = work_dir / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen | None = None
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = {name.replace("\\", "/").rstrip("/") for name in bundle.namelist()}
            missing = sorted(REQUIRED_RELEASE_FILES - names)
            if missing:
                raise RuntimeError(f"release archive is missing required compliance files: {missing}")
            forbidden_names = sorted(
                name for name in names if any(marker in name.lower() for marker in FORBIDDEN_RELEASE_MARKERS)
            )
            if forbidden_names:
                raise RuntimeError(f"release archive contains removed workflow paths: {forbidden_names}")
            provenance = json.loads(bundle.read("cli-proxy-api.provenance.json").decode("utf-8-sig"))
            digest = str(provenance.get("executable_sha256", "")).strip()
            if len(digest) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in digest):
                raise RuntimeError("release provenance has no valid CLIProxyAPI SHA-256")
            license_names = {name for name in names if name.startswith("licenses/")}
            if not {"licenses/CLIProxyAPI-LICENSE.txt", "licenses/zstandard-LICENSE.txt"}.issubset(license_names):
                raise RuntimeError(f"release archive is missing required third-party licenses: {sorted(license_names)}")
            bundle.extractall(extract_dir)
        exe = find_exe(extract_dir)

        env = os.environ.copy()
        env["AIPROXYHUB_HOME"] = str(home_dir)
        env["AIPROXYHUB_NO_BROWSER"] = "1"
        launcher_port = 19090
        base = f"http://127.0.0.1:{launcher_port}"
        process = subprocess.Popen(
            [str(exe), "--host", "127.0.0.1", "--port", str(launcher_port), "--strict-port", "--no-browser"],
            cwd=str(exe.parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )

        status = wait_for_status(base, float(args.timeout))
        if "register" in status or "autopilot" in status or "accounts" in status:
            raise RuntimeError(f"clean runtime status still exposes removed account workflow: {status}")

        code, page, page_headers = http_text(f"{base}/")
        if code != 200:
            raise RuntimeError(f"management page failed: HTTP {code}")
        for marker in ("AIProxyHub", "Codex"):
            if marker not in page:
                raise RuntimeError(f"management page is missing expected marker: {marker}")
        for forbidden in FORBIDDEN_RELEASE_MARKERS:
            if forbidden.lower() in page.lower():
                raise RuntimeError(f"management page contains removed workflow marker: {forbidden}")
        for header, expected in (("x-content-type-options", "nosniff"), ("x-frame-options", "DENY")):
            if page_headers.get(header, "").lower() != expected.lower():
                raise RuntimeError(f"management page is missing security header {header}: {page_headers}")

        code, settings = http_json(f"{base}/api/settings")
        if code != 200:
            raise RuntimeError(f"settings endpoint failed: HTTP {code} {settings}")
        for secret in ("api_key", "management_password", "admin_api_key", "api_keys_extra"):
            if settings.get(secret):
                raise RuntimeError(f"settings endpoint leaked secret field: {secret}")

        for limit_field in ("gateway_rate_limit_rpm", "gateway_max_concurrent_requests"):
            if int(settings.get(limit_field, 0) or 0) <= 0:
                raise RuntimeError(
                    f"packaged build ships with the gateway limit disabled: {limit_field}={settings.get(limit_field)!r}"
                )

        code, _ = http_json(f"{base}/api/ext/status")
        if code != 401:
            raise RuntimeError(f"external management API accepted an unauthenticated request: HTTP {code}")

        print(json.dumps({"ok": True, "archive": str(archive), "exe": str(exe), "status": status}, ensure_ascii=False))
        return 0
    finally:
        if process is not None:
            stop_process_tree(process)
        if not args.keep:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"kept smoke directory: {work_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
