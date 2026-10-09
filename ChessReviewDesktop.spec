import os

HERE = SPECPATH

datas = [(os.path.join(HERE, 'web'), 'web')]
for _extra in ('openings_cache.json', 'initial_game.json', 'stockfish.exe'):
    _p = os.path.join(HERE, _extra)
    if os.path.exists(_p):
        datas.append((_p, '.'))

a = Analysis(
    [os.path.join(HERE, 'app.py')],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['webview', 'webview.platforms.winforms', 'webview.platforms.edgechromium', 'clr', 'engine', 'openings', 'history_manager', 'ai_provider', 'hardware_scanner', 'server', 'app_paths'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ChessReviewDesktop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        'stockfish.exe',
        'vcruntime140.dll',
        'vcruntime140_1.dll',
        'python3.dll',
        'python314.dll',
        'api-ms-win-*',
    ],
    name='ChessReviewDesktop',
)
