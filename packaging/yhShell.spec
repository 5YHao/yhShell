# -*- mode: python ; coding: utf-8 -*-
"""Windows x64 onedir distribution. Run through packaging/build.ps1."""
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, copy_metadata

root = Path(SPECPATH).parent
webview_data, webview_bins, webview_hidden = collect_all('webview')
clr_data, clr_bins, clr_hidden = collect_all('clr_loader')
pythonnet_data, pythonnet_bins, pythonnet_hidden = collect_all('pythonnet')

analysis = Analysis(
    [str(root / 'app.py')],
    pathex=[str(root)],
    binaries=webview_bins + clr_bins + pythonnet_bins,
    datas=[(str(root / 'web'), 'web')] + webview_data + clr_data + pythonnet_data
          + copy_metadata('pywebview'),
    hiddenimports=webview_hidden + clr_hidden + pythonnet_hidden + ['webview.platforms.winforms'],
    hookspath=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'gi', 'cefpython3'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [],
    exclude_binaries=True,
    name='yhShell',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
collection = COLLECT(exe, analysis.binaries, analysis.datas,
                     strip=False, upx=False, name='yhShell')
