"""Module entrypoint for `python3 -m mipmem` and `.pyz` zipapp execution."""

from __future__ import annotations

import sys
import time


def _bootstrap() -> int:
    boot_started = time.perf_counter()
    from mipmem.cli import main

    return main(sys.argv[1:], boot_started=boot_started)


if __name__ == "__main__":
    raise SystemExit(_bootstrap())
