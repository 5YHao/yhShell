"""Launch a distribution with a temporary profile; verify native window then close.
Usage: .venv/Scripts/python.exe packaging/smoke.py dist/yhShell/yhShell.exe
Windows only; does not use or modify the real user's application profile.
"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

exe = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='yhshell-package-smoke-') as profile:
    Path(profile, 'config.json').write_text(json.dumps({'settings': {'updateAutoCheck': False, 'modelSyncHours': 0}}), encoding='utf-8')
    env = dict(os.environ, YHSELL_HOME=profile)
    process = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    window = None
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32 = ctypes.windll.user32
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    def visit(hwnd, _):
        global window
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process.pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            print('WINDOW:', title.value, flush=True)
            if title.value == 'yhShell':
                window = hwnd
        return True
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.communicate()[0].decode('utf-8', errors='replace')
                raise RuntimeError(f'Application exited before window: {process.returncode}\n{output}')
            user32.EnumWindows(callback_type(visit), 0)
            if window:
                break
            time.sleep(.5)
        if not window:
            raise RuntimeError('No yhShell native window after 60 seconds')
        time.sleep(3)
        if process.poll() is not None:
            raise RuntimeError('Application crashed after window opened')
        print('PASS: packaged native window opened and stayed alive', flush=True)
        user32.PostMessageW(window, 0x0010, 0, 0)
        process.wait(timeout=15)
        if process.returncode != 0:
            raise RuntimeError(f'Application close returned {process.returncode}')
        print('PASS: clean close', flush=True)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
