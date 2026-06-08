from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def _resource_root() -> Path:
    """Return the bundled resource root in source and PyInstaller-frozen runs."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parents[1]


def _prepend_path(path: Path) -> None:
    if path.exists():
        os.environ["PATH"] = f"{path}{os.pathsep}{os.environ.get('PATH', '')}"


def main() -> None:
    root = _resource_root()

    src_dir = root / "src"
    if src_dir.exists():
        sys.path.insert(0, str(src_dir))

    _prepend_path(root / "bin")

    abc_resources = root / "abc_resources"
    genlib = abc_resources / "my.genlib"
    abc_rc = abc_resources / "abc.rc"
    if genlib.exists():
        os.environ.setdefault("GENLIB_PATH", str(genlib))
    if abc_rc.exists():
        os.environ.setdefault("ABC_RC_PATH", str(abc_rc))

    bundled_bin = root / "bin"
    yosys_abc = bundled_bin / "yosys-abc"
    abc = bundled_bin / "abc"
    if yosys_abc.exists():
        os.environ.setdefault("ABC_CEC_BIN", str(yosys_abc))
    if abc.exists():
        os.environ.setdefault("ABC_BIN", str(abc))

    os.environ.setdefault("TMPDIR", tempfile.gettempdir())

    from contest_agent import main as contest_main

    contest_main()


if __name__ == "__main__":
    main()
