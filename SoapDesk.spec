# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['soapdesk.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Kullanilmayan modulleri disarida birak (~4 MB). Bu listedekiler test
    # edildi; unicodedata (idna) ve decimal (zeep xsd tipleri) ZORUNLU, onlar
    # cikarilirsa exe acilmaz.
    excludes=[
        'sqlite3', '_sqlite3',
        'lxml.objectify', 'lxml.html', 'lxml.isoschematron', 'lxml.cssselect',
        '_zstd', 'compression.zstd', 'zstandard',
        'bz2', '_bz2', 'lzma', '_lzma',
        'multiprocessing',
        'unittest', 'doctest', 'pdb', 'pydoc',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SoapDesk',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
