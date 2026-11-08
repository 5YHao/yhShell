"""Windows DPAPI helpers used to protect saved secrets at rest.

Secrets (SSH passwords, key passphrases, LLM API keys) are never written to
disk in plain text when DPAPI is available.  The produced token has the form
``dpapi:<base64>``.  On non-Windows platforms, or when DPAPI fails, the value
is stored with a ``b64:`` prefix instead (obfuscation only) so the app keeps
working instead of crashing.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import sys

_PREFIX_DPAPI = "dpapi:"
_PREFIX_B64 = "b64:"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    _crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def _make_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data, len(data))
        blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        return blob, buffer

    def _dpapi_protect(raw: bytes) -> bytes:
        in_blob, _keep = _make_blob(raw)
        out_blob = _DataBlob()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "yhShell secret",
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            _kernel32.LocalFree(out_blob.pbData)

    def _dpapi_unprotect(raw: bytes) -> bytes:
        in_blob, _keep = _make_blob(raw)
        out_blob = _DataBlob()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            _kernel32.LocalFree(out_blob.pbData)


def protect(secret: str) -> str:
    """Encrypt ``secret`` for storage.  Empty input stays empty."""
    if not secret:
        return ""
    raw = secret.encode("utf-8")
    if _IS_WINDOWS:
        try:
            return _PREFIX_DPAPI + base64.b64encode(_dpapi_protect(raw)).decode("ascii")
        except OSError:
            pass
    return _PREFIX_B64 + base64.b64encode(raw).decode("ascii")


def unprotect(token: str) -> str:
    """Decrypt a stored secret.  Unreadable values return an empty string."""
    if not token:
        return ""
    try:
        if token.startswith(_PREFIX_DPAPI):
            if not _IS_WINDOWS:
                return ""
            return _dpapi_unprotect(base64.b64decode(token[len(_PREFIX_DPAPI):])).decode("utf-8")
        if token.startswith(_PREFIX_B64):
            return base64.b64decode(token[len(_PREFIX_B64):]).decode("utf-8")
    except Exception:  # noqa: BLE001 - unreadable secret must not crash startup
        return ""
    # Legacy / plain values are accepted so hand-edited configs still work.
    return token
