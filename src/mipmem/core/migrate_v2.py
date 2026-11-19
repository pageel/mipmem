"""Idempotent, non-destructive migrator from legacy v2 flat memory directories."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Optional, Union

from mipmem.core.diagnostics import (
    CP_VLT_01,
    MIGRATE_SOURCE_MISSING,
    VAULT_PATH_TRAVERSAL,
    MipmemError,
    RotatingJsonLogger,
    _utc_timestamp_ms,
)
from mipmem.core.redactor import redact_text, scrub_without_logging
from mipmem.core.vault import (
    append_mutation_event,
    atomic_write_vault_file,
    default_host_id,
    init_vault,
    validate_host_id,
)

_MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")
_MONTH_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})(?:$|[-_])")
_TITLE_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")
_STATUS_RE = re.compile(
    r"(?im)^(?:\s*>?\s*\*{0,2}status\*{0,2}\s*:\*{0,2}\s*)"
    r"(open|decided|superseded|archived)\b"
)


@dataclass(frozen=True)
class _SourceEntry:
    """Internal representation of one discovered v2 markdown artifact."""

    category: str
    source_rel: str
    source_path: Path
    target_rel: str
    meta_rel: Optional[str] = None


@dataclass(frozen=True)
class MigrationReport:
    """Summary returned by dry-run or apply migration execution."""

    dry_run: bool
    host_id: str
    source_dir: str
    vault_dir: str
    sessions_found: int = 0
    brainstorms_found: int = 0
    pinned_found: bool = False
    sessions_copied: int = 0
    brainstorms_copied: int = 0
    pinned_copied: bool = False
    skipped_existing: int = 0
    conflicts_skipped: int = 0
    planned_actions: list[dict[str, str]] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize report into a JSON-compatible dictionary."""
        return asdict(self)


def _format_utc_ms(epoch_seconds: float) -> str:
    """Format a POSIX timestamp into ISO-8601 UTC millisecond Z notation."""
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"


def _validate_source_root(source_dir: Union[str, Path]) -> Path:
    """Validate and resolve the legacy v2 root directory without following symlinks."""
    candidate = Path(source_dir).expanduser()
    if candidate.is_symlink():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            f"Security block: source directory cannot be a symlink ({source_dir}).",
        )
    if not candidate.exists() or not candidate.is_dir():
        raise MipmemError(
            MIGRATE_SOURCE_MISSING,
            f"Source v2 memory directory not found: {source_dir}.",
        )
    return candidate.resolve()


def _assert_safe_source_item(src_root: Path, item: Path) -> Path:
    """Ensure every segment from src_root to item is symlink-free and inside src_root."""
    rel = item.relative_to(src_root)
    cursor = src_root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise MipmemError(
                VAULT_PATH_TRAVERSAL,
                f"Security block: symlink rejected inside source tree ({rel.as_posix()}).",
            )
    resolved = item.resolve()
    if not resolved.is_relative_to(src_root):
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            f"Security block: source item escapes root directory ({rel.as_posix()}).",
        )
    return resolved


def _session_target_rel(host_id: str, sessions_root: Path, src_file: Path) -> str:
    """Map one legacy session markdown file into hosts/<host_id>/sessions/<YYYY-MM>/."""
    rel = src_file.relative_to(sessions_root)
    parts = rel.parts
    if len(parts) >= 2 and _MONTH_DIR_RE.match(parts[0]):
        month = parts[0]
        rest = parts[1:]
    else:
        match = _MONTH_PREFIX_RE.match(src_file.stem)
        if match:
            month = match.group(1)
        else:
            month = datetime.fromtimestamp(src_file.stat().st_mtime, tz=timezone.utc).strftime(
                "%Y-%m"
            )
        rest = parts

    safe_name = "__".join(rest)
    return f"hosts/{host_id}/sessions/{month}/{safe_name}"


def _brainstorm_target_rels(
    host_id: str,
    brainstorms_root: Path,
    src_file: Path,
) -> tuple[str, str]:
    """Map one legacy brainstorm note and its .meta.json sidecar into shared/by-host."""
    rel = src_file.relative_to(brainstorms_root)
    flat_name = "__".join(rel.parts)
    base_dir = f"shared/brainstorms/by-host/{host_id}"
    md_rel = f"{base_dir}/{flat_name}"
    meta_rel = f"{base_dir}/{Path(flat_name).stem}.meta.json"
    return md_rel, meta_rel


def _collect_source_entries(
    src_root: Path,
    host_id: str,
) -> tuple[int, int, bool, list[_SourceEntry]]:
    """Discover sessions, brainstorms, and pinned notes safely inside src_root."""
    entries: list[_SourceEntry] = []
    sessions_count = 0
    brainstorms_count = 0
    pinned_found = False

    sessions_dir = src_root / "sessions"
    if sessions_dir.exists() or sessions_dir.is_symlink():
        _assert_safe_source_item(src_root, sessions_dir)
        if sessions_dir.is_dir():
            for md_file in sorted(sessions_dir.rglob("*.md")):
                _assert_safe_source_item(src_root, md_file)
                if md_file.is_file():
                    sessions_count += 1
                    entries.append(
                        _SourceEntry(
                            category="session",
                            source_rel=md_file.relative_to(src_root).as_posix(),
                            source_path=md_file,
                            target_rel=_session_target_rel(host_id, sessions_dir, md_file),
                        )
                    )

    brainstorms_dir = src_root / "brainstorms"
    if brainstorms_dir.exists() or brainstorms_dir.is_symlink():
        _assert_safe_source_item(src_root, brainstorms_dir)
        if brainstorms_dir.is_dir():
            for md_file in sorted(brainstorms_dir.rglob("*.md")):
                _assert_safe_source_item(src_root, md_file)
                if md_file.is_file():
                    brainstorms_count += 1
                    md_rel, meta_rel = _brainstorm_target_rels(host_id, brainstorms_dir, md_file)
                    entries.append(
                        _SourceEntry(
                            category="brainstorm",
                            source_rel=md_file.relative_to(src_root).as_posix(),
                            source_path=md_file,
                            target_rel=md_rel,
                            meta_rel=meta_rel,
                        )
                    )

    pinned_file = src_root / "pinned_notes.md"
    if pinned_file.exists() or pinned_file.is_symlink():
        _assert_safe_source_item(src_root, pinned_file)
        if pinned_file.is_file():
            pinned_found = True
            entries.append(
                _SourceEntry(
                    category="pinned",
                    source_rel="pinned_notes.md",
                    source_path=pinned_file,
                    target_rel=f"hosts/{host_id}/pinned_host.md",
                )
            )

    return sessions_count, brainstorms_count, pinned_found, entries


def _build_brainstorm_meta(
    entry: _SourceEntry,
    redacted_text: str,
    sha256_hex: str,
    host_id: str,
) -> dict[str, Any]:
    """Build the canonical 9-field brainstorm .meta.json sidecar payload."""
    stem = entry.source_path.stem
    bs_id = re.sub(r"[^A-Za-z0-9_-]", "-", stem).strip("-") or "brainstorm"
    title_match = _TITLE_RE.search(redacted_text)
    title = title_match.group(1).strip() if title_match else stem
    status_match = _STATUS_RE.search(redacted_text)
    status = status_match.group(1).lower() if status_match else "open"

    return {
        "schema_version": 1,
        "bs_id": bs_id,
        "title": title,
        "origin_host": host_id,
        "status": status,
        "line_count": len(redacted_text.splitlines()),
        "sha256": sha256_hex,
        "created_at": _format_utc_ms(entry.source_path.stat().st_mtime),
        "updated_at": _utc_timestamp_ms(),
    }


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    """Read an existing host migration manifest without touching disk."""
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return {"schema_version": 1, "entries": {}}
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "entries": {}}
    if not isinstance(loaded, dict) or not isinstance(loaded.get("entries"), dict):
        return {"schema_version": 1, "entries": {}}
    return loaded


# @para-doc [#csa-migrate-v2-importer]
def migrate_from_v2(
    source_dir: Union[str, Path],
    vault_root: Union[str, Path],
    *,
    host_id: Optional[str] = None,
    dry_run: bool = True,
    force: bool = False,
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> MigrationReport:
    """Safely migrate legacy v2 sessions, brainstorms, and pinned notes into a sharded vault."""
    started = time.perf_counter()
    resolved_host = default_host_id() if host_id is None else validate_host_id(host_id)
    src_root = _validate_source_root(source_dir)
    vault_path = Path(vault_root).expanduser()
    if vault_path.is_symlink():
        raise MipmemError(
            VAULT_PATH_TRAVERSAL,
            "Security block: vault root cannot be a symlink.",
        )

    sessions_found, brainstorms_found, pinned_found, entries = _collect_source_entries(
        src_root,
        resolved_host,
    )

    active_logger = logger if logger is not None else RotatingJsonLogger()
    manifest_rel = f"hosts/{resolved_host}/migrated_v2_manifest.json"
    manifest_data = _load_manifest(vault_path / manifest_rel)
    manifest_entries: dict[str, dict[str, str]] = dict(manifest_data.get("entries", {}))

    planned_actions: list[dict[str, str]] = []
    to_write: list[tuple[_SourceEntry, str, str, str]] = []
    skipped_existing = 0
    conflicts_skipped = 0

    for entry in entries:
        raw_text = entry.source_path.read_text(encoding="utf-8")
        preview_text = scrub_without_logging(raw_text, home_dir=home_dir)
        sha256_hex = hashlib.sha256(preview_text.encode("utf-8")).hexdigest()

        target_file = vault_path / entry.target_rel
        existing_sha: Optional[str] = None
        if target_file.is_file() and not target_file.is_symlink():
            existing_sha = hashlib.sha256(target_file.read_bytes()).hexdigest()

        recorded = manifest_entries.get(entry.source_rel)
        recorded_match = isinstance(recorded, dict) and recorded.get("sha256") == sha256_hex

        if existing_sha == sha256_hex and (recorded_match or existing_sha is not None):
            skipped_existing += 1
            planned_actions.append(
                {
                    "source": entry.source_rel,
                    "target": entry.target_rel,
                    "action": "SKIPPED_ALREADY_MIGRATED",
                }
            )
            continue

        if existing_sha is not None and not force:
            conflicts_skipped += 1
            planned_actions.append(
                {
                    "source": entry.source_rel,
                    "target": entry.target_rel,
                    "action": "CONFLICT_SKIPPED",
                }
            )
            continue

        if dry_run:
            planned_actions.append(
                {
                    "source": entry.source_rel,
                    "target": entry.target_rel,
                    "action": "WOULD_OVERWRITE" if existing_sha is not None else "WOULD_COPY",
                }
            )
            continue

        action_label = "OVERWRITTEN" if existing_sha is not None else "COPIED"
        to_write.append((entry, raw_text, sha256_hex, action_label))

    sessions_copied = 0
    brainstorms_copied = 0
    pinned_copied = False

    if not dry_run:
        init_vault(vault_path, host_id=resolved_host, home_dir=home_dir, logger=active_logger)
        for entry, raw_text, sha256_hex, action_label in to_write:
            scrubbed = redact_text(raw_text, home_dir=home_dir, logger=active_logger)
            atomic_write_vault_file(
                vault_path,
                entry.target_rel,
                scrubbed,
                actor_host=resolved_host,
                home_dir=home_dir,
                logger=active_logger,
            )
            if entry.category == "brainstorm" and entry.meta_rel is not None:
                meta_payload = _build_brainstorm_meta(entry, scrubbed, sha256_hex, resolved_host)
                atomic_write_vault_file(
                    vault_path,
                    entry.meta_rel,
                    json.dumps(meta_payload, indent=2) + "\n",
                    actor_host=resolved_host,
                    home_dir=home_dir,
                    logger=active_logger,
                )
                brainstorms_copied += 1
            elif entry.category == "session":
                sessions_copied += 1
            elif entry.category == "pinned":
                pinned_copied = True

            manifest_entries[entry.source_rel] = {
                "category": entry.category,
                "target_rel": entry.target_rel,
                "sha256": sha256_hex,
                "migrated_at": _utc_timestamp_ms(),
            }
            planned_actions.append(
                {
                    "source": entry.source_rel,
                    "target": entry.target_rel,
                    "action": action_label,
                }
            )

        manifest_file = vault_path / manifest_rel
        if to_write or not manifest_file.exists():
            updated_manifest = {
                "schema_version": 1,
                "host_id": resolved_host,
                "updated_at": _utc_timestamp_ms(),
                "entries": manifest_entries,
            }
            atomic_write_vault_file(
                vault_path,
                manifest_rel,
                json.dumps(updated_manifest, indent=2) + "\n",
                actor_host=resolved_host,
                home_dir=home_dir,
                logger=active_logger,
            )

        if sessions_copied + brainstorms_copied + int(pinned_copied) > 0:
            append_mutation_event(
                vault_path,
                resolved_host,
                "migration",
                "v2-import",
                {
                    "sessions_copied": sessions_copied,
                    "brainstorms_copied": brainstorms_copied,
                    "pinned_copied": pinned_copied,
                    "skipped_existing": skipped_existing,
                },
                home_dir=home_dir,
                logger=active_logger,
            )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    active_logger.log_event(
        "INFO",
        "migrate",
        CP_VLT_01,
        duration_ms=elapsed_ms,
        details={
            "dry_run": dry_run,
            "sessions_copied": sessions_copied,
            "brainstorms_copied": brainstorms_copied,
            "pinned_copied": pinned_copied,
            "skipped_existing": skipped_existing,
            "conflicts_skipped": conflicts_skipped,
            "duration_ms": round(elapsed_ms, 3),
        },
    )

    return MigrationReport(
        dry_run=dry_run,
        host_id=resolved_host,
        source_dir=str(src_root),
        vault_dir=str(vault_path.resolve()),
        sessions_found=sessions_found,
        brainstorms_found=brainstorms_found,
        pinned_found=pinned_found,
        sessions_copied=sessions_copied,
        brainstorms_copied=brainstorms_copied,
        pinned_copied=pinned_copied,
        skipped_existing=skipped_existing,
        conflicts_skipped=conflicts_skipped,
        planned_actions=planned_actions,
        duration_ms=round(elapsed_ms, 3),
    )
