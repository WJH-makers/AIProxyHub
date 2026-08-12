import ctypes
import base64
import os
from ctypes import wintypes

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
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)
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
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(_DATA_BLOB),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)
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
