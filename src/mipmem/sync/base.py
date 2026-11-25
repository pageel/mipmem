"""SyncBackend protocol, configuration resolution, and atomic sync_status.json store."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator, Mapping, Optional, Protocol, Union, runtime_checkable

from mipmem.config import resolve_paths
from mipmem.core.diagnostics import (
    SYNC_BACKEND_UNCONFIGURED,
    MipmemError,
    RotatingJsonLogger,
    _best_effort_chmod,
    _utc_timestamp_ms,
)
from mipmem.core.redactor import scrub_without_logging
from mipmem.core.vault import default_host_id, validate_host_id

try:
    import fcntl as _imported_fcntl
except ImportError:  # pragma: no cover
    _imported_fcntl = None

_fcntl: Any = _imported_fcntl

SYNC_STATUS_FILENAME = "sync_status.json"
_TBD_MARKER = "[" + "TBD]"
_REMOTE_URI_PREFIXES = ("git@", "ssh://", "http://", "https://")


@contextmanager
def acquire_sync_lock(cache_dir: Path) -> Iterator[None]:
    """Acquire an exclusive fcntl.flock on <cache_dir>/sync.lock safely."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / "sync.lock"
    with open(lock_path, "a+", encoding="utf-8") as handle:
        locked = False
        if _fcntl is not None:
            try:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
        try:
            yield
        finally:
            if locked and _fcntl is not None:
                try:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                except OSError:
                    pass


@dataclass(frozen=True)
class SyncResult:
    """Structured result emitted by SyncBackend pull, push, and sync operations."""

    backend: str
    direction: str
    ok: bool
    files_changed: int = 0
    conflict: bool = False
    error_code: Optional[str] = None
    message: str = ""
    host_id: str = ""
    last_sync_utc: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize this sync result into a JSON-compatible dictionary."""
        return asdict(self)


# @para-doc [#csa-sync-backend-protocol]
@runtime_checkable
class SyncBackend(Protocol):
    """Unified multi-host vault synchronization contract."""

    def pull(self) -> SyncResult:
        """Pull remote shards and shared metadata into the local vault."""
        ...

    def push(self) -> SyncResult:
        """Push active host single-writer shards to the sync target."""
        ...

    def sync(self) -> SyncResult:
        """Execute a full pull-then-push synchronization cycle."""
        ...


def read_sync_status(cache_dir: Union[str, Path]) -> Optional[dict[str, Any]]:
    """Read <cache_dir>/sync_status.json safely, returning None if missing or corrupt."""
    target = Path(cache_dir).expanduser() / SYNC_STATUS_FILENAME
    if target.is_symlink() or not target.is_file():
        return None
    try:
        parsed = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def write_sync_status(
    cache_dir: Union[str, Path],
    result: Union[SyncResult, Mapping[str, Any]],
) -> Path:
    """Atomically persist redacted sync status telemetry into <cache_dir>/sync_status.json."""
    resolved_cache = Path(cache_dir).expanduser()
    resolved_cache.mkdir(parents=True, exist_ok=True)
    _best_effort_chmod(resolved_cache, 0o700)

    payload = dict(result.to_dict() if isinstance(result, SyncResult) else result)
    raw_msg = str(payload.get("message") or "")
    payload["message"] = scrub_without_logging(raw_msg)
    if not str(payload.get("last_sync_utc") or "").strip():
        payload["last_sync_utc"] = _utc_timestamp_ms()

    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    target = resolved_cache / SYNC_STATUS_FILENAME
    fd, tmp_name = tempfile.mkstemp(dir=str(resolved_cache), prefix=".tmp_sync_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        _best_effort_chmod(Path(tmp_name), 0o600)
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return target


def is_sync_configured(cfg: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Return (configured, backend_name, target) for a loaded MipMem config dictionary."""
    sync_tbl = cfg.get("sync", {}) if isinstance(cfg, Mapping) else {}
    if not isinstance(sync_tbl, Mapping):
        return False, "none", ""
    backend = str(sync_tbl.get("backend") or "none").strip().lower() or "none"
    remote_url = str(sync_tbl.get("vault_remote_url") or "").strip()
    folder_path = str(sync_tbl.get("folder_path") or "").strip()

    if backend == "git":
        if remote_url and remote_url != _TBD_MARKER:
            return True, "git", remote_url
        return False, "git", ""

    if backend == "folder":
        if folder_path and folder_path != _TBD_MARKER:
            return True, "folder", folder_path
        if (
            remote_url
            and remote_url != _TBD_MARKER
            and not remote_url.startswith(_REMOTE_URI_PREFIXES)
        ):
            return True, "folder", remote_url
        return False, "folder", ""

    return False, backend, ""


def resolve_sync_backend(
    cfg: Mapping[str, Any],
    *,
    vault_dir: Optional[Union[str, Path]] = None,
    cache_dir: Optional[Union[str, Path]] = None,
    host_id: Optional[str] = None,
    locale: Optional[str] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> SyncBackend:
    """Resolve and construct the active SyncBackend or raise SYNC_BACKEND_UNCONFIGURED."""
    configured, backend, target = is_sync_configured(cfg)
    if not configured:
        raise MipmemError(
            SYNC_BACKEND_UNCONFIGURED,
            f"Sync backend {backend!r} is not configured. Set `[sync]` remote or folder_path.",
        )

    core_tbl = cfg.get("core", {}) if isinstance(cfg, Mapping) else {}
    cfg_vault = core_tbl.get("vault_path") if isinstance(core_tbl, Mapping) else None
    paths = resolve_paths(configured_vault_dir=vault_dir if vault_dir is not None else cfg_vault)
    resolved_vault = Path(vault_dir).expanduser() if vault_dir is not None else paths.vault_dir
    resolved_cache = Path(cache_dir).expanduser() if cache_dir is not None else paths.cache_dir

    raw_host = host_id
    if not raw_host and isinstance(core_tbl, Mapping):
        cand = core_tbl.get("host_id")
        if isinstance(cand, str) and cand.strip():
            raw_host = cand.strip()
    active_host = validate_host_id(raw_host or default_host_id())

    active_locale = locale
    if not active_locale and isinstance(core_tbl, Mapping):
        cand_loc = core_tbl.get("locale")
        if isinstance(cand_loc, str) and cand_loc.strip():
            active_locale = cand_loc.strip()
    active_locale = active_locale or "en"

    if backend == "git":
        from mipmem.sync.git_backend import GitSyncBackend

        return GitSyncBackend(
            vault_dir=resolved_vault,
            cache_dir=resolved_cache,
            remote_url=target,
            host_id=active_host,
            locale=active_locale,
            logger=logger,
        )
    if backend == "folder":
        from mipmem.sync.folder_backend import FolderSyncBackend

        return FolderSyncBackend(
            vault_dir=resolved_vault,
            cache_dir=resolved_cache,
            folder_path=target,
            host_id=active_host,
            locale=active_locale,
            logger=logger,
        )
    raise MipmemError(
        SYNC_BACKEND_UNCONFIGURED,
        f"Unsupported sync backend {backend!r}. Expected 'git', 'folder', or 'none'.",
    )
