"""Clean-room static scanner preventing personal data, secrets, and DB leaks."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
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

_ARCHIVE_SUFFIXES: tuple[str, ...] = (".pyz", ".zip", ".whl", ".tar.gz", ".tgz")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_COMMIT_LINE_RE = re.compile(r"^commit ([0-9a-fA-F]{7,64})\b")
_BINARY_DIFF_RE = re.compile(r"^Binary files .+ and b/(.+?) differ$")

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


def _is_archive_path(path: Path) -> bool:
    """Return True if path matches any supported archive or package suffix."""
    lower = path.name.lower()
    return any(lower.endswith(suffix) for suffix in _ARCHIVE_SUFFIXES)


def _scan_archive_member(
    *,
    member: str,
    raw_bytes: Optional[bytes],
    rel_posix: str,
    home_literals: Sequence[str],
    current_user: str,
) -> list[str]:
    """Audit a single archive entry's path and content for bytecode, DBs, or secrets."""
    findings: list[str] = []
    parts = Path(member).parts
    if "__pycache__" in parts or member.lower().endswith((".pyc", ".pyo")):
        findings.append(f"{rel_posix}:{member}: archive-bytecode-disallowed")
        return findings
    if _is_forbidden_filename(Path(member).name):
        findings.append(f"{rel_posix}:{member}: archive-forbidden-file")
        return findings

    findings.extend(
        _scan_text_content(
            member,
            rel_label=f"{rel_posix}:{member}",
            allow_tbd=True,
            home_literals=home_literals,
            current_user=current_user,
        )
    )
    if raw_bytes is None or b"\x00" in raw_bytes[:1024]:
        return findings

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
    return findings


def _scan_archive(
    archive_path: Path,
    *,
    rel_posix: str,
    home_literals: Sequence[str],
    current_user: str,
) -> list[str]:
    """Inspect .pyz, .zip, .whl, or .tar.gz archive entries for leaks or bytecode."""
    findings: list[str] = []
    lower_name = archive_path.name.lower()
    if lower_name.endswith((".tar.gz", ".tgz")):
        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                for member_info in tf.getmembers():
                    raw_bytes: Optional[bytes] = None
                    if member_info.isfile():
                        extracted = tf.extractfile(member_info)
                        if extracted is not None:
                            raw_bytes = extracted.read()
                    findings.extend(
                        _scan_archive_member(
                            member=member_info.name,
                            raw_bytes=raw_bytes,
                            rel_posix=rel_posix,
                            home_literals=home_literals,
                            current_user=current_user,
                        )
                    )
        except (tarfile.TarError, EOFError, OSError):
            findings.append(f"{rel_posix}: corrupt-tar-archive")
        return findings

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                raw_bytes = None if info.is_dir() else zf.read(info.filename)
                findings.extend(
                    _scan_archive_member(
                        member=info.filename,
                        raw_bytes=raw_bytes,
                        rel_posix=rel_posix,
                        home_literals=home_literals,
                        current_user=current_user,
                    )
                )
    except zipfile.BadZipFile:
        findings.append(f"{rel_posix}: corrupt-zipapp-archive")
    return findings


# @para-doc [#csa-audit-git-history-release]
def scan_git_history(repo_root: Path) -> list[str]:
    """Audit full Git commit diff history for historical secrets, personal paths, or DBs."""
    root = repo_root.resolve()
    if not (root / ".git").exists():
        return [f"{repo_root}: git-history-unavailable"]

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(
        ["git", "-C", str(root), "log", "--all", "-p", "--no-color"],
        env=env,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err_detail = proc.stderr.decode("utf-8", errors="replace").strip()
        return [f"{repo_root}: git-log-failed ({err_detail})"]

    home_literals = _build_forbidden_home_literals()
    try:
        current_user = getpass.getuser()
    except Exception:
        current_user = ""

    diff_text = proc.stdout.decode("utf-8", errors="replace")
    findings: list[str] = []
    commit_sha = "unknown"
    current_file = ""
    in_hunk = False

    for line in diff_text.splitlines():
        commit_match = _COMMIT_LINE_RE.match(line)
        if commit_match and not in_hunk:
            commit_sha = commit_match.group(1)[:8]
            current_file = ""
            continue

        diff_match = _DIFF_GIT_RE.match(line)
        if diff_match:
            in_hunk = False
            current_file = diff_match.group(2)
            continue

        if line.startswith("+++ "):
            target = line[4:].strip()
            if target.startswith("b/"):
                current_file = target[2:]
                rel_label = f"git-history:{commit_sha}:{current_file}"
                if _is_forbidden_filename(Path(current_file).name):
                    findings.append(f"{rel_label}: forbidden-file")
                findings.extend(
                    _scan_text_content(
                        current_file,
                        rel_label=rel_label,
                        allow_tbd=True,
                        home_literals=home_literals,
                        current_user=current_user,
                    )
                )
            continue

        binary_match = _BINARY_DIFF_RE.match(line)
        if binary_match:
            current_file = binary_match.group(1)
            rel_label = f"git-history:{commit_sha}:{current_file}"
            if _is_forbidden_filename(Path(current_file).name):
                findings.append(f"{rel_label}: forbidden-file")
            findings.extend(
                _scan_text_content(
                    current_file,
                    rel_label=rel_label,
                    allow_tbd=True,
                    home_literals=home_literals,
                    current_user=current_user,
                )
            )
            continue

        if line.startswith("@@ "):
            in_hunk = True
            continue

        if line.startswith("--- ") or line.startswith("index "):
            continue

        if in_hunk and line.startswith("+"):
            added_line = line[1:]
            if not current_file:
                continue
            rel_label = f"git-history:{commit_sha}:{current_file}"
            findings.extend(
                _scan_text_content(
                    added_line,
                    rel_label=rel_label,
                    allow_tbd=(current_file == "config.example.toml"),
                    home_literals=home_literals,
                    current_user=current_user,
                )
            )

    return findings


# @para-doc [#csa-audit-cleanroom-scanner]
def scan_cleanroom(
    repo_root: Path,
    *,
    include_git_history: bool = False,
    dist_dir: Optional[Path] = None,
) -> list[str]:
    """Scan a repository tree, optional distribution artifacts, and optional git history."""
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

        if _is_archive_path(path):
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

    if dist_dir is not None:
        dist_root = dist_dir.resolve()
        if not dist_root.is_dir():
            findings.append(f"{dist_dir}: dist-directory-missing")
        else:
            for path in sorted(dist_root.rglob("*")):
                if path.is_dir():
                    continue
                rel_posix = f"dist/{path.relative_to(dist_root).as_posix()}"
                if _is_forbidden_filename(path.name):
                    findings.append(f"{rel_posix}: forbidden-file")
                elif _is_archive_path(path):
                    findings.extend(
                        _scan_archive(
                            path,
                            rel_posix=rel_posix,
                            home_literals=home_literals,
                            current_user=current_user,
                        )
                    )

    if include_git_history:
        findings.extend(scan_git_history(root))

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
    parser.add_argument(
        "--git-history",
        action="store_true",
        help="Also scan full Git commit diff history via git log --all -p.",
    )
    parser.add_argument(
        "--dist-dir",
        default=None,
        help="Optional directory of distribution artifacts (.pyz, .whl, .tar.gz) to scan.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    dist_arg = Path(args.dist_dir) if args.dist_dir else None
    violations = scan_cleanroom(
        Path(args.repo_root),
        include_git_history=bool(args.git_history),
        dist_dir=dist_arg,
    )
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
