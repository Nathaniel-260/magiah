# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the magiah review-UI launcher.

One-file build, console KEPT (so startup errors are visible; a --noconsole
windowed build can come later). Bundles the whole magiah package plus the
webui static SPA files (index.html / app.js / style.css) at
magiah/webui/static so server.py finds them when frozen.

Build:  python -X utf8 -m PyInstaller magiah.spec --noconfirm
Output: dist/magiah.exe
"""
import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Package data that server.py loads from disk at runtime (not importable code,
# so PyInstaller does not pick it up automatically). The static SPA MUST be
# bundled or GET / returns the Hebrew placeholder instead of the real UI.
datas = [
    ('magiah/webui/static', 'magiah/webui/static'),
]

# Pull in every magiah submodule so lazy/deferred imports (webui.*, corpus_*,
# review, etc.) survive freezing.
hiddenimports = collect_submodules('magiah')

# zstd decoder for schema-6 seforim.db (see magiah/textsource.py). It is
# imported lazily inside a try, so a build environment without one would
# freeze an exe that fails every scan on a modern Otzaria DB — refuse instead.
try:
    import compression.zstd  # noqa: F401  (Python 3.14+ stdlib)
    hiddenimports += ['compression.zstd', '_zstd']
except ImportError:
    try:
        import zstandard  # noqa: F401
    except ImportError:
        raise SystemExit(
            'magiah.spec: no zstd decoder in this Python — run '
            '"pip install zstandard" (or build with Python 3.14+) first')
    hiddenimports += collect_submodules('zstandard')

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Icon: use one only if it happens to exist next to the spec, else skip.
_icon = 'magiah.ico' if os.path.exists('magiah.ico') else None

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='magiah',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)
