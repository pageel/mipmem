"""Lean CLI entrypoint router maintaining < 25ms cold-start for hot paths."""

from __future__ import annotations

import os
import sys
import time


def _json_escape(value: str) -> str:
    """Escape a string value safely for inline JSON serialization."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _fast_writable_home() -> str | None:
    """Return expanded $HOME when present and writable without importing pathlib."""
    home_env = os.environ.get("HOME")
    if home_env is not None:
        trimmed = home_env.strip()
        if not trimmed:
            return None
        candidate = os.path.expanduser(trimmed)
    else:
        candidate = os.path.expanduser("~")
    if os.path.exists(candidate) and os.access(candidate, os.W_OK | os.X_OK):
        return candidate
    return None


def _fast_health(raw_args: list[str], started: float) -> int | None:
    """Serve `mipmem health [--vault <dir>] [--json]` without loading heavy modules."""
    positionals: list[str] = []
    json_mode = False
    vault_override: str | None = None
    idx = 0
    total = len(raw_args)
    while idx < total:
        tok = raw_args[idx]
        if tok == "--json":
            json_mode = True
        elif tok == "--vault":
            if idx + 1 >= total:
                return None
            vault_override = raw_args[idx + 1]
            idx += 1
        elif tok.startswith("--vault="):
            vault_override = tok.split("=", 1)[1]
        elif tok.startswith("-"):
            return None
        else:
            positionals.append(tok)
        idx += 1
    if positionals != ["health"]:
        return None

    home_dir = _fast_writable_home()
    plugin_raw = os.environ.get("PLUGIN_DATA", "").strip()
    plugin_dir = os.path.expanduser(plugin_raw) if plugin_raw else None
    env_cfg = os.environ.get("MIPMEM_CONFIG_DIR", "").strip()
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if env_cfg:
        cfg_dir = os.path.expanduser(env_cfg)
    elif xdg_cfg:
        cfg_dir = os.path.join(os.path.expanduser(xdg_cfg), "mipmem")
    elif home_dir is not None:
        cfg_dir = os.path.join(home_dir, ".config", "mipmem")
    elif plugin_dir is not None:
        cfg_dir = os.path.join(plugin_dir, "config")
    else:
        cfg_dir = os.path.join(os.path.expanduser("~"), ".config", "mipmem")

    config_file = os.path.join(cfg_dir, "config.toml")
    config_exists = os.path.isfile(config_file)
    env_vault = os.environ.get("MIPMEM_VAULT_DIR", "").strip()
    if vault_override is not None:
        vault_dir = os.path.expanduser(vault_override)
    elif env_vault:
        vault_dir = os.path.expanduser(env_vault)
    elif config_exists:
        return None
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg_data:
            vault_dir = os.path.join(os.path.expanduser(xdg_data), "mipmem", "vault")
        elif home_dir is not None:
            vault_dir = os.path.join(home_dir, ".local", "share", "mipmem", "vault")
        elif plugin_dir is not None:
            vault_dir = os.path.join(plugin_dir, "vault")
        else:
            vault_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "mipmem", "vault")

    manifest_path = os.path.join(vault_dir, "vault_manifest.json")
    vault_init = os.path.isfile(manifest_path) and not os.path.islink(manifest_path)
    if vault_init:
        try:
            with open(manifest_path, "rb") as handle:
                raw_manifest = handle.read().strip()
            if not (raw_manifest.startswith(b"{") and raw_manifest.endswith(b"}")):
                return None
        except OSError:
            vault_init = False

    from mipmem import __version__

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    if json_mode:
        vi_s = "true" if vault_init else "false"
        ce_s = "true" if config_exists else "false"
        sys.stdout.write(
            '{"success": true, '
            f'"version": "{_json_escape(__version__)}", '
            f'"python": "{py_ver}", '
            f'"vault_dir": "{_json_escape(vault_dir)}", '
            f'"vault_initialized": {vi_s}, '
            f'"config_file": "{_json_escape(config_file)}", '
            f'"config_exists": {ce_s}, '
            f'"cold_start_ms": {elapsed_ms}}}\n'
        )
    else:
        status = "initialized" if vault_init else "uninitialized"
        sys.stdout.write(
            f"mipmem {__version__} | vault={status} ({vault_dir}) | {elapsed_ms:.2f}ms\n"
        )
    return 0


_MAX_FAST_HOOK_BYTES = 4 * 1024 * 1024


def _fast_flat_skip(event: str, stripped: bytes) -> bool:
    """Detect unambiguous no-op hook payloads on flat JSON objects without importing json/re."""
    if len(stripped) > 4096 or stripped[:1] != b"{" or stripped[-1:] != b"}":
        return False
    if b"\\" in stripped or stripped.count(b"{") != 1 or stripped.count(b"}") != 1:
        return False
    if stripped.count(b'"') % 2 != 0:
        return False
    try:
        text = stripped.decode("utf-8")
    except UnicodeDecodeError:
        return False
    compact = "".join(text.split())
    if event == "pre-invocation":
        marker = '"invocationNum":'
        pos = compact.find(marker)
        if pos >= 0:
            rest = compact[pos + len(marker):]
            end = 0
            while end < len(rest) and rest[end] in "-0123456789":
                end += 1
            tok = rest[:end]
            if tok and (tok.isdigit() or (tok[0] == "-" and tok[1:].isdigit())) and int(tok) != 1:
                if end < len(rest) and rest[end] in ",}":
                    return True
    elif event == "stop":
        if (
            '"terminationReason":"TERMINAL_CUSTOM_HOOK"' in compact
            or '"continuationActive":true' in compact
            or '"isSubagent":true' in compact
        ):
            return True
    return False


# @para-doc [#csa-cli-hook-mcp-entrypoint]
def _fast_hook_dispatch(raw_args: list[str]) -> int | None:
    """Serve `mipmem hook <session-start|pre-invocation|stop>` without argparse overhead."""
    if len(raw_args) != 2 or raw_args[0] != "hook":
        return None
    event = raw_args[1]
    if event not in ("session-start", "pre-invocation", "stop"):
        return None
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    chunk = stream.read(_MAX_FAST_HOOK_BYTES + 1)
    raw_bytes = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else bytes(chunk)
    if len(raw_bytes) <= _MAX_FAST_HOOK_BYTES and _fast_flat_skip(event, raw_bytes.strip()):
        sys.stdout.write("{}\n")
        sys.stdout.flush()
        return 0
    from mipmem.adapters.hook_io import run_hook_safe

    return run_hook_safe(event, preloaded_bytes=raw_bytes)


# @para-doc [#csa-cli-foundation-router]
def main(
    argv: list[str] | tuple[str, ...] | None = None,
    *,
    boot_started: float | None = None,
) -> int:
    """Dispatch mipmem CLI commands cleanly with sub-25ms health and hook fast-paths."""
    started = boot_started if boot_started is not None else time.perf_counter()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    fast_rc = _fast_health(raw_args, started)
    if fast_rc is not None:
        return fast_rc
    hook_rc = _fast_hook_dispatch(raw_args)
    if hook_rc is not None:
        return hook_rc
    from mipmem.cli_commands import dispatch_full_cli

    return dispatch_full_cli(raw_args, started)
