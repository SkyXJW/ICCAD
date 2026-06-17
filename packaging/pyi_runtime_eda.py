"""PyInstaller runtime hook for bundled EDA command-line tools.

The contest executable is evaluated on machines that may not provide yosys,
Berkeley ABC, or Icarus Verilog. PyInstaller extracts the staged tool tree under
sys._MEIPASS; this hook exposes those tools to the existing runtime resolvers and
to child processes launched by PyVerilog/Yosys/ABC.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

try:
    import certifi
except Exception:  # pragma: no cover - depends on frozen optional bundle
    certifi = None


def _prepend_env_path(name: str, value: Path) -> None:
    if not value.exists():
        return
    current = os.environ.get(name, "")
    text = str(value)
    os.environ[name] = text if not current else text + os.pathsep + current


def _write_iverilog_wrapper(wrapper_dir: Path, real_iverilog: Path, ivl_dir: Path) -> Path:
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_dir / "iverilog"
    wrapper.write_text(
        "#!/bin/sh\n"
        "set -e\n"
        f"exec {str(real_iverilog)!r} -B {str(ivl_dir)!r} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


def _ca_bundle_path(root: Path) -> Path | None:
    if certifi is not None:
        try:
            ca_bundle = Path(certifi.where())
        except Exception:
            ca_bundle = None
        if ca_bundle is not None and ca_bundle.exists():
            return ca_bundle
    bundled = root / "certifi" / "cacert.pem"
    if bundled.exists():
        return bundled
    return None


root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
eda = root / "eda"
bin_dir = eda / "bin"
lib_dir = eda / "lib64"
yosys_share = eda / "share" / "yosys"
ivl_dir = eda / "lib" / "ivl"

ca_bundle = _ca_bundle_path(root)
if ca_bundle is not None:
    os.environ.setdefault("SSL_CERT_FILE", str(ca_bundle))
    os.environ.setdefault("REQUESTS_CA_BUNDLE", str(ca_bundle))

# Child-process dynamic linker search path. This is intentionally scoped to
# subprocesses launched after startup; do not bundle or override glibc itself.
_prepend_env_path("LD_LIBRARY_PATH", lib_dir)

# Prefer a generated Icarus wrapper so PyVerilog's internal `iverilog` call uses
# the relocated ivl support directory instead of a compiled-in /usr path.
real_iverilog = bin_dir / "iverilog.real"
if real_iverilog.exists() and ivl_dir.exists():
    wrapper_root = Path(tempfile.mkdtemp(prefix="cada_iverilog_"))
    wrapper = _write_iverilog_wrapper(wrapper_root, real_iverilog, ivl_dir)
    _prepend_env_path("PATH", wrapper_root)
    os.environ.setdefault("IVERILOG_BIN", str(wrapper))
else:
    os.environ.setdefault("IVERILOG_BIN", str(bin_dir / "iverilog"))

_prepend_env_path("PATH", bin_dir)

os.environ.setdefault("YOSYS_BIN", str(bin_dir / "yosys"))
os.environ.setdefault("ABC_BIN", str(bin_dir / "berkeley-abc"))
os.environ.setdefault("ABC_CEC_BIN", str(bin_dir / "yosys-abc"))
if yosys_share.exists():
    os.environ.setdefault("YOSYS_DATDIR", str(yosys_share))
