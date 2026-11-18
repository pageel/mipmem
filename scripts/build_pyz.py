"""Deterministic zero-dependency zipapp bundler for mipmem."""

from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
from typing import Optional
import zipfile

FIXED_ZIP_TIMESTAMP: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)
REPO_ROOT = Path(__file__).resolve().parents[1]

_ZIPAPP_ENTRYPOINT = (
    '"""Deterministic zipapp entrypoint for mipmem."""\n\n'
    "import sys\n"
    "import time\n\n\n"
    "def _run() -> int:\n"
    "    boot_started = time.perf_counter()\n"
    "    from mipmem.cli import main\n\n"
    "    return main(sys.argv[1:], boot_started=boot_started)\n\n\n"
    "if __name__ == '__main__':\n"
    "    raise SystemExit(_run())\n"
)


def _add_zip_bytes(zf: zipfile.ZipFile, arcname: str, payload: bytes) -> None:
    """Write one archive member with deterministic timestamp and permissions."""
    info = zipfile.ZipInfo(filename=arcname, date_time=FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    zf.writestr(info, payload)


# @para-doc [#csa-build-pyz-bundler]
def build_zipapp(
    src_dir: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> Path:
    """Package src/mipmem/*.py deterministically into a runnable .pyz zipapp archive."""
    resolved_src = (src_dir if src_dir is not None else REPO_ROOT / "src").resolve()
    pkg_dir = resolved_src / "mipmem"
    if not pkg_dir.is_dir():
        raise FileNotFoundError(f"Package directory not found: {pkg_dir}")

    resolved_out = (
        output_path if output_path is not None else REPO_ROOT / "bin" / "mipmem.pyz"
    ).resolve()
    resolved_out.parent.mkdir(parents=True, exist_ok=True)

    py_files: list[Path] = []
    for candidate in sorted(pkg_dir.rglob("*.py")):
        rel_parts = candidate.relative_to(resolved_src).parts
        if "__pycache__" in rel_parts or any(part.startswith(".") for part in rel_parts):
            continue
        if candidate.is_file():
            py_files.append(candidate)

    buffer = io.BytesIO()
    buffer.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as zf:
        _add_zip_bytes(zf, "__main__.py", _ZIPAPP_ENTRYPOINT.encode("utf-8"))
        for py_file in py_files:
            arcname = py_file.relative_to(resolved_src).as_posix()
            normalized_bytes = py_file.read_text(encoding="utf-8").replace("\r\n", "\n").encode(
                "utf-8"
            )
            _add_zip_bytes(zf, arcname, normalized_bytes)

    fd, tmp_name = tempfile.mkstemp(dir=str(resolved_out.parent), prefix=".tmp_pyz_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(buffer.getvalue())
        try:
            os.chmod(tmp_name, 0o755)
        except OSError:
            pass
        os.replace(tmp_name, resolved_out)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return resolved_out


if __name__ == "__main__":
    out_file = build_zipapp()
    print(f"BUILT_PYZ={out_file}")
