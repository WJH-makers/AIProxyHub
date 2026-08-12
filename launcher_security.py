import os
import re
import time
import ctypes
from ctypes import wintypes

_REDACT_PATTERNS = [
    (re.compile(r"\bdk_[A-Za-z0-9]{8,}\b"), "dk_***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"), "sk-***"),
    (re.compile(r"\baiph_[A-Za-z0-9_-]{6,}\b"), "aiph_***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "jwt_***"),
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

def _is_windows():
    return os.name == "nt"

def _try_truncate_file(path: str) -> bool:
    if not path:
        return False
    try:
        with open(path, "r+b") as f:
            try:
                f.seek(0)
            except Exception:
                pass
            f.truncate(0)
        return True
    except Exception:
        return False

def _schedule_delete_on_reboot(path: str) -> bool:
    if not path or not _is_windows():
        return False
    try:
        MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004
        kernel32 = ctypes.windll.kernel32
        kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        kernel32.MoveFileExW.restype = wintypes.BOOL
        return bool(kernel32.MoveFileExW(str(path), None, MOVEFILE_DELAY_UNTIL_REBOOT))
    except Exception:
        return False

def _safe_unlink(path: str):
    from launcher import log
    if not path:
        return
    retries = 4 if _is_windows() else 1
    for i in range(int(retries)):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError as e:
            _try_truncate_file(path)
            if i < retries - 1:
                time.sleep(0.12 * (2**i))
                continue
            if _schedule_delete_on_reboot(path):
                log(f"[WARN] 删除临时文件失败（已计划重启后删除）: {path} - {e}")
            else:
                log(f"[WARN] 删除临时文件失败: {path} - {e}")
            return
        except Exception as e:
            if i < retries - 1 and _is_windows():
                time.sleep(0.12 * (2**i))
                continue
            log(f"[WARN] 删除临时文件失败: {path} - {e}")
            return
