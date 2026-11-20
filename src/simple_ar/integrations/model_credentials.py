"""Per-user Windows storage for named API credentials."""

import ctypes as ct
from ctypes import wintypes as wt
from hashlib import sha256
import os


class _Credential(ct.Structure):
    _fields_ = [
        ("flags", wt.DWORD), ("kind", wt.DWORD), ("target", wt.LPWSTR),
        ("comment", wt.LPWSTR), ("last_written", wt.FILETIME),
        ("size", wt.DWORD), ("blob", ct.POINTER(wt.BYTE)),
        ("persist", wt.DWORD), ("attribute_count", wt.DWORD), ("attributes", ct.c_void_p),
        ("target_alias", wt.LPWSTR), ("username", wt.LPWSTR),
    ]


def _target(reference: str, endpoint: str) -> str:
    # A catalog pointed at a different endpoint cannot retrieve this key.
    return "SimpleAutoResearch/" + sha256(f"{reference}\0{endpoint}".encode()).hexdigest()


def read_key(reference: str, endpoint: str) -> str:
    if os.name != "nt":
        return ""
    api = ct.WinDLL("Advapi32", use_last_error=True)
    api.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ct.POINTER(ct.POINTER(_Credential))]
    api.CredReadW.restype = wt.BOOL
    api.CredFree.argtypes = [ct.c_void_p]
    api.CredFree.restype = None
    pointer = ct.POINTER(_Credential)()
    if not api.CredReadW(_target(reference, endpoint), 1, 0, ct.byref(pointer)):
        return ""
    try:
        value = pointer.contents
        return ct.string_at(value.blob, value.size).decode("utf-8")
    except UnicodeDecodeError:
        return ""
    finally:
        api.CredFree(pointer)


def write_key(reference: str, endpoint: str, key: str) -> None:
    if os.name != "nt":
        raise ValueError("请通过模型配置中的环境变量提供 API Key。")
    raw = key.encode("utf-8")
    if not raw or len(raw) > 2560:
        raise ValueError("API Key 长度不受支持。")
    blob = (wt.BYTE * len(raw)).from_buffer_copy(raw)
    api = ct.WinDLL("Advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ct.POINTER(_Credential), wt.DWORD]
    api.CredWriteW.restype = wt.BOOL
    value = _Credential(kind=1, target=_target(reference, endpoint), size=len(raw),
                        blob=ct.cast(blob, ct.POINTER(wt.BYTE)), persist=2)
    if not api.CredWriteW(ct.byref(value), 0):
        raise OSError(f"Windows 凭据保存失败（错误 {ct.get_last_error()}）。")
