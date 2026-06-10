# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

spec_dir = Path(SPECPATH).resolve()
repo = spec_dir.parent
if not (repo / "src" / "contest_agent.py").exists():
    repo = Path("/src")

a = Analysis(
    [str(repo / "src" / "contest_agent.py")],
    pathex=[str(repo / "src")],
    binaries=[],
    datas=[
        (str(repo / "mcp_tools_spec.json"), "."),
        (str(repo / "docs" / "llm_tool_routing_guide_for_llm.md"), "docs"),
        (str(repo / "abc_resources" / "abc.rc"), "abc_resources"),
        (str(repo / "abc_resources" / "my.genlib"), "abc_resources"),
    ] + collect_data_files("pyverilog"),
    hiddenimports=[
        "eda_core",
        "eda_transform",
        "eda_abc",
        "nx_probe",
        "pyv_extractor",
        "pyverilog.vparser.parser",
        "pyverilog.vparser.ast",
        "pyverilog.ast_code_generator.codegen",
        "ply.lex",
        "ply.yacc",
        "yaml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "analysis_check",
        "verify_equiv",
        "llm_vs_regex",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cada1078_alpha",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
)
