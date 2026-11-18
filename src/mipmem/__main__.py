"""Module entrypoint for `python3 -m mipmem` and `.pyz` zipapp execution."""

from __future__ import annotations

import sys


def _bootstrap() -> int:
    from mipmem import __version__

    sys.stdout.write(f"mipmem {__version__}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_bootstrap())
