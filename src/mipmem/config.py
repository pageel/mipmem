"""XDG path resolution, zero-dependency TOML codec, and workspace policy for MipMem."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Optional, Sequence, Union

from mipmem.core.diagnostics import (
    CONFIG_INVALID_TOML,
    CP_CFG_01,
    MipmemError,
    RotatingJsonLogger,
)

try:
    import tomllib as _tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    _tomllib: Any = None

_TBD_MARKER = "[" + "TBD]"
_INT_RE = re.compile(r"^[+-]?(?:0|[1-9](?:_?[0-9])*)$")
_FLOAT_RE = re.compile(
    r"^[+-]?(?:0|[1-9](?:_?[0-9])*)(?:\.[0-9](?:_?[0-9])*(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)$"
)
_SECTION_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_DEFAULT_CONFIG: dict[str, Any] = {
    "core": {
        "host_id": "workstation-alpha",
        "vault_path": "~/.local/share/mipmem/vault",
        "locale": "en",
    },
    "sync": {
        "backend": "none",
        "vault_remote_url": _TBD_MARKER,
        "folder_path": "",
        "auto_sync_on_stop": True,
    },
    "budget": {
        "l0_max_tokens": 300,
        "l1_max_tokens": 450,
        "topic_shift_novelty": 0.70,
    },
    "workspace_policies": {
        "default": "active",
        "passive_paths": [],
        "disabled_paths": [],
    },
}


@dataclass(frozen=True)
class MipmemPaths:
    """Resolved runtime configuration, vault, and cache directories."""

    config_dir: Path
    vault_dir: Path
    cache_dir: Path
    config_file: Path
    used_plugin_fallback: bool = False


def _is_writable_dir(path: Path) -> bool:
    """Return True if path exists and allows write and execute access."""
    return path.exists() and os.access(path, os.W_OK | os.X_OK)


def _resolve_writable_home() -> Optional[Path]:
    """Return the user's home directory only if it exists and is writable."""
    home_env = os.environ.get("HOME")
    if home_env is not None:
        trimmed = home_env.strip()
        if not trimmed:
            return None
        candidate = Path(trimmed).expanduser()
        return candidate if _is_writable_dir(candidate) else None
    try:
        candidate = Path.home()
    except RuntimeError:
        return None
    return candidate if _is_writable_dir(candidate) else None


# @para-doc [#csa-config-xdg-resolver]
def resolve_paths(
    configured_vault_dir: Optional[Union[str, Path]] = None,
) -> MipmemPaths:
    """Resolve config, vault, and cache paths cleanly across CLI and IDE plugins."""
    home_dir = _resolve_writable_home()
    plugin_raw = os.environ.get("PLUGIN_DATA", "").strip()
    plugin_dir = Path(plugin_raw).expanduser() if plugin_raw else None
    used_fallback = False

    env_cfg = os.environ.get("MIPMEM_CONFIG_DIR", "").strip()
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if env_cfg:
        config_dir = Path(env_cfg).expanduser()
    elif xdg_cfg:
        config_dir = Path(xdg_cfg).expanduser() / "mipmem"
    elif home_dir is not None:
        config_dir = home_dir / ".config" / "mipmem"
    elif plugin_dir is not None:
        config_dir = plugin_dir / "config"
        used_fallback = True
    else:
        config_dir = Path.home() / ".config" / "mipmem"

    env_vault = os.environ.get("MIPMEM_VAULT_DIR", "").strip()
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    cfg_vault_str = str(configured_vault_dir).strip() if configured_vault_dir is not None else ""
    if env_vault:
        vault_dir = Path(env_vault).expanduser()
    elif cfg_vault_str:
        vault_dir = Path(cfg_vault_str).expanduser()
    elif xdg_data:
        vault_dir = Path(xdg_data).expanduser() / "mipmem" / "vault"
    elif home_dir is not None:
        vault_dir = home_dir / ".local" / "share" / "mipmem" / "vault"
    elif plugin_dir is not None:
        vault_dir = plugin_dir / "vault"
        used_fallback = True
    else:
        vault_dir = Path.home() / ".local" / "share" / "mipmem" / "vault"

    env_cache = os.environ.get("MIPMEM_CACHE_DIR", "").strip()
    xdg_cache = os.environ.get("XDG_CACHE_HOME", "").strip()
    if env_cache:
        cache_dir = Path(env_cache).expanduser()
    elif xdg_cache:
        cache_dir = Path(xdg_cache).expanduser() / "mipmem"
    elif home_dir is not None:
        cache_dir = home_dir / ".cache" / "mipmem"
    elif plugin_dir is not None:
        cache_dir = plugin_dir / "cache"
        used_fallback = True
    else:
        cache_dir = Path.home() / ".cache" / "mipmem"

    return MipmemPaths(
        config_dir=config_dir,
        vault_dir=vault_dir,
        cache_dir=cache_dir,
        config_file=config_dir / "config.toml",
        used_plugin_fallback=used_fallback,
    )


def _strip_toml_comment(line: str, line_no: int) -> str:
    """Strip inline # comments outside quotes and reject unclosed single-line strings."""
    in_double = False
    in_single = False
    escaped = False
    for idx, ch in enumerate(line):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "#":
                return line[:idx]
    if in_double or in_single:
        raise MipmemError(
            CONFIG_INVALID_TOML,
            f"Invalid config syntax (line {line_no}): unterminated string literal.",
        )
    return line


def _count_brackets(text: str) -> int:
    """Return net [ minus ] balance outside quoted strings."""
    depth = 0
    in_double = False
    in_single = False
    escaped = False
    for ch in text:
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
    return depth


def _split_key_value(line: str, line_no: int) -> tuple[str, str]:
    """Split a TOML key = value line outside quotes."""
    in_double = False
    in_single = False
    escaped = False
    for idx, ch in enumerate(line):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "=":
                key = line[:idx].strip().strip('"').strip("'")
                val = line[idx + 1:].strip()
                if not key or not _KEY_RE.match(key):
                    raise MipmemError(
                        CONFIG_INVALID_TOML,
                        f"Invalid config syntax (line {line_no}): invalid key {key!r}.",
                    )
                if not val:
                    raise MipmemError(
                        CONFIG_INVALID_TOML,
                        f"Invalid config syntax (line {line_no}): missing value.",
                    )
                return key, val
    raise MipmemError(
        CONFIG_INVALID_TOML,
        f"Invalid config syntax (line {line_no}): expected '='.",
    )


def _unescape_basic_string(raw_inner: str, line_no: int) -> str:
    """Decode TOML basic string escape sequences."""
    out: list[str] = []
    idx = 0
    length = len(raw_inner)
    simple_escapes = {
        '"': '"',
        "\\": "\\",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    while idx < length:
        ch = raw_inner[idx]
        if ch != "\\":
            out.append(ch)
            idx += 1
            continue
        idx += 1
        if idx >= length:
            raise MipmemError(
                CONFIG_INVALID_TOML,
                f"Invalid config syntax (line {line_no}): trailing escape.",
            )
        esc = raw_inner[idx]
        if esc in simple_escapes:
            out.append(simple_escapes[esc])
            idx += 1
        elif esc == "u" and idx + 4 < length:
            hex_part = raw_inner[idx + 1:idx + 5]
            try:
                out.append(chr(int(hex_part, 16)))
            except ValueError as exc:
                raise MipmemError(
                    CONFIG_INVALID_TOML,
                    f"Invalid config syntax (line {line_no}): bad \\u escape.",
                ) from exc
            idx += 5
        else:
            raise MipmemError(
                CONFIG_INVALID_TOML,
                f"Invalid config syntax (line {line_no}): unsupported escape \\{esc}.",
            )
    return "".join(out)


def _parse_toml_array(expr: str, line_no: int) -> list[Any]:
    """Parse a single-line or normalized multi-line TOML array expression."""
    stripped = expr.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        raise MipmemError(
            CONFIG_INVALID_TOML,
            f"Invalid config syntax (line {line_no}): malformed array.",
        )
    inner = stripped[1:-1].strip()
    if not inner:
        return []

    items: list[str] = []
    start = 0
    depth = 0
    in_double = False
    in_single = False
    escaped = False

    for idx, ch in enumerate(inner):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            elif ch == "," and depth == 0:
                token = inner[start:idx].strip()
                if not token:
                    raise MipmemError(
                        CONFIG_INVALID_TOML,
                        f"Invalid config syntax (line {line_no}): empty array item.",
                    )
                items.append(token)
                start = idx + 1

    tail = inner[start:].strip()
    if tail:
        items.append(tail)

    return [_parse_toml_value(item, line_no) for item in items]


def _parse_toml_value(token: str, line_no: int) -> Any:
    """Parse one TOML scalar or array token."""
    val = token.strip()
    if not val:
        raise MipmemError(
            CONFIG_INVALID_TOML,
            f"Invalid config syntax (line {line_no}): empty value.",
        )
    if val.startswith("["):
        return _parse_toml_array(val, line_no)
    if val.startswith('"'):
        if len(val) < 2 or not val.endswith('"'):
            raise MipmemError(
                CONFIG_INVALID_TOML,
                f"Invalid config syntax (line {line_no}): unterminated string.",
            )
        return _unescape_basic_string(val[1:-1], line_no)
    if val.startswith("'"):
        if len(val) < 2 or not val.endswith("'"):
            raise MipmemError(
                CONFIG_INVALID_TOML,
                f"Invalid config syntax (line {line_no}): unterminated literal string.",
            )
        return val[1:-1]
    if val == "true":
        return True
    if val == "false":
        return False
    if _INT_RE.match(val):
        return int(val.replace("_", ""))
    if _FLOAT_RE.match(val):
        return float(val.replace("_", ""))
    raise MipmemError(
        CONFIG_INVALID_TOML,
        f"Invalid config syntax (line {line_no}): unsupported value {val!r}.",
    )


def _select_section(root: dict[str, Any], header: str, line_no: int) -> dict[str, Any]:
    """Resolve and create nested table dicts for a [section] header."""
    if not _SECTION_RE.match(header):
        raise MipmemError(
            CONFIG_INVALID_TOML,
            f"Invalid config syntax (line {line_no}): invalid section [{header}].",
        )
    cursor: dict[str, Any] = root
    for part in header.split("."):
        existing = cursor.get(part)
        if existing is None:
            new_table: dict[str, Any] = {}
            cursor[part] = new_table
            cursor = new_table
        elif isinstance(existing, dict):
            cursor = existing
        else:
            raise MipmemError(
                CONFIG_INVALID_TOML,
                f"Invalid config syntax (line {line_no}): section {part!r} redefined.",
            )
    return cursor


def _parse_minimal_toml(raw_text: str) -> dict[str, Any]:
    """Parse 2-level config.toml subset cleanly on Python 3.9 and 3.10."""
    result: dict[str, Any] = {}
    current_section: dict[str, Any] = result
    pending_key: Optional[str] = None
    pending_lines: list[str] = []
    pending_start_line = 0
    bracket_depth = 0

    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        if pending_key is not None:
            cleaned = _strip_toml_comment(raw_line, line_no).strip()
            if not cleaned:
                continue
            pending_lines.append(cleaned)
            bracket_depth += _count_brackets(cleaned)
            if bracket_depth < 0:
                raise MipmemError(
                    CONFIG_INVALID_TOML,
                    f"Invalid config syntax (line {line_no}): unexpected ']'.",
                )
            if bracket_depth == 0:
                joined = " ".join(pending_lines)
                current_section[pending_key] = _parse_toml_array(joined, pending_start_line)
                pending_key = None
                pending_lines = []
            continue

        cleaned = _strip_toml_comment(raw_line, line_no).strip()
        if not cleaned:
            continue

        if cleaned.startswith("["):
            if not cleaned.endswith("]") or cleaned.count("[") != 1:
                raise MipmemError(
                    CONFIG_INVALID_TOML,
                    f"Invalid config syntax (line {line_no}): unclosed section header.",
                )
            header = cleaned[1:-1].strip()
            current_section = _select_section(result, header, line_no)
            continue

        key, raw_val = _split_key_value(cleaned, line_no)
        if raw_val.startswith("["):
            balance = _count_brackets(raw_val)
            if balance < 0:
                raise MipmemError(
                    CONFIG_INVALID_TOML,
                    f"Invalid config syntax (line {line_no}): unmatched ']'.",
                )
            if balance > 0:
                pending_key = key
                pending_lines = [raw_val]
                pending_start_line = line_no
                bracket_depth = balance
                continue
        current_section[key] = _parse_toml_value(raw_val, line_no)

    if pending_key is not None:
        raise MipmemError(
            CONFIG_INVALID_TOML,
            f"Invalid config syntax (line {pending_start_line}): unclosed array.",
        )
    return result


def _format_toml_string(value: str) -> str:
    """Escape a Python string as a TOML basic string literal."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _format_toml_value(value: Any) -> str:
    """Serialize scalar or list config values deterministically."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _format_toml_string(value)
    if isinstance(value, (list, tuple)):
        rendered = ", ".join(_format_toml_value(item) for item in value)
        return f"[{rendered}]"
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def serialize_toml(data: dict[str, Any]) -> str:
    """Deterministically serialize 2-level config dictionaries into TOML text."""
    lines: list[str] = []
    scalar_top: list[tuple[str, Any]] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    for key, val in data.items():
        if isinstance(val, dict):
            tables.append((key, val))
        else:
            scalar_top.append((key, val))

    for key, val in scalar_top:
        lines.append(f"{key} = {_format_toml_value(val)}")
    if scalar_top and tables:
        lines.append("")

    for idx, (section, section_data) in enumerate(tables):
        lines.append(f"[{section}]")
        for key, val in section_data.items():
            lines.append(f"{key} = {_format_toml_value(val)}")
        if idx < len(tables) - 1:
            lines.append("")
    return "\n".join(lines) + "\n"


def default_config() -> dict[str, Any]:
    """Return a deep copy of the default configuration schema."""
    return deepcopy(_DEFAULT_CONFIG)


# @para-doc [#csa-config-toml-codec]
def load_config(
    config_path: Optional[Path] = None,
    *,
    logger: Optional[RotatingJsonLogger] = None,
) -> dict[str, Any]:
    """Load config.toml using tomllib (3.11+) or fallback TOML subset parser."""
    started = time.perf_counter()
    target = config_path if config_path is not None else resolve_paths().config_file
    if not target.exists():
        parsed = default_config()
    else:
        raw_text = target.read_text(encoding="utf-8")
        if _tomllib is not None:
            try:
                parsed = _tomllib.loads(raw_text)
            except Exception as exc:
                raise MipmemError(
                    CONFIG_INVALID_TOML,
                    f"Invalid config syntax at {target}: {exc}",
                ) from exc
        else:
            parsed = _parse_minimal_toml(raw_text)

    sync_section = parsed.get("sync", {})
    if isinstance(sync_section, dict):
        remote_url = str(sync_section.get("vault_remote_url", _TBD_MARKER) or "").strip()
        folder_path = str(sync_section.get("folder_path", "") or "").strip()
        backend = str(sync_section.get("backend", "none") or "none").strip().lower()
        is_local_only = (
            backend == "none"
            or (backend == "git" and (not remote_url or remote_url == _TBD_MARKER))
            or (
                backend == "folder"
                and not folder_path
                and (not remote_url or remote_url == _TBD_MARKER)
            )
        )
        if is_local_only:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            active_logger = logger if logger is not None else RotatingJsonLogger()
            active_logger.log_event(
                "INFO",
                "config",
                CP_CFG_01,
                duration_ms=elapsed_ms,
                details={"mode": "local_only"},
            )
    return parsed


def save_config(data: dict[str, Any], config_path: Optional[Path] = None) -> Path:
    """Atomically write validated config.toml to disk with 0600 best-effort mode."""
    target = config_path if config_path is not None else resolve_paths().config_file
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = serialize_toml(data)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_cfg_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return target


_POLICY_RANK: dict[str, int] = {
    "active": 0,
    "passive": 1,
    "disabled": 2,
}


def _normalize_policy_path(raw: Union[str, Path]) -> Path:
    """Resolve user-supplied workspace or policy paths including symlinks."""
    candidate = Path(str(raw)).expanduser()
    try:
        return candidate.resolve()
    except OSError:
        return candidate.absolute()


def _matches_any_root(target: Path, roots: Sequence[Path]) -> bool:
    """Return True if target equals or is nested inside any normalized root path."""
    for root in roots:
        if target == root or target.is_relative_to(root):
            return True
    return False


# @para-doc [#csa-config-workspace-policy]
def resolve_workspace_policy(
    workspace_paths: Union[str, Path, Sequence[Union[str, Path]]],
    config: dict[str, Any],
) -> str:
    """Evaluate active, passive, or disabled policy using Most-Restrictive Wins."""
    policies = config.get("workspace_policies", {})
    if not isinstance(policies, dict):
        policies = {}

    default_raw = str(policies.get("default", "active")).lower()
    default_mode = default_raw if default_raw in _POLICY_RANK else "active"

    disabled_roots = [
        _normalize_policy_path(item)
        for item in policies.get("disabled_paths", [])
        if str(item).strip()
    ]
    passive_roots = [
        _normalize_policy_path(item)
        for item in policies.get("passive_paths", [])
        if str(item).strip()
    ]

    if isinstance(workspace_paths, (str, Path)):
        candidates: list[Union[str, Path]] = [workspace_paths]
    else:
        candidates = list(workspace_paths)

    if not candidates:
        return default_mode

    best_mode = default_mode
    for item in candidates:
        normalized = _normalize_policy_path(item)
        if _matches_any_root(normalized, disabled_roots):
            return "disabled"
        if _matches_any_root(normalized, passive_roots):
            mode = "passive"
        else:
            mode = default_mode
        if _POLICY_RANK[mode] > _POLICY_RANK[best_mode]:
            best_mode = mode
    return best_mode
