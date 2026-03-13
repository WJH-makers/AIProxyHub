#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本脚本用于本机 AIProxyHub + CLIProxyAPI 的“可复现”冒烟测试：

1) /v1/models 是否可用（带鉴权）
2) 目标别名模型（例如 gpt5.2-codex-high / gpt5.2-xhigh）是否出现在 /models
3) /v1/chat/completions 对别名模型的返回（用于判定是否需要网关层做 alias 映射）
4) /v1/responses 的 reasoning.effort 支持范围（high / xhigh）

注意：
- 不会打印 api_key 明文（从 launcher.load_settings() 里读取 DPAPI 解密后的值，仅用于请求头）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# 确保能从 scripts/ 目录运行并导入项目根目录的 launcher.py
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import launcher  # noqa: E402


def _http_json(
    *,
    base: str,
    path: str,
    method: str = "GET",
    token: str,
    json_body: dict | None = None,
    timeout_s: int = 30,
) -> tuple[int, dict, bytes]:
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw = r.read() or b""
            try:
                obj = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                obj = {}
            return int(getattr(r, "status", 200) or 200), obj, raw
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception:
            obj = {}
        return int(getattr(e, "code", 0) or 0), obj, raw
    except Exception as e:
        return 0, {"error": str(e)}, str(e).encode("utf-8", errors="replace")


def _print_kv(k: str, v) -> None:
    sys.stdout.write(f"{k}={v}\n")


def main() -> int:
    base = "http://127.0.0.1:8317/v1"
    s = launcher.load_settings()
    token = str(s.get("api_key", "") or "")
    if not token:
        _print_kv("ok", False)
        _print_kv("error", "api_key 为空（请在 AIProxyHub 配置页设置 API Key）")
        return 2

    # 1) /models
    st, obj, _raw = _http_json(base=base, path="/models", method="GET", token=token, timeout_s=10)
    model_ids: list[str] = []
    if isinstance(obj, dict) and isinstance(obj.get("data"), list):
        for x in obj["data"]:
            if isinstance(x, dict) and x.get("id"):
                model_ids.append(str(x["id"]))
    model_ids = sorted(set(model_ids))
    _print_kv("models.http", st)
    _print_kv("models.count", len(model_ids))
    for mid in model_ids:
        sys.stdout.write(f"model.id={mid}\n")

    wanted_aliases = [
        "gpt5.2-codex-high",
        "gpt5.2-xhigh",
        "gpt-5.2-codex-high",
        "gpt-5.2-xhigh",
    ]
    for a in wanted_aliases:
        sys.stdout.write(f"alias.in_models.{a}={a in model_ids}\n")

    # 2) /chat/completions：用别名模型试探（期待：若未做 alias 映射会报 unknown provider）
    for a in wanted_aliases:
        st2, obj2, raw2 = _http_json(
            base=base,
            path="/chat/completions",
            method="POST",
            token=token,
            json_body={
                "model": a,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            },
            timeout_s=30,
        )
        msg = ""
        if isinstance(obj2, dict):
            msg = str(obj2.get("error") or obj2.get("message") or "")
        if not msg:
            msg = raw2.decode("utf-8", errors="replace")[:160].replace("\n", " ")
        sys.stdout.write(f"chat.alias.http.{a}={st2}\n")
        sys.stdout.write(f"chat.alias.err.{a}={msg}\n")

    # 3) /responses：reasoning.effort 测试（high/xhigh）
    tests = [
        # 基线：原始模型 + effort
        ("gpt-5.2-codex", "high"),
        ("gpt-5.2-codex", "xhigh"),
        ("gpt-5.2-codex-full", "high"),
        ("gpt-5.2-codex-full", "xhigh"),
        # 重点：用户需要的别名模型是否能在 /responses 正常使用（建议用 high）
        ("gpt5.2-codex-high", "high"),
        ("gpt5.2-xhigh", "high"),
        ("gpt-5.2-codex-high", "high"),
        ("gpt-5.2-xhigh", "high"),
    ]
    for model, effort in tests:
        st3, obj3, raw3 = _http_json(
            base=base,
            path="/responses",
            method="POST",
            token=token,
            json_body={
                "model": model,
                "input": "ping",
                "reasoning": {"effort": effort},
            },
            timeout_s=60,
        )
        msg3 = ""
        if isinstance(obj3, dict):
            msg3 = str(obj3.get("error") or obj3.get("message") or "")
        if not msg3:
            msg3 = raw3.decode("utf-8", errors="replace")[:200].replace("\n", " ")
        sys.stdout.write(f"responses.http.{model}.{effort}={st3}\n")
        sys.stdout.write(f"responses.msg.{model}.{effort}={msg3}\n")

    _print_kv("ok", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
