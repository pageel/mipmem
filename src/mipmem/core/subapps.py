"""Per-host private sub-app telemetry reader, L0/fleet presenter, and RCE-safe apps.d delegator."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Mapping, Optional, Sequence, Union

from mipmem.core.diagnostics import (
    CP_SUB_01,
    SUBAPP_EXEC_FAILED,
    SUBAPP_EXEC_TIMEOUT,
    SUBAPP_UNTRUSTED_PERMS,
    MipmemError,
    RotatingJsonLogger,
    _utc_timestamp_ms,
)
from mipmem.core.redactor import redact_text, scrub_without_logging
from mipmem.core.vault import is_vault_initialized, validate_host_id

APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VALID_TELEMETRY_STATUSES: frozenset[str] = frozenset({"ok", "warn", "critical"})
CORE_SLASH_SUBCOMMANDS: frozenset[str] = frozenset(
    {"help", "status", "recall", "list", "bs", "config", "handoff", "fleet", "sync"}
)
DEFAULT_DELEGATE_TIMEOUT_SEC = 5.0
MAX_DELEGATE_TIMEOUT_SEC = 15.0
MAX_DELEGATE_OUTPUT_BYTES = 65536


def is_valid_app_id(app_id: str) -> bool:
    """Validate a sub-app slug matching ^[a-z0-9][a-z0-9_-]{0,63}$."""
    if not isinstance(app_id, str):
        return False
    return bool(APP_ID_RE.match(app_id.strip()))


@dataclass(frozen=True)
class SubAppTelemetry:
    """Validated per-host private sub-app telemetry status record."""

    host_id: str
    app_id: str
    status: str
    badge: str
    l0_summary: str
    alert_on_l0: bool
    detail_file: Optional[str]
    updated_at: str
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        """Serialize this telemetry record into a JSON-compatible dictionary."""
        return asdict(self)


def resolve_telemetry_detail_path(host_dir: Path, raw_detail: Any) -> Optional[str]:
    """Resolve detail_file/detail_path strictly within host_dir without symlink escapes."""
    if not isinstance(raw_detail, str):
        return None
    cleaned = raw_detail.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("~"):
        return None
    rel_candidate = Path(cleaned)
    if rel_candidate.is_absolute() or ".." in rel_candidate.parts:
        return None

    host_unexpanded = host_dir.expanduser()
    probe = host_unexpanded
    for part in rel_candidate.parts:
        if part in ("", "."):
            continue
        probe = probe / part
        if probe.is_symlink():
            return None

    try:
        host_resolved = host_unexpanded.resolve()
        resolved = (host_resolved / rel_candidate).resolve()
    except OSError:
        return None

    if resolved == host_resolved or not resolved.is_relative_to(host_resolved):
        return None
    return resolved.relative_to(host_resolved).as_posix()


def extract_safe_detail_path(host_dir: Path, data: Mapping[str, Any]) -> Optional[str]:
    """Extract and validate detail_file (with legacy detail_path fallback) inside host_dir."""
    raw_primary = data.get("detail_file")
    if isinstance(raw_primary, str) and raw_primary.strip():
        return resolve_telemetry_detail_path(host_dir, raw_primary)
    return resolve_telemetry_detail_path(host_dir, data.get("detail_path"))


def _resolve_telemetry_target_hosts(
    hosts_dir: Path,
    host_id: Optional[str],
) -> list[tuple[str, Path]]:
    """Collect valid non-symlink host directories to scan for sub-app telemetry."""
    if host_id is not None:
        safe_host = validate_host_id(host_id)
        candidate = hosts_dir / safe_host
        if candidate.is_dir() and not candidate.is_symlink():
            return [(safe_host, candidate)]
        return []

    targets: list[tuple[str, Path]] = []
    for entry in sorted(hosts_dir.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            safe_name = validate_host_id(entry.name)
        except Exception:
            continue
        targets.append((safe_name, entry))
    return targets


# @para-doc [#csa-subapp-telemetry-reader]
def load_subapp_telemetry(
    vault_root: Union[str, Path],
    *,
    host_id: Optional[str] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> list[SubAppTelemetry]:
    """Load validated telemetry/*.status.json entries across one or all vault hosts."""
    started = time.perf_counter()
    hosts_dir = Path(vault_root).expanduser() / "hosts"
    if not hosts_dir.is_dir() or hosts_dir.is_symlink():
        return []

    results: list[SubAppTelemetry] = []
    for safe_host, host_dir in _resolve_telemetry_target_hosts(hosts_dir, host_id):
        telemetry_dir = host_dir / "telemetry"
        if not telemetry_dir.is_dir() or telemetry_dir.is_symlink():
            continue
        for status_file in sorted(telemetry_dir.glob("*.status.json")):
            if status_file.is_symlink() or not status_file.is_file():
                continue
            try:
                raw_text = status_file.read_text(encoding="utf-8")
                parsed = json.loads(raw_text)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(parsed, dict):
                continue

            stem_id = status_file.name[: -len(".status.json")]
            raw_app_id = parsed.get("app_id")
            app_id = (
                str(raw_app_id).strip()
                if raw_app_id is not None and str(raw_app_id).strip()
                else stem_id
            )
            if not is_valid_app_id(app_id):
                continue

            status = str(parsed.get("status", "")).strip().lower()
            if status not in VALID_TELEMETRY_STATUSES:
                continue

            badge = scrub_without_logging(str(parsed.get("badge", "")).strip())
            l0_summary = scrub_without_logging(str(parsed.get("l0_summary", "")).strip())
            alert_on_l0 = bool(parsed.get("alert_on_l0", False))
            detail_file = extract_safe_detail_path(host_dir, parsed)
            updated_at = str(parsed.get("updated_at", "")).strip() or _utc_timestamp_ms()
            schema_version = str(parsed.get("schema_version", "1")).strip() or "1"

            results.append(
                SubAppTelemetry(
                    host_id=safe_host,
                    app_id=app_id,
                    status=status,
                    badge=badge,
                    l0_summary=l0_summary,
                    alert_on_l0=alert_on_l0,
                    detail_file=detail_file,
                    updated_at=updated_at,
                    schema_version=schema_version,
                )
            )

    results.sort(key=lambda item: (item.host_id, item.app_id))
    if logger is not None:
        logger.log_event(
            "INFO",
            "subapps",
            CP_SUB_01,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            details={
                "action": "telemetry_scan",
                "host_id": host_id or "*",
                "count": len(results),
                "status": "ok",
            },
        )
    return results


def read_cached_sync_status(cache_dir: Optional[Path]) -> Optional[dict[str, Any]]:
    """Read <cache_dir>/sync_status.json safely without raising on missing/corrupt files."""
    if cache_dir is None:
        return None
    status_path = Path(cache_dir).expanduser() / "sync_status.json"
    if status_path.is_symlink() or not status_path.is_file():
        return None
    try:
        parsed = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def read_sync_alert(cache_dir: Optional[Path]) -> Optional[str]:
    """Return a concise sync alert string when sync_status.json indicates conflict or error."""
    status = read_cached_sync_status(cache_dir)
    if status is None:
        return None
    has_conflict = bool(status.get("conflict"))
    err_code = str(status.get("error_code") or "").strip()
    message = str(status.get("message") or "").strip()
    if not has_conflict and not err_code:
        return None
    label = err_code or "SYNC_CONFLICT_DETECTED"
    if message:
        return f"{label}: {message}"
    return f"{label} (run `mipmem sync`)"


# @para-doc [#csa-subapp-l0-fleet-presenter]
def format_l0_telemetry_lines(
    telemetry: Sequence[SubAppTelemetry],
    *,
    sync_alert: Optional[str] = None,
) -> list[str]:
    """Format compact L0 Radar lines honoring empty-hide, all-ok collapse, and alert rules."""
    if not telemetry and not sync_alert:
        return []

    lines: list[str] = []
    if sync_alert:
        lines.append(f"- [SYNC WARN] {scrub_without_logging(sync_alert)[:120]}")

    if telemetry:
        flagged = [
            item
            for item in telemetry
            if item.status in ("warn", "critical") or item.alert_on_l0
        ]
        quiet_ok = [item for item in telemetry if item not in flagged and item.status == "ok"]
        if not flagged:
            badges = ", ".join(
                f"{item.app_id}={item.badge}" if item.badge else item.app_id
                for item in quiet_ok[:4]
            )
            suffix = ", ..." if len(quiet_ok) > 4 else ""
            lines.append(f"- Sub-Apps: {len(quiet_ok)} ok ({badges}{suffix})")
        else:
            severity_rank = {"critical": 0, "warn": 1, "ok": 2}
            flagged.sort(
                key=lambda item: (severity_rank.get(item.status, 3), item.host_id, item.app_id)
            )
            for item in flagged[:4]:
                detail = item.l0_summary or item.badge or item.status
                badge_part = f" ({item.badge})" if item.badge else ""
                lines.append(
                    f"- [{item.status}] `{item.host_id}/{item.app_id}`{badge_part}: "
                    f"{detail[:80]}"
                )
            if quiet_ok:
                lines.append(f"- Sub-Apps: {len(quiet_ok)} ok")
    return lines


def build_fleet_status(
    vault_root: Union[str, Path],
    *,
    active_host_id: str,
    cache_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> dict[str, Any]:
    """Build unified fleet inventory, sub-app telemetry list, and cached sync status."""
    vault_dir = Path(vault_root).expanduser()
    sync_status = read_cached_sync_status(cache_dir)
    if not is_vault_initialized(vault_dir):
        return {
            "vault_dir": str(vault_dir),
            "vault_initialized": False,
            "active_host_id": active_host_id,
            "hosts": [],
            "total_sessions": 0,
            "total_brainstorms": 0,
            "subapp_telemetry": [],
            "sync_status": sync_status,
        }

    from mipmem.adapters.slash_mem import load_recent_sessions
    from mipmem.core.fts_recall import list_indexed_brainstorms

    hosts_dir = vault_dir / "hosts"
    indexed_bs = list_indexed_brainstorms(vault_dir, auto_sync=True, logger=logger)
    bs_by_host: dict[str, int] = {}
    for item in indexed_bs:
        origin = str(item.get("origin_host", ""))
        bs_by_host[origin] = bs_by_host.get(origin, 0) + 1

    hosts_summary: list[dict[str, Any]] = []
    total_sessions = 0
    if hosts_dir.is_dir():
        for host_dir in sorted(hosts_dir.iterdir()):
            if not host_dir.is_dir() or host_dir.is_symlink():
                continue
            h_id = host_dir.name
            meta_info: dict[str, Any] = {}
            meta_file = host_dir / "host_meta.json"
            if meta_file.is_file() and not meta_file.is_symlink():
                try:
                    parsed = json.loads(meta_file.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        meta_info = parsed
                except (OSError, json.JSONDecodeError):
                    meta_info = {}
            sess_count = len(load_recent_sessions(vault_dir, host_id=h_id, limit=2000))
            total_sessions += sess_count
            handoff_exists = (host_dir / "latest_handoff.md").is_file()
            hosts_summary.append(
                {
                    "host_id": h_id,
                    "sessions_count": sess_count,
                    "brainstorms_count": bs_by_host.get(h_id, 0),
                    "has_latest_handoff": handoff_exists,
                    "meta": meta_info,
                }
            )

    telemetry_items = load_subapp_telemetry(vault_dir, logger=logger)
    return {
        "vault_dir": str(vault_dir),
        "vault_initialized": True,
        "active_host_id": active_host_id,
        "hosts": hosts_summary,
        "total_sessions": total_sessions,
        "total_brainstorms": len(indexed_bs),
        "subapp_telemetry": [item.to_dict() for item in telemetry_items],
        "sync_status": sync_status,
    }


def _fleet_cell(text: str, *, max_len: int) -> str:
    """Format a compact single-line GFM table cell."""
    cleaned = " ".join(str(text or "").split()).replace("|", "\\|")
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max(1, max_len - 3)].rstrip() + "..."


def format_fleet_markdown(
    fleet: Mapping[str, Any],
    *,
    surface: str = "generic",
) -> str:
    """Render compact GFM Markdown tables for /mem fleet on CLI and IDE surfaces."""
    summary_width = 28 if surface == "cli" else 56
    vault_dir = scrub_without_logging(str(fleet.get("vault_dir", "")))
    active_host = str(fleet.get("active_host_id", ""))
    lines = [
        "### MipMem Fleet Status",
        "",
        f"- **Active Host:** `{active_host}` | **Vault:** `{vault_dir}`",
    ]
    sync_status = fleet.get("sync_status")
    if isinstance(sync_status, Mapping):
        backend = str(sync_status.get("backend", "none"))
        conflict = bool(sync_status.get("conflict"))
        err = str(sync_status.get("error_code") or "").strip()
        last_sync = str(sync_status.get("last_sync_utc") or "never")
        state_str = (
            f"CONFLICT ({err})"
            if conflict and err
            else ("CONFLICT" if conflict else (err or "ok"))
        )
        lines.append(f"- **Sync:** `{backend}` (`{state_str}`, last: `{last_sync}`)")

    hosts = fleet.get("hosts", [])
    if isinstance(hosts, Sequence) and hosts:
        lines.extend(
            [
                "",
                "| Host | Sessions | Brainstorms | Handoff |",
                "| :--- | :---: | :---: | :---: |",
            ]
        )
        for h in hosts:
            if not isinstance(h, Mapping):
                continue
            mark = "yes" if h.get("has_latest_handoff") else "no"
            lines.append(
                f"| `{h.get('host_id', '')}` | {h.get('sessions_count', 0)} | "
                f"{h.get('brainstorms_count', 0)} | {mark} |"
            )

    telemetry = fleet.get("subapp_telemetry", [])
    if isinstance(telemetry, Sequence) and telemetry:
        lines.extend(
            [
                "",
                "| Host | App | Status | Badge | Summary |",
                "| :--- | :--- | :---: | :--- | :--- |",
            ]
        )
        for item in telemetry:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                f"| `{item.get('host_id', '')}` | `{item.get('app_id', '')}` | "
                f"`{item.get('status', '')}` | "
                f"{_fleet_cell(str(item.get('badge', '')), max_len=18)} | "
                f"{_fleet_cell(str(item.get('l0_summary', '')), max_len=summary_width)} |"
            )
    else:
        lines.extend(["", "_No sub-app telemetry recorded yet._"])

    return "\n".join(lines)


@dataclass(frozen=True)
class SubAppDelegate:
    """Validated local-only sub-app delegate descriptor discovered from apps.d/*.json."""

    subcommand: str
    executable: Path
    args: tuple[str, ...]
    description: str
    timeout_sec: float
    manifest_path: Path


def _scrub_delegate_output(text: str, logger: Optional[RotatingJsonLogger]) -> str:
    """Redact secrets and home paths from delegate stdout/stderr."""
    if not text:
        return ""
    if logger is not None:
        return redact_text(text, logger=logger)
    return scrub_without_logging(text)


def _assert_trusted_local_entry(
    path: Path,
    *,
    expect_dir: bool,
    vault_root: Optional[Path] = None,
) -> Path:
    """Verify a local apps.d file or directory is not symlinked, Vault-hosted, or 0o022."""
    kind_label = "apps.d directory" if expect_dir else "sub-app file"
    raw_candidate = path.expanduser()
    if raw_candidate.is_symlink():
        raise MipmemError(
            SUBAPP_UNTRUSTED_PERMS,
            f"Refusing symlinked {kind_label}: {raw_candidate.name}.",
        )
    try:
        resolved = raw_candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise MipmemError(
            SUBAPP_EXEC_FAILED,
            f"Missing {kind_label}: {raw_candidate.name}.",
        ) from exc
    except OSError as exc:
        raise MipmemError(
            SUBAPP_UNTRUSTED_PERMS,
            f"Unable to verify {kind_label} {raw_candidate.name}: {exc}",
        ) from exc

    valid_kind = resolved.is_dir() if expect_dir else resolved.is_file()
    if not valid_kind:
        expected_desc = "a directory" if expect_dir else "a regular file"
        raise MipmemError(
            SUBAPP_UNTRUSTED_PERMS,
            f"{kind_label} must be {expected_desc}: {resolved.name}.",
        )
    if vault_root is not None:
        vault_resolved = vault_root.expanduser().resolve()
        if resolved == vault_resolved or resolved.is_relative_to(vault_resolved):
            raise MipmemError(
                SUBAPP_UNTRUSTED_PERMS,
                f"Refusing {kind_label} located inside synced vault.",
            )
    if os.name == "posix":
        st = resolved.stat()
        if st.st_uid != os.getuid():
            raise MipmemError(
                SUBAPP_UNTRUSTED_PERMS,
                f"Untrusted owner for {kind_label}: {resolved.name}.",
            )
        if st.st_mode & 0o022:
            raise MipmemError(
                SUBAPP_UNTRUSTED_PERMS,
                f"Group/world-writable {kind_label} rejected: {resolved.name}.",
            )
    return resolved


# @para-doc [#csa-subapp-appsd-delegator]
def _assert_trusted_local_file(
    path: Path,
    *,
    vault_root: Optional[Path] = None,
) -> Path:
    """Verify a delegate manifest or executable is local, UID-owned, and not 0o022-writable."""
    return _assert_trusted_local_entry(path, expect_dir=False, vault_root=vault_root)


def _assert_trusted_local_dir(
    path: Path,
    *,
    vault_root: Optional[Path] = None,
) -> Path:
    """Verify the local apps.d directory is not inside Vault, symlinked, or 0o022-writable."""
    return _assert_trusted_local_entry(path, expect_dir=True, vault_root=vault_root)


def _clamp_delegate_timeout(raw_timeout: Any) -> float:
    """Clamp delegate execution timeout to [0.1, MAX_DELEGATE_TIMEOUT_SEC]."""
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        return DEFAULT_DELEGATE_TIMEOUT_SEC
    val = float(raw_timeout)
    if val <= 0.0:
        return DEFAULT_DELEGATE_TIMEOUT_SEC
    return min(MAX_DELEGATE_TIMEOUT_SEC, max(0.1, val))


def _parse_delegate_manifest(
    manifest_file: Path,
    *,
    vault_root: Optional[Path] = None,
) -> SubAppDelegate:
    """Validate and parse one local apps.d/*.json manifest and its executable."""
    trusted_manifest = _assert_trusted_local_file(manifest_file, vault_root=vault_root)
    try:
        parsed = json.loads(trusted_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MipmemError(
            SUBAPP_EXEC_FAILED,
            f"Invalid JSON manifest {trusted_manifest.name}: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise MipmemError(
            SUBAPP_EXEC_FAILED,
            f"Manifest {trusted_manifest.name} must contain a JSON object.",
        )

    subcommand = str(parsed.get("subcommand") or trusted_manifest.stem).strip().lower()
    if not is_valid_app_id(subcommand):
        raise MipmemError(
            SUBAPP_EXEC_FAILED,
            f"Invalid subcommand slug {subcommand!r} in {trusted_manifest.name}.",
        )
    if subcommand in CORE_SLASH_SUBCOMMANDS:
        raise MipmemError(
            SUBAPP_EXEC_FAILED,
            f"Sub-app cannot override core `/mem {subcommand}` command.",
        )

    raw_exec = parsed.get("executable")
    if not isinstance(raw_exec, str) or not raw_exec.strip():
        raise MipmemError(
            SUBAPP_EXEC_FAILED,
            f"Manifest {trusted_manifest.name} missing executable path.",
        )
    exec_candidate = Path(raw_exec.strip()).expanduser()
    if not exec_candidate.is_absolute():
        exec_candidate = trusted_manifest.parent / exec_candidate
    trusted_exec = _assert_trusted_local_file(exec_candidate, vault_root=vault_root)

    raw_args = parsed.get("args", ())
    if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
        raise MipmemError(
            SUBAPP_EXEC_FAILED,
            f"Manifest {trusted_manifest.name} 'args' must be a list of strings.",
        )

    timeout_sec = _clamp_delegate_timeout(parsed.get("timeout_sec"))
    description = scrub_without_logging(str(parsed.get("description") or "").strip())
    return SubAppDelegate(
        subcommand=subcommand,
        executable=trusted_exec,
        args=tuple(raw_args),
        description=description,
        timeout_sec=timeout_sec,
        manifest_path=trusted_manifest,
    )


def _log_subapp_event(
    logger: Optional[RotatingJsonLogger],
    *,
    level: str,
    action: str,
    app_id: str,
    status: str,
    duration_ms: float,
    error_code: Optional[str] = None,
) -> None:
    """Emit structured CP-SUB-01 telemetry when logger is provided."""
    if logger is None:
        return
    logger.log_event(
        level,
        "subapps",
        CP_SUB_01,
        error_code=error_code,
        duration_ms=duration_ms,
        details={"action": action, "app_id": app_id, "status": status},
    )


def discover_app_delegates(
    config_dir: Path,
    *,
    vault_root: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> dict[str, SubAppDelegate]:
    """Discover trusted local sub-app delegates from <config_dir>/apps.d/*.json only."""
    apps_dir = Path(config_dir).expanduser() / "apps.d"
    if not os.path.lexists(apps_dir):
        return {}
    try:
        trusted_dir = _assert_trusted_local_dir(apps_dir, vault_root=vault_root)
    except MipmemError as exc:
        _log_subapp_event(
            logger,
            level="WARN",
            action="delegate_discover",
            app_id="*",
            status="rejected",
            duration_ms=0.0,
            error_code=exc.code,
        )
        return {}

    delegates: dict[str, SubAppDelegate] = {}
    for manifest_file in sorted(trusted_dir.glob("*.json")):
        try:
            delegate = _parse_delegate_manifest(manifest_file, vault_root=vault_root)
        except MipmemError as exc:
            _log_subapp_event(
                logger,
                level="WARN",
                action="delegate_discover",
                app_id=manifest_file.stem,
                status="rejected",
                duration_ms=0.0,
                error_code=exc.code,
            )
            continue
        delegates[delegate.subcommand] = delegate
    return delegates


def _find_manifest_for_subcommand(apps_dir: Path, subcommand: str) -> Optional[Path]:
    """Locate the manifest path matching subcommand (including symlink/perm candidates)."""
    direct = apps_dir / f"{subcommand}.json"
    if os.path.lexists(direct):
        return direct
    if not apps_dir.is_dir() or apps_dir.is_symlink():
        return None
    for manifest_file in sorted(apps_dir.glob("*.json")):
        if manifest_file.is_symlink() or not manifest_file.is_file():
            continue
        try:
            parsed = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            declared = str(parsed.get("subcommand") or manifest_file.stem).strip().lower()
            if declared == subcommand:
                return manifest_file
    return None


def has_app_delegate_candidate(config_dir: Path, subcommand: str) -> bool:
    """Check whether <config_dir>/apps.d contains a candidate manifest for subcommand."""
    cleaned = str(subcommand or "").strip().lower()
    if not cleaned or cleaned in CORE_SLASH_SUBCOMMANDS or not is_valid_app_id(cleaned):
        return False
    apps_dir = Path(config_dir).expanduser() / "apps.d"
    return _find_manifest_for_subcommand(apps_dir, cleaned) is not None


def run_app_delegate(
    subcommand: str,
    user_args: Sequence[str],
    *,
    config_dir: Path,
    vault_root: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> str:
    """Execute a verified local apps.d delegate with shell=False, timeout cap, and redaction."""
    started = time.perf_counter()
    cleaned = str(subcommand or "").strip().lower()
    if not is_valid_app_id(cleaned) or cleaned in CORE_SLASH_SUBCOMMANDS:
        raise MipmemError(
            SUBAPP_EXEC_FAILED,
            f"Subcommand {subcommand!r} cannot be delegated.",
        )

    apps_dir = Path(config_dir).expanduser() / "apps.d"
    try:
        trusted_dir = _assert_trusted_local_dir(apps_dir, vault_root=vault_root)
        manifest_path = _find_manifest_for_subcommand(trusted_dir, cleaned)
        if manifest_path is None:
            raise MipmemError(SUBAPP_EXEC_FAILED, f"Unknown sub-app delegate: {cleaned!r}.")
        delegate = _parse_delegate_manifest(manifest_path, vault_root=vault_root)
    except MipmemError as exc:
        _log_subapp_event(
            logger,
            level="WARN",
            action="delegate_exec",
            app_id=cleaned,
            status="rejected",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error_code=exc.code,
        )
        raise

    argv = [str(delegate.executable), *delegate.args, *(str(a) for a in user_args)]
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            timeout=delegate.timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _log_subapp_event(
            logger,
            level="WARN",
            action="delegate_exec",
            app_id=cleaned,
            status="timeout",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error_code=SUBAPP_EXEC_TIMEOUT,
        )
        raise MipmemError(
            SUBAPP_EXEC_TIMEOUT,
            f"Sub-app `/mem {cleaned}` timed out after {delegate.timeout_sec:.2f}s.",
        ) from exc
    except OSError as exc:
        err_text = _scrub_delegate_output(str(exc), logger)
        _log_subapp_event(
            logger,
            level="WARN",
            action="delegate_exec",
            app_id=cleaned,
            status="error",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error_code=SUBAPP_EXEC_FAILED,
        )
        raise MipmemError(
            SUBAPP_EXEC_FAILED,
            f"Failed to launch sub-app `/mem {cleaned}`: {err_text}",
        ) from exc

    raw_stdout = proc.stdout[:MAX_DELEGATE_OUTPUT_BYTES].decode("utf-8", errors="replace")
    raw_stderr = proc.stderr[:MAX_DELEGATE_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stdout_clean = _scrub_delegate_output(raw_stdout, logger).strip()
    stderr_clean = _scrub_delegate_output(raw_stderr, logger).strip()
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if proc.returncode != 0:
        detail = stderr_clean or stdout_clean or f"exit status {proc.returncode}"
        _log_subapp_event(
            logger,
            level="WARN",
            action="delegate_exec",
            app_id=cleaned,
            status="failed",
            duration_ms=elapsed_ms,
            error_code=SUBAPP_EXEC_FAILED,
        )
        raise MipmemError(
            SUBAPP_EXEC_FAILED,
            f"Sub-app `/mem {cleaned}` failed ({proc.returncode}): {detail}",
        )

    _log_subapp_event(
        logger,
        level="INFO",
        action="delegate_exec",
        app_id=cleaned,
        status="ok",
        duration_ms=elapsed_ms,
    )
    if stdout_clean:
        return stdout_clean
    if stderr_clean:
        return stderr_clean
    return f"Sub-app `/mem {cleaned}` completed (exit 0)."
