"""Structured rotating JSONL diagnostics and error taxonomy for MipMem."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

CONFIG_INVALID_TOML = "CONFIG_INVALID_TOML"
CONFIG_INVALID_HOST_ID = "CONFIG_INVALID_HOST_ID"
VAULT_NOT_INITIALIZED = "VAULT_NOT_INITIALIZED"
VAULT_PATH_TRAVERSAL = "VAULT_PATH_TRAVERSAL"
MIGRATE_SOURCE_MISSING = "MIGRATE_SOURCE_MISSING"
CLEANROOM_VIOLATION = "CLEANROOM_VIOLATION"
CLI_INVALID_ARGUMENT = "CLI_INVALID_ARGUMENT"

FTS_UNAVAILABLE = "FTS_UNAVAILABLE"
FTS_INDEX_CORRUPT = "FTS_INDEX_CORRUPT"
BRAINSTORM_NOT_FOUND = "BRAINSTORM_NOT_FOUND"
INVALID_SLICE_SELECTOR = "INVALID_SLICE_SELECTOR"
INVALID_BRAINSTORM_STATUS = "INVALID_BRAINSTORM_STATUS"

HOOK_INVALID_PAYLOAD = "HOOK_INVALID_PAYLOAD"
ADAPTER_READ_FAILED = "ADAPTER_READ_FAILED"
SLASH_COMMAND_INVALID = "SLASH_COMMAND_INVALID"
MCP_PARSE_ERROR = "MCP_PARSE_ERROR"
MCP_METHOD_NOT_FOUND = "MCP_METHOD_NOT_FOUND"
MCP_INVALID_PARAMS = "MCP_INVALID_PARAMS"
PLUGIN_MANIFEST_INVALID = "PLUGIN_MANIFEST_INVALID"

SPEC_ERROR_CODES: tuple[str, ...] = (
    CONFIG_INVALID_TOML,
    CONFIG_INVALID_HOST_ID,
    VAULT_NOT_INITIALIZED,
    VAULT_PATH_TRAVERSAL,
    MIGRATE_SOURCE_MISSING,
    CLEANROOM_VIOLATION,
)

S2_ERROR_CODES: tuple[str, ...] = (
    FTS_UNAVAILABLE,
    FTS_INDEX_CORRUPT,
    BRAINSTORM_NOT_FOUND,
    INVALID_SLICE_SELECTOR,
    INVALID_BRAINSTORM_STATUS,
)

S3_ERROR_CODES: tuple[str, ...] = (
    HOOK_INVALID_PAYLOAD,
    ADAPTER_READ_FAILED,
    SLASH_COMMAND_INVALID,
    MCP_PARSE_ERROR,
    MCP_METHOD_NOT_FOUND,
    MCP_INVALID_PARAMS,
    PLUGIN_MANIFEST_INVALID,
)

ALL_ERROR_CODES: tuple[str, ...] = SPEC_ERROR_CODES + S2_ERROR_CODES + S3_ERROR_CODES

CP_CFG_01 = "CP-CFG-01"
CP_RED_01 = "CP-RED-01"
CP_VLT_01 = "CP-VLT-01"
CP_IDX_01 = "CP-IDX-01"
CP_FTS_01 = "CP-FTS-01"
CP_MIP_01 = "CP-MIP-01"
CP_HK_01 = "CP-HK-01"
CP_ADP_01 = "CP-ADP-01"
CP_MCP_01 = "CP-MCP-01"

DEFAULT_MAX_LOG_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 2

_default_scrubber: Optional[Callable[[str], str]] = None
_custom_scrubber_set: bool = False


class MipmemError(Exception):
    """Domain exception carrying a canonical Spec S1 error code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        """Serialize the error into the canonical JSON CLI error envelope."""
        return {
            "success": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }


def register_default_scrubber(scrubber: Optional[Callable[[str], str]]) -> None:
    """Register or clear the global line scrubber used before writing JSONL logs."""
    global _default_scrubber, _custom_scrubber_set
    _default_scrubber = scrubber
    _custom_scrubber_set = scrubber is not None


def get_default_scrubber() -> Optional[Callable[[str], str]]:
    """Return the currently registered global line scrubber, if any."""
    return _default_scrubber


def _utc_timestamp_ms() -> str:
    """Format the current UTC timestamp in ISO-8601 millisecond Z notation."""
    now = datetime.now(timezone.utc)
    ms = now.microsecond // 1000
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"


def _best_effort_chmod(path: Path, mode: int) -> None:
    """Apply POSIX permissions while ignoring OSError on NTFS/exFAT/WSL mounts."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _is_writable_home(home_path: Path) -> bool:
    """Check whether a candidate home directory exists and can host cache data."""
    if not home_path.exists():
        return False
    return os.access(home_path, os.W_OK | os.X_OK)


# @para-doc [#csa-diag-json-logger]
class RotatingJsonLogger:
    """Lazy, clean-room, size-rotating JSONL logger writing under MIPMEM_CACHE_DIR."""

    def __init__(
        self,
        log_path: Optional[Path] = None,
        max_bytes: int = DEFAULT_MAX_LOG_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        scrubber: Optional[Callable[[str], str]] = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if backup_count < 0:
            raise ValueError("backup_count must be non-negative")
        self._log_path = log_path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._scrubber = scrubber

    def resolve_log_path(self) -> Path:
        """Resolve the destination log file lazily at call time (zero import-time I/O)."""
        if self._log_path is not None:
            return self._log_path

        env_cache = os.environ.get("MIPMEM_CACHE_DIR", "").strip()
        if env_cache:
            return Path(env_cache).expanduser() / "mipmem.log"

        xdg_cache = os.environ.get("XDG_CACHE_HOME", "").strip()
        if xdg_cache:
            return Path(xdg_cache).expanduser() / "mipmem" / "mipmem.log"

        home_env = os.environ.get("HOME")
        if home_env is not None and home_env.strip():
            home_dir = Path(home_env.strip()).expanduser()
            if _is_writable_home(home_dir):
                return home_dir / ".cache" / "mipmem" / "mipmem.log"
        elif home_env is None:
            try:
                home_dir = Path.home()
                if _is_writable_home(home_dir):
                    return home_dir / ".cache" / "mipmem" / "mipmem.log"
            except RuntimeError:
                pass

        plugin_data = os.environ.get("PLUGIN_DATA", "").strip()
        if plugin_data:
            return Path(plugin_data).expanduser() / "cache" / "mipmem.log"

        return Path.home() / ".cache" / "mipmem" / "mipmem.log"

    def _rotate_if_needed(self, target: Path, incoming_size: int) -> None:
        """Rotate existing log files when appending incoming_size exceeds max_bytes."""
        if not target.exists():
            return
        if target.stat().st_size + incoming_size <= self.max_bytes:
            return

        if self.backup_count == 0:
            target.unlink()
            return

        oldest = target.with_name(f"{target.name}.{self.backup_count}")
        if oldest.exists():
            oldest.unlink()

        for index in range(self.backup_count - 1, 0, -1):
            src = target.with_name(f"{target.name}.{index}")
            if src.exists():
                dst = target.with_name(f"{target.name}.{index + 1}")
                os.replace(src, dst)

        first_backup = target.with_name(f"{target.name}.1")
        os.replace(target, first_backup)

    def log_event(
        self,
        level: str,
        component: str,
        event: str,
        *,
        error_code: Optional[str] = None,
        duration_ms: Optional[float] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Path]:
        """Append one redacted single-line JSONL record; fails open on filesystem errors."""
        record: dict[str, Any] = {
            "timestamp": _utc_timestamp_ms(),
            "level": level,
            "component": component,
            "event": event,
            "error_code": error_code,
            "duration_ms": round(duration_ms, 3) if duration_ms is not None else None,
        }
        if details is not None:
            record["details"] = dict(details)

        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        scrubber = self._scrubber if self._scrubber is not None else _default_scrubber
        if scrubber is None and not _custom_scrubber_set:
            try:
                from mipmem.core.redactor import scrub_without_logging

                scrubber = scrub_without_logging
            except ImportError:
                scrubber = None
        if scrubber is not None:
            serialized = scrubber(serialized)
        serialized = serialized.replace("\r", " ").replace("\n", " ")
        payload_bytes = (serialized + "\n").encode("utf-8")

        target = self.resolve_log_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _best_effort_chmod(target.parent, 0o700)
            self._rotate_if_needed(target, len(payload_bytes))
            with open(target, "ab") as handle:
                handle.write(payload_bytes)
            _best_effort_chmod(target, 0o600)
        except OSError:
            return None
        return target
