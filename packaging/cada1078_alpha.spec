# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path.cwd()

block_cipher = None

a = Analysis(
    [str(ROOT / "packaging" / "cada1078_alpha_wrapper.py")],
    pathex=[str(ROOT / "src"), str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "src"), "src"),
        (str(ROOT / "mcp_tools_spec.json"), "."),
        (str(ROOT / "abc_resources"), "abc_resources"),
        (str(ROOT / "configs" / "contest.yml"), "configs"),
        *collect_data_files("pyverilog"),
    ],
    hiddenimports=[
        "yaml",
        "networkx",
        "pyverilog",
        "pyverilog.vparser",
        "pyverilog.vparser.parser",
        "pyverilog.vparser.ast",
        "ply",
        "ply.lex",
        "ply.yacc",
    ],
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cada1078_alpha_bin",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="cada1078_alpha_dist",
)
