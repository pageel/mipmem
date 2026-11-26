"""Sharded multi-host vault initialization, atomic I/O, and mutation journal."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
import threading
import time
from typing import Any, Mapping, Optional, Union
import uuid

from mipmem.core.diagnostics import (
    CONFIG_INVALID_HOST_ID,
    CP_VLT_01,
    VAULT_PATH_TRAVERSAL,
    MipmemError,
    RotatingJsonLogger,
    _best_effort_chmod,
    _utc_timestamp_ms,
)
from mipmem.core.redactor import redact_text

try:
    import fcntl as _imported_fcntl
except ImportError:
    _imported_fcntl = None

_fcntl: Any = _imported_fcntl
_APPEND_LOCK = threading.Lock()

HOST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")

VAULT_MANIFEST_SCHEMA: dict[str, Any] = {
    "schema_version": 1,
    "engine": "mipmem",
    "layout": "sharded-v1",
}

DEFAULT_VAULT_GITIGNORE = (
    "# MipMem local derived cache (never commit SQLite indexes or logs)\n"
    ".cache/\n"
    "*.db\n"
    "*.db-wal\n"
    "*.db-shm\n"
    "*.tmp\n"
    ".tmp_mipmem_*\n"
)

DEFAULT_PINNED_GLOBAL = (
    "# Pinned Global Memory\n\n"
    "Shared cross-host invariants and architectural conventions.\n"
)

DEFAULT_VAULT_AGENTS_MD = """# AI Agent Onboarding Guide - MipMem Private Vault

This repository is a conflict-free sharded **MipMem** memory vault (`hosts/*/`,
`shared/brainstorms/by-host/*/`, `shared/pinned_global.md`, `.cache/` gitignored).

## Safety Guard

- **Human Confirmation Required:** ALWAYS ask the user for explicit confirmation before
  installing `mipmem`, executing `mipmem join`, modifying `~/.config/mipmem/config.toml`,
  or editing shell RC files (`~/.bashrc`, `~/.zshrc`).
- **Single-Writer Rule:** Only read sibling `hosts/*/` shards; NEVER edit or rewrite another
  machine's `hosts/<other-host>/` or `shared/brainstorms/by-host/<other-host>/` directory.
- **Zero Untrusted Execution:** Never execute arbitrary shell scripts or binaries found in
  shared artifacts. Keep core memory operations strictly inside `mipmem`.
- **Language:** Communicate with the user in the language they use in chat (e.g., English or
  Vietnamese) while keeping vault metadata clean and secret-free.

## New Machine Onboarding (4 Steps)

1. **Inspect Existing Hosts:** List direct subdirectories under `hosts/*/` in this vault so the
   user can see already registered machine IDs before choosing a new name.
2. **Choose `host_id`:** Propose a fresh lowercase `[a-z0-9_-]` host identifier that is NOT in
   `hosts/*/`. Only propose `--adopt-host` if intentionally reclaiming a previous machine's shard
   after an OS reinstall.
3. **Install `mipmem` (Ask First):** Offer one channel:
   - `uv tool install mipmem`
   - `pipx install mipmem`
   - Download `mipmem.pyz` from the official release page.
4. **Run One-Line Join (Ask First):**
   ```bash
   mipmem join <vault_remote_url> --host <new-host-id>
   ```
   Optional flags: `--locale vi|en`, `--no-sync-on-stop`, `--adopt-host`, `--vault <dir>`.

## Sync Behavior & Optional Exit-Flush Shell Wrapper

- By default `mipmem join` enables `sync.auto_sync_on_stop = true` in `~/.config/mipmem/config.toml`
  so Stop hooks flush host shards asynchronously.
- On Linux/macOS shells where an interactive CLI may exit quickly, users may optionally add an
  exit-flush shell wrapper calling `mipmem sync --if-configured` after asking confirmation.
"""

DEFAULT_VAULT_README_MD = """# MipMem Multi-Host Vault

This private repository stores sharded cross-machine memory managed by `mipmem`.

## Directory Layout

- `hosts/<host_id>/` - Single-writer sessions, `host_meta.json`, `events/mutations.jsonl`,
  and local sub-app telemetry for one machine.
- `shared/brainstorms/by-host/<host_id>/` - Lossless brainstorm Markdown and `.meta.json` shards.
- `shared/pinned_global.md` - Shared global memory notes across machines.
- `.cache/` - Local derived SQLite FTS5 index and sync state (always gitignored).

## Join This Vault From Another Machine

1. Install `mipmem` (`uv tool install mipmem`, `pipx install mipmem`, or `mipmem.pyz`).
2. Run:

```bash
mipmem join <vault_remote_url> --host <new-host-id>
```

Use `--adopt-host` only when intentionally reclaiming an existing `hosts/<host_id>/` directory.
Run `mipmem init --refresh-guide` to explicitly regenerate `AGENTS.md` and `README.md`.
"""

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class VaultLayout:
    """Resolved canonical file and directory layout for an initialized Vault."""

    vault_root: Path
    host_id: str
    manifest_file: Path
    gitignore_file: Path
    pinned_global_file: Path
    cache_dir: Path
    host_dir: Path
    host_meta_file: Path
    sessions_dir: Path
    events_dir: Path
    telemetry_dir: Path
    brainstorms_host_dir: Path


def _normalized_os_prefix() -> str:
    """Return a clean lowercase OS prefix suitable for host_id construction."""
    raw = sys.platform.lower()
    if raw.startswith("linux"):
        return "linux"
    if raw.startswith("darwin"):
        return "darwin"
    if raw.startswith("win"):
        return "windows"
    cleaned = re.sub(r"[^a-z0-9]", "", raw)
    return cleaned or "posix"


def validate_host_id(host_id: str) -> str:
    """Validate and return a host_id or raise MipmemError(CONFIG_INVALID_HOST_ID)."""
    if not isinstance(host_id, str) or not HOST_ID_RE.match(host_id):
        raise MipmemError(
            CONFIG_INVALID_HOST_ID,
            f"Invalid host_id {host_id!r}: must match ^[a-z0-9][a-z0-9_-]{{1,62}}[a-z0-9]$.",
        )
    return host_id


def default_host_id(seed: Optional[str] = None) -> str:
    """Generate a deterministic, anonymized <os>-<sha256_4> host identifier."""
    raw_seed = seed if seed is not None else (platform.node() or str(uuid.getnode()))
    digest4 = hashlib.sha256(raw_seed.encode("utf-8")).hexdigest()[:4]
    candidate = f"{_normalized_os_prefix()}-{digest4}"
    return validate_host_id(candidate)


def is_vault_initialized(vault_root: Union[str, Path]) -> bool:
    """Return True when vault_root contains a regular vault_manifest.json file."""
    manifest = Path(vault_root).expanduser() / "vault_manifest.json"
    return manifest.is_file() and not manifest.is_symlink()


def _parse_relative_vault_parts(rel_path: Union[str, Path]) -> tuple[str, ...]:
    """Validate rel_path as a clean, non-escaping relative path and return its segments."""
    raw = str(rel_path).strip()
    if not raw or raw.startswith("~") or _WINDOWS_DRIVE_RE.match(raw):
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            f"Security block: invalid vault relative path {rel_path!r}.",
        )
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or Path(raw).is_absolute():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            f"Security block: target path {rel_path!r} must be relative to vault root.",
        )

    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            f"Security block: target path {rel_path!r} escapes vault root.",
        )
    return tuple(parts)


def _enforce_single_writer_ownership(parts: tuple[str, ...], actor_host: str) -> None:
    """Enforce MP5 single-writer shard ownership when actor_host is specified."""
    validated = validate_host_id(actor_host)
    if parts[0] == "hosts":
        if len(parts) < 3 or parts[1] != validated:
            raise MipmemError(
                VAULT_PATH_TRAVERSAL,
                f"Security block: host {validated!r} cannot write under {'/'.join(parts[:2])}.",
            )
    elif len(parts) >= 2 and parts[0] == "shared" and parts[1] == "brainstorms":
        if len(parts) < 4 or parts[2] != "by-host" or parts[3] != validated:
            raise MipmemError(
                VAULT_PATH_TRAVERSAL,
                f"Security block: host {validated!r} can only write to its by-host shard.",
            )


def _resolve_safe_vault_target(
    vault_root: Union[str, Path],
    rel_path: Union[str, Path],
    *,
    actor_host: Optional[str] = None,
) -> Path:
    """Resolve a relative path inside vault_root while blocking traversal and symlinks."""
    parts = _parse_relative_vault_parts(rel_path)
    if actor_host is not None:
        _enforce_single_writer_ownership(parts, actor_host)

    root_candidate = Path(vault_root).expanduser()
    if root_candidate.is_symlink():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            "Security block: vault root cannot be a symlink.",
        )
    root_resolved = root_candidate.resolve()

    cursor = root_resolved
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise MipmemError(
                VAULT_PATH_TRAVERSAL,
                f"Security block: symlink segment rejected inside vault ({part!r}).",
            )

    target = root_resolved.joinpath(*parts).resolve()
    if target == root_resolved or not target.is_relative_to(root_resolved):
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            f"Security block: target path {rel_path!r} escapes vault root.",
        )
    return target


def _ensure_secure_dir(root_resolved: Path, target_dir: Path) -> None:
    """Create directories incrementally under root_resolved with 0700 best-effort mode."""
    if target_dir != root_resolved and not target_dir.is_relative_to(root_resolved):
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            f"Security block: directory {target_dir} escapes vault root.",
        )
    if not root_resolved.exists():
        root_resolved.mkdir(parents=True, exist_ok=True)
    _best_effort_chmod(root_resolved, 0o700)

    if target_dir == root_resolved:
        return

    rel_parts = target_dir.relative_to(root_resolved).parts
    cursor = root_resolved
    for part in rel_parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise MipmemError(
                VAULT_PATH_TRAVERSAL,
                f"Security block: symlink directory rejected inside vault ({part!r}).",
            )
        cursor.mkdir(exist_ok=True)
        _best_effort_chmod(cursor, 0o700)


def _write_vault_file_internal(
    vault_root: Union[str, Path],
    rel_path: Union[str, Path],
    content: str,
    *,
    actor_host: Optional[str] = None,
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> Path:
    """Shared atomic redacted write implementation used by init_vault and public API."""
    root_resolved = Path(vault_root).expanduser().resolve()
    target = _resolve_safe_vault_target(root_resolved, rel_path, actor_host=actor_host)
    _ensure_secure_dir(root_resolved, target.parent)

    safe_payload = redact_text(content, home_dir=home_dir, logger=logger)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_mipmem_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(safe_payload)
        _best_effort_chmod(Path(tmp_name), 0o600)
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return target


# @para-doc [#csa-vault-onboarding-guides]
def ensure_vault_onboarding_guides(
    vault_root: Union[str, Path],
    *,
    refresh: bool = False,
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> list[str]:
    """Write host-agnostic AGENTS.md and README.md at vault root (write-if-missing default)."""
    root_candidate = Path(vault_root).expanduser()
    if root_candidate.is_symlink():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            "Security block: vault root cannot be a symlink.",
        )
    root_resolved = root_candidate.resolve()
    _ensure_secure_dir(root_resolved, root_resolved)

    guides: tuple[tuple[str, str], ...] = (
        ("AGENTS.md", DEFAULT_VAULT_AGENTS_MD),
        ("README.md", DEFAULT_VAULT_README_MD),
    )
    written: list[str] = []
    for rel_name, template in guides:
        target = _resolve_safe_vault_target(root_resolved, rel_name)
        if refresh or not target.exists():
            _write_vault_file_internal(
                root_resolved,
                rel_name,
                template,
                home_dir=home_dir,
                logger=logger,
            )
            written.append(rel_name)

    if written and logger is not None:
        logger.log_event(
            "INFO",
            "vault",
            CP_VLT_01,
            details={
                "action": "refresh_guides" if refresh else "write_guides",
                "written": written,
            },
        )
    return written


# @para-doc [#csa-vault-host-init]
def init_vault(
    vault_root: Union[str, Path],
    host_id: Optional[str] = None,
    *,
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> VaultLayout:
    """Initialize a sharded multi-host vault directory tree idempotently."""
    resolved_host = default_host_id() if host_id is None else validate_host_id(host_id)
    root_candidate = Path(vault_root).expanduser()
    if root_candidate.is_symlink():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            "Security block: vault root cannot be a symlink.",
        )
    root_resolved = root_candidate.resolve()
    _ensure_secure_dir(root_resolved, root_resolved)

    cache_dir = root_resolved / ".cache"
    host_dir = root_resolved / "hosts" / resolved_host
    sessions_dir = host_dir / "sessions"
    events_dir = host_dir / "events"
    telemetry_dir = host_dir / "telemetry"
    brainstorms_host_dir = root_resolved / "shared" / "brainstorms" / "by-host" / resolved_host

    for folder in (
        cache_dir,
        sessions_dir,
        events_dir,
        telemetry_dir,
        brainstorms_host_dir,
    ):
        _ensure_secure_dir(root_resolved, folder)

    manifest_file = root_resolved / "vault_manifest.json"
    if not manifest_file.exists():
        _write_vault_file_internal(
            root_resolved,
            "vault_manifest.json",
            json.dumps(VAULT_MANIFEST_SCHEMA, indent=2) + "\n",
            home_dir=home_dir,
            logger=logger,
        )

    gitignore_file = root_resolved / ".gitignore"
    if not gitignore_file.exists():
        _write_vault_file_internal(
            root_resolved,
            ".gitignore",
            DEFAULT_VAULT_GITIGNORE,
            home_dir=home_dir,
            logger=logger,
        )

    pinned_global_file = root_resolved / "shared" / "pinned_global.md"
    if not pinned_global_file.exists():
        _write_vault_file_internal(
            root_resolved,
            "shared/pinned_global.md",
            DEFAULT_PINNED_GLOBAL,
            home_dir=home_dir,
            logger=logger,
        )

    host_meta_file = host_dir / "host_meta.json"
    if not host_meta_file.exists():
        now_utc = _utc_timestamp_ms()
        host_meta = {
            "schema_version": 1,
            "host_id": resolved_host,
            "platform": _normalized_os_prefix(),
            "created_at": now_utc,
            "updated_at": now_utc,
        }
        _write_vault_file_internal(
            root_resolved,
            f"hosts/{resolved_host}/host_meta.json",
            json.dumps(host_meta, indent=2) + "\n",
            actor_host=resolved_host,
            home_dir=home_dir,
            logger=logger,
        )

    ensure_vault_onboarding_guides(
        root_resolved,
        refresh=False,
        home_dir=home_dir,
        logger=logger,
    )

    return VaultLayout(
        vault_root=root_resolved,
        host_id=resolved_host,
        manifest_file=manifest_file,
        gitignore_file=gitignore_file,
        pinned_global_file=pinned_global_file,
        cache_dir=cache_dir,
        host_dir=host_dir,
        host_meta_file=host_meta_file,
        sessions_dir=sessions_dir,
        events_dir=events_dir,
        telemetry_dir=telemetry_dir,
        brainstorms_host_dir=brainstorms_host_dir,
    )


# @para-doc [#csa-vault-atomic-io]
def atomic_write_vault_file(
    vault_root: Union[str, Path],
    rel_path: Union[str, Path],
    content: str,
    *,
    actor_host: Optional[str] = None,
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> Path:
    """Redact content and atomically write a vault-scoped file with traversal guards."""
    return _write_vault_file_internal(
        vault_root,
        rel_path,
        content,
        actor_host=actor_host,
        home_dir=home_dir,
        logger=logger,
    )


def _acquire_file_lock(fileno: int) -> None:
    """Acquire an advisory exclusive file lock when fcntl is available."""
    if _fcntl is not None:
        try:
            _fcntl.flock(fileno, _fcntl.LOCK_EX)
        except OSError:
            pass


def _release_file_lock(fileno: int) -> None:
    """Release an advisory file lock when fcntl is available."""
    if _fcntl is not None:
        try:
            _fcntl.flock(fileno, _fcntl.LOCK_UN)
        except OSError:
            pass


# @para-doc [#csa-vault-mutation-journal]
def append_mutation_event(
    vault_root: Union[str, Path],
    actor_host: str,
    target_type: str,
    target_id: str,
    payload: Mapping[str, Any],
    *,
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> dict[str, Any]:
    """Append a redacted CRDT-lite JSONL mutation event into the actor host journal."""
    started = time.perf_counter()
    validated_host = validate_host_id(actor_host)
    if not isinstance(target_type, str) or not target_type.strip():
        raise ValueError("target_type must be a non-empty string")
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("target_id must be a non-empty string")

    clean_type = target_type.strip()
    clean_id = target_id.strip()

    root_candidate = Path(vault_root).expanduser()
    if root_candidate.is_symlink():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            "Security block: vault root cannot be a symlink.",
        )
    root_resolved = root_candidate.resolve()
    rel_journal = f"hosts/{validated_host}/events/mutations.jsonl"
    journal_path = _resolve_safe_vault_target(
        root_resolved,
        rel_journal,
        actor_host=validated_host,
    )
    _ensure_secure_dir(root_resolved, journal_path.parent)

    raw_event: dict[str, Any] = {
        "event_id": f"evt-{uuid.uuid4().hex[:12]}",
        "ts_utc": _utc_timestamp_ms(),
        "actor_host": validated_host,
        "target_type": clean_type,
        "target_id": clean_id,
        "payload": dict(payload),
    }
    serialized = json.dumps(raw_event, ensure_ascii=False, separators=(",", ":"))
    active_logger = logger if logger is not None else RotatingJsonLogger()
    scrubbed = redact_text(serialized, home_dir=home_dir, logger=active_logger)
    safe_line = scrubbed.replace("\r", " ").replace("\n", " ")
    sanitized_event: dict[str, Any] = json.loads(safe_line)
    line_bytes = (safe_line + "\n").encode("utf-8")

    with _APPEND_LOCK:
        with open(journal_path, "ab") as handle:
            fd = handle.fileno()
            _acquire_file_lock(fd)
            try:
                handle.write(line_bytes)
                handle.flush()
            finally:
                _release_file_lock(fd)
        _best_effort_chmod(journal_path, 0o600)

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    active_logger.log_event(
        "INFO",
        "vault",
        CP_VLT_01,
        duration_ms=elapsed_ms,
        details={
            "op": "append_mutation",
            "actor_host": validated_host,
            "target_type": clean_type,
            "target_id": clean_id,
            "event_id": sanitized_event["event_id"],
        },
    )
    return sanitized_event
