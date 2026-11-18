"""Clean-room static scanner preventing personal data, secrets, and DB leaks."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import re
import sys
from typing import Optional, Sequence
import zipfile

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)

_FORBIDDEN_SUFFIXES: tuple[str, ...] = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
)

_ARCHIVE_SUFFIXES: frozenset[str] = frozenset({".pyz", ".zip"})

_NEUTRAL_USER_NAMES: frozenset[str] = frozenset(
    {
        "user",
        "example",
        "alice",
        "alicex",
        "bob",
        "runner",
        "ubuntu",
        "dev",
        "workstation",
    }
)

_TBD_MARKER = "[" + "TBD]"

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-pat", re.compile("ghp" + r"_[A-Za-z0-9]{20,}")),
    ("github-fine-pat", re.compile("github" + r"_pat_[A-Za-z0-9_]{20,}")),
    ("github-oauth", re.compile("gho" + r"_[A-Za-z0-9]{20,}")),
    ("openai-key", re.compile("sk" + r"-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("google-api-key", re.compile("AIza" + r"[0-9A-Za-z_-]{30,}")),
    ("aws-access-key", re.compile("AKIA" + r"[0-9A-Z]{16}")),
    (
        "jwt-token",
        re.compile("eyJ" + r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    ("pem-private-key", re.compile("-----BEGIN" + r" [A-Z0-9 ]*PRIVATE KEY-----")),
    ("uri-credentials", re.compile(r"://[A-Za-z0-9_.-]+:(?!\[REDACTED:)[^@\s/\"']+@")),
)

_UNIX_USER_PATH_RE = re.compile(r"/(?:home|Users|media)/([A-Za-z0-9._-]+)")
_WIN_USER_PATH_RE = re.compile(r"[A-Za-z]:\\Users\\([A-Za-z0-9._-]+)")


def _build_forbidden_home_literals() -> tuple[str, ...]:
    """Return runtime home and media paths that must never appear in clean-room code."""
    candidates: list[str] = []
    try:
        home_resolved = str(Path.home().resolve())
        if len(home_resolved) >= 4:
            candidates.append(home_resolved)
    except RuntimeError:
        pass

    try:
        user_name = getpass.getuser()
    except Exception:
        user_name = ""

    if user_name and user_name.lower() not in _NEUTRAL_USER_NAMES:
        candidates.append(f"/home/{user_name}")
        candidates.append(f"/media/{user_name}")
        candidates.append(f"/Users/{user_name}")

    # Preserve order while deduplicating.
    return tuple(dict.fromkeys(candidates))


def _is_forbidden_filename(name: str) -> bool:
    """Return True if a filename matches forbidden env, key, or SQLite patterns."""
    lower = name.lower()
    if lower == ".env" or (lower.startswith(".env.") and lower != ".env.example"):
        return True
    if lower.startswith("id_rsa") or lower.startswith("id_ed25519"):
        return True
    return lower.endswith(_FORBIDDEN_SUFFIXES)


def _scan_text_content(
    content: str,
    *,
    rel_label: str,
    allow_tbd: bool,
    home_literals: Sequence[str],
    current_user: str,
) -> list[str]:
    """Scan decoded text content for personal paths, secrets, and disallowed TBDs."""
    findings: list[str] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        for literal in home_literals:
            if literal in line:
                findings.append(f"{rel_label}:{line_no}: personal-home-path")

        for match in _UNIX_USER_PATH_RE.finditer(line):
            user_seg = match.group(1)
            lowered = user_seg.lower()
            if user_seg == current_user or lowered not in _NEUTRAL_USER_NAMES:
                findings.append(f"{rel_label}:{line_no}: unix-user-path ({user_seg})")

        for match in _WIN_USER_PATH_RE.finditer(line):
            user_seg = match.group(1)
            if user_seg == current_user or user_seg.lower() not in _NEUTRAL_USER_NAMES:
                findings.append(f"{rel_label}:{line_no}: windows-user-path ({user_seg})")

        for secret_label, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(f"{rel_label}:{line_no}: secret-pattern ({secret_label})")

        if not allow_tbd and _TBD_MARKER in line:
            findings.append(f"{rel_label}:{line_no}: disallowed-tbd-marker")

    return findings


def _scan_archive(
    archive_path: Path,
    *,
    rel_posix: str,
    home_literals: Sequence[str],
    current_user: str,
) -> list[str]:
    """Inspect ZIP/PYZ archive entries and decoded source files for leaks or bytecode."""
    findings: list[str] = []
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                member = info.filename
                parts = Path(member).parts
                if "__pycache__" in parts or member.lower().endswith((".pyc", ".pyo")):
                    findings.append(f"{rel_posix}:{member}: archive-bytecode-disallowed")
                    continue
                if _is_forbidden_filename(Path(member).name):
                    findings.append(f"{rel_posix}:{member}: archive-forbidden-file")
                    continue

                name_hits = _scan_text_content(
                    member,
                    rel_label=f"{rel_posix}:{member}",
                    allow_tbd=True,
                    home_literals=home_literals,
                    current_user=current_user,
                )
                findings.extend(name_hits)

                if info.is_dir():
                    continue
                raw_bytes = zf.read(member)
                if b"\x00" in raw_bytes[:1024]:
                    continue
                decoded = raw_bytes.decode("utf-8", errors="replace")
                findings.extend(
                    _scan_text_content(
                        decoded,
                        rel_label=f"{rel_posix}:{member}",
                        allow_tbd=False,
                        home_literals=home_literals,
                        current_user=current_user,
                    )
                )
    except zipfile.BadZipFile:
        findings.append(f"{rel_posix}: corrupt-zipapp-archive")
    return findings


# @para-doc [#csa-audit-cleanroom-scanner]
def scan_cleanroom(repo_root: Path) -> list[str]:
    """Scan a repository tree and return clean-room violation messages."""
    root = repo_root.resolve()
    if not root.exists():
        return [f"{repo_root}: root-directory-missing"]

    home_literals = _build_forbidden_home_literals()
    try:
        current_user = getpass.getuser()
    except Exception:
        current_user = ""

    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        if path.is_dir():
            continue

        rel_posix = path.relative_to(root).as_posix()
        if _is_forbidden_filename(path.name):
            findings.append(f"{rel_posix}: forbidden-file")
            continue

        if path.suffix.lower() in _ARCHIVE_SUFFIXES:
            findings.extend(
                _scan_archive(
                    path,
                    rel_posix=rel_posix,
                    home_literals=home_literals,
                    current_user=current_user,
                )
            )
            continue

        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            findings.append(f"{rel_posix}: unreadable-file ({exc})")
            continue

        if b"\x00" in raw_bytes[:1024]:
            continue

        text = raw_bytes.decode("utf-8", errors="replace")
        findings.extend(
            _scan_text_content(
                text,
                rel_label=rel_posix,
                allow_tbd=(rel_posix == "config.example.toml"),
                home_literals=home_literals,
                current_user=current_user,
            )
        )

    return findings


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint returning zero when no clean-room violations are found."""
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        prog="check_cleanroom",
        description="Verify clean-room integrity of a mipmem repository.",
    )
    parser.add_argument(
        "repo_root",
        nargs="?",
        default=str(default_root),
        help="Path to the clean-room repository root.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    violations = scan_cleanroom(Path(args.repo_root))
    if violations:
        sys.stderr.write(
            f"CLEANROOM_VIOLATION: {len(violations)} violation(s) detected:\n"
        )
        for item in violations:
            sys.stderr.write(f"  - {item}\n")
        return 1
    sys.stdout.write(f"CLEANROOM_OK: 0 violations in {Path(args.repo_root).name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
