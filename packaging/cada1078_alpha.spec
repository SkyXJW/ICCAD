# -*- mode: python ; coding: utf-8 -*-

import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

spec_dir = Path(SPECPATH).resolve()
repo = spec_dir.parent
entrypoint = repo / "src" / "contest_agent.py"
if not entrypoint.exists():
    raise FileNotFoundError(f"cannot find contest entrypoint: {entrypoint}")

eda_bundle = Path(os.environ.get("CADA_EDA_BUNDLE_DIR", repo / "packaging" / "out" / "eda_bundle")).resolve()
if not eda_bundle.exists():
    raise FileNotFoundError(f"cannot find staged EDA bundle: {eda_bundle}; run packaging/build_pyinstaller.sh")


def tree_datas(src_root: Path, dest_root: str):
    if not src_root.exists():
        raise FileNotFoundError(f"missing bundled data directory: {src_root}")
    result = []
    for path in src_root.rglob("*"):
        if path.is_file():
            dest = Path(dest_root) / path.relative_to(src_root).parent
            result.append((str(path), str(dest)))
    return result


eda_binaries = []
for name in ["yosys", "berkeley-abc", "yosys-abc", "abc", "iverilog.real", "vvp"]:
    path = eda_bundle / "bin" / name
    if not path.exists():
        raise FileNotFoundError(f"missing staged EDA executable: {path}")
    eda_binaries.append((str(path), "eda/bin"))

for path in (eda_bundle / "lib64").glob("*.so*"):
    if path.is_file():
        eda_binaries.append((str(path), "eda/lib64"))

eda_datas = []
eda_datas += tree_datas(eda_bundle / "share" / "yosys", "eda/share/yosys")
eda_datas += tree_datas(eda_bundle / "lib" / "ivl", "eda/lib/ivl")

certifi_datas = []
certifi_hiddenimports = []
if importlib.util.find_spec("certifi") is not None:
    certifi_datas += collect_data_files("certifi")
    certifi_hiddenimports.append("certifi")
ca_bundle = Path(sys.prefix) / "ssl" / "cacert.pem"
if ca_bundle.exists():
    certifi_datas.append((str(ca_bundle), "certifi"))
elif not certifi_datas:
    raise FileNotFoundError(
        "cannot find certifi package or conda CA bundle; install certifi or ca-certificates in the build environment"
    )

a = Analysis(
    [str(entrypoint)],
    pathex=[str(repo / "src")],
    binaries=eda_binaries,
    datas=[
        (str(repo / "mcp_tools_spec.json"), "."),
        (str(repo / "docs" / "llm_tool_routing_guide_for_llm.md"), "docs"),
        (str(repo / "abc_resources" / "abc.rc"), "abc_resources"),
        (str(repo / "abc_resources" / "my.genlib"), "abc_resources"),
    ] + eda_datas + collect_data_files("pyverilog") + certifi_datas,
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
    ] + certifi_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(repo / "packaging" / "pyi_runtime_eda.py")],
    excludes=[
        "analysis_check",
        "verify_equiv",
        "llm_vs_regex",
        # Keep the frozen contest binary small and avoid collecting optional
        # scientific/ML stacks from the build environment. The agent only uses
        # NetworkX core graph algorithms, not these optional integrations.
        "matplotlib",
        "numpy",
        "pandas",
        "PIL",
        "scipy",
        "sklearn",
        "tensorflow",
        "torch",
        "torchvision",
        "triton",
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
