"""Full CLI subcommand handlers loaded lazily outside the sub-25ms health fast-path."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


def _emit_json(payload: dict[str, Any]) -> None:
    """Write a single-line JSON payload to stdout cleanly."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _resolve_active_paths(vault_override: str | None = None) -> Any:
    """Resolve XDG/env paths lazily using CLI --vault, env, or config.toml."""
    from dataclasses import replace

    from mipmem.config import load_config, resolve_paths

    if vault_override is not None:
        base_paths = resolve_paths()
        return replace(base_paths, vault_dir=Path(vault_override).expanduser())

    configured_vault: str | None = None
    if not os.environ.get("MIPMEM_VAULT_DIR", "").strip():
        probe_paths = resolve_paths()
        if probe_paths.config_file.is_file():
            cfg = load_config(probe_paths.config_file)
            core_tbl = cfg.get("core", {})
            if isinstance(core_tbl, dict):
                raw_vault = core_tbl.get("vault_path")
                if isinstance(raw_vault, str) and raw_vault.strip():
                    configured_vault = raw_vault.strip()
    return resolve_paths(configured_vault_dir=configured_vault)


def _coerce_config_value(
    section: str,
    key: str,
    raw_value: str,
    schema: dict[str, Any],
) -> Any:
    """Validate and safely coerce a CLI string value to match default_config() types."""
    from mipmem.core.diagnostics import CLI_INVALID_ARGUMENT, MipmemError

    full_key = f"{section}.{key}"
    section_table = schema.get(section)
    if not isinstance(section_table, dict) or key not in section_table:
        raise MipmemError(CLI_INVALID_ARGUMENT, f"Unknown configuration key: {full_key!r}.")

    sample = section_table[key]
    if isinstance(sample, bool):
        lowered = raw_value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Expected boolean ('true'/'false') for {full_key}, got {raw_value!r}.",
        )

    if isinstance(sample, int):
        try:
            parsed_int = int(raw_value.strip())
        except ValueError as exc:
            raise MipmemError(
                CLI_INVALID_ARGUMENT,
                f"Expected integer for {full_key}, got {raw_value!r}.",
            ) from exc
        if section == "budget" and parsed_int < 0:
            raise MipmemError(
                CLI_INVALID_ARGUMENT,
                f"Budget value for {full_key} must be non-negative.",
            )
        return parsed_int

    if isinstance(sample, float):
        try:
            parsed_float = float(raw_value.strip())
        except ValueError as exc:
            raise MipmemError(
                CLI_INVALID_ARGUMENT,
                f"Expected float for {full_key}, got {raw_value!r}.",
            ) from exc
        if full_key == "budget.topic_shift_novelty" and not (0.0 <= parsed_float <= 1.0):
            raise MipmemError(
                CLI_INVALID_ARGUMENT,
                f"{full_key} must be between 0.0 and 1.0.",
            )
        return parsed_float

    if isinstance(sample, list):
        trimmed = raw_value.strip()
        if not trimmed or trimmed == "[]":
            return []
        if trimmed.startswith("["):
            try:
                decoded = json.loads(trimmed)
            except json.JSONDecodeError as exc:
                raise MipmemError(
                    CLI_INVALID_ARGUMENT,
                    f"Invalid list literal for {full_key}: {exc}.",
                ) from exc
            if not isinstance(decoded, list) or not all(isinstance(x, str) for x in decoded):
                raise MipmemError(
                    CLI_INVALID_ARGUMENT,
                    f"Expected list of strings for {full_key}.",
                )
            return list(decoded)
        return [item.strip() for item in trimmed.split(",") if item.strip()]

    if full_key == "core.host_id":
        from mipmem.core.vault import validate_host_id

        return validate_host_id(raw_value)
    if full_key == "core.locale" and raw_value not in ("en", "vi"):
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Unsupported locale {raw_value!r}: expected 'en' or 'vi'.",
        )
    if full_key == "sync.backend" and raw_value not in ("none", "git", "folder"):
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Unsupported sync backend {raw_value!r}: expected 'none', 'git', or 'folder'.",
        )
    if full_key == "workspace_policies.default" and raw_value not in (
        "active",
        "passive",
        "disabled",
    ):
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Unsupported workspace policy {raw_value!r}.",
        )
    return raw_value


def _handle_init(args: argparse.Namespace, json_mode: bool) -> int:
    """Initialize config.toml and sharded multi-host vault layout."""
    from mipmem.config import default_config, load_config, save_config
    from mipmem.core.vault import init_vault

    target_vault_arg: str | None = getattr(args, "vault", None)
    paths = _resolve_active_paths(target_vault_arg)
    layout = init_vault(paths.vault_dir, host_id=getattr(args, "host", None))

    cfg = load_config(paths.config_file) if paths.config_file.is_file() else default_config()
    core_tbl = cfg.setdefault("core", {})
    if isinstance(core_tbl, dict):
        core_tbl["host_id"] = layout.host_id
        core_tbl["vault_path"] = str(layout.vault_root)
    saved_cfg = save_config(cfg, paths.config_file)

    payload: dict[str, Any] = {
        "success": True,
        "host_id": layout.host_id,
        "vault_dir": str(layout.vault_root),
        "config_file": str(saved_cfg),
    }
    if json_mode:
        _emit_json(payload)
    else:
        sys.stdout.write(
            f"Initialized mipmem vault at {layout.vault_root} (host_id={layout.host_id}).\n"
        )
    return 0


def _handle_config(args: argparse.Namespace, json_mode: bool) -> int:
    """Show, read, or safely update typed settings in config.toml."""
    from mipmem.config import default_config, load_config, resolve_paths, save_config
    from mipmem.core.diagnostics import CLI_INVALID_ARGUMENT, MipmemError

    paths = resolve_paths()
    action = getattr(args, "config_action", None) or "show"
    cfg = load_config(paths.config_file)

    if action == "show":
        if json_mode:
            _emit_json({"success": True, "config_file": str(paths.config_file), "config": cfg})
        else:
            sys.stdout.write(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
        return 0

    raw_key = str(getattr(args, "key", "")).strip()
    if "." not in raw_key:
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Invalid config key {raw_key!r}: expected <section>.<key>.",
        )
    section, key = raw_key.split(".", 1)
    schema = default_config()

    if action == "get":
        sec_tbl = cfg.get(section)
        schema_sec = schema.get(section)
        if (
            not isinstance(schema_sec, dict)
            or key not in schema_sec
            or not isinstance(sec_tbl, dict)
            or key not in sec_tbl
        ):
            raise MipmemError(CLI_INVALID_ARGUMENT, f"Unknown configuration key: {raw_key!r}.")
        val = sec_tbl[key]
        if json_mode:
            _emit_json({"success": True, "key": raw_key, "value": val})
        else:
            sys.stdout.write(f"{val}\n")
        return 0

    if action == "set":
        coerced = _coerce_config_value(section, key, str(args.value), schema)
        target_section = cfg.setdefault(section, {})
        if not isinstance(target_section, dict):
            raise MipmemError(CLI_INVALID_ARGUMENT, f"Invalid config section: {section!r}.")
        target_section[key] = coerced
        saved_path = save_config(cfg, paths.config_file)
        if json_mode:
            _emit_json(
                {
                    "success": True,
                    "key": raw_key,
                    "value": coerced,
                    "config_file": str(saved_path),
                }
            )
        else:
            sys.stdout.write(f"Updated {raw_key} = {coerced!r} in {saved_path}.\n")
        return 0

    raise MipmemError(CLI_INVALID_ARGUMENT, f"Unsupported config action: {action!r}.")


def _handle_health_full(
    args: argparse.Namespace,
    json_mode: bool,
    boot_started: float,
) -> int:
    """Handle health checks requiring config.toml lookup or --require-vault validation."""
    from mipmem import __version__

    paths = _resolve_active_paths(getattr(args, "vault", None))
    manifest_path = Path(paths.vault_dir) / "vault_manifest.json"
    vault_initialized = manifest_path.is_file() and not manifest_path.is_symlink()

    if vault_initialized:
        try:
            json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            vault_initialized = False

    if getattr(args, "require_vault", False) and not vault_initialized:
        from mipmem.core.diagnostics import VAULT_NOT_INITIALIZED, MipmemError

        raise MipmemError(
            VAULT_NOT_INITIALIZED,
            f"Vault not initialized at {paths.vault_dir}. Run 'mipmem init --host <name>'.",
        )

    cold_start_ms = round((time.perf_counter() - boot_started) * 1000.0, 3)
    payload: dict[str, Any] = {
        "success": True,
        "version": __version__,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "vault_dir": str(paths.vault_dir),
        "vault_initialized": vault_initialized,
        "config_file": str(paths.config_file),
        "config_exists": paths.config_file.is_file(),
        "cold_start_ms": cold_start_ms,
    }
    if json_mode:
        _emit_json(payload)
    else:
        status = "initialized" if vault_initialized else "uninitialized"
        sys.stdout.write(
            f"mipmem {__version__} | vault={status} ({paths.vault_dir}) | {cold_start_ms:.2f}ms\n"
        )
    return 0


def _handle_migrate(args: argparse.Namespace, json_mode: bool) -> int:
    """Execute dry-run or idempotent apply migration from a legacy v2 directory."""
    from mipmem.core.migrate_v2 import migrate_from_v2

    paths = _resolve_active_paths(getattr(args, "vault", None))
    report = migrate_from_v2(
        args.from_v2,
        paths.vault_dir,
        host_id=getattr(args, "host", None),
        dry_run=not bool(getattr(args, "apply", False)),
        force=bool(getattr(args, "force", False)),
    )
    report_dict = report.to_dict()
    if json_mode:
        _emit_json({"success": True, "report": report_dict})
    else:
        mode_label = "DRY-RUN" if report.dry_run else "APPLIED"
        sys.stdout.write(
            f"[{mode_label}] sessions={report.sessions_copied}/{report.sessions_found} "
            f"brainstorms={report.brainstorms_copied}/{report.brainstorms_found} "
            f"skipped={report.skipped_existing} conflicts={report.conflicts_skipped}\n"
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser with uniform --json support."""

    class _CliArgumentParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:  # type: ignore[override]
            from mipmem.core.diagnostics import CLI_INVALID_ARGUMENT, MipmemError

            raise MipmemError(CLI_INVALID_ARGUMENT, message)

    json_parent = _CliArgumentParser(add_help=False)
    json_parent.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
        help="Emit structured JSON on stdout.",
    )

    parser = _CliArgumentParser(
        prog="mipmem",
        description="Zero-dependency 4-layer mipmapped cognitive memory engine.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=False,
        help="Emit structured JSON on stdout.",
    )
    subparsers = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)

    p_init = subparsers.add_parser("init", parents=[json_parent], help="Initialize host vault.")
    p_init.add_argument("--host", default=None, help="Explicit host identifier.")
    p_init.add_argument("--vault", default=None, help="Override vault directory path.")

    p_cfg = subparsers.add_parser("config", parents=[json_parent], help="Manage config.toml.")
    cfg_subs = p_cfg.add_subparsers(dest="config_action", parser_class=_CliArgumentParser)
    cfg_subs.add_parser("show", parents=[json_parent], help="Show active configuration.")
    p_cfg_get = cfg_subs.add_parser("get", parents=[json_parent], help="Get one setting.")
    p_cfg_get.add_argument("key", help="Dotted key (<section>.<key>).")
    p_cfg_set = cfg_subs.add_parser("set", parents=[json_parent], help="Set one setting.")
    p_cfg_set.add_argument("key", help="Dotted key (<section>.<key>).")
    p_cfg_set.add_argument("value", help="Value string coerced to key schema type.")

    p_health = subparsers.add_parser("health", parents=[json_parent], help="Check runtime health.")
    p_health.add_argument("--vault", default=None, help="Override vault directory path.")
    p_health.add_argument(
        "--require-vault",
        action="store_true",
        help="Fail with VAULT_NOT_INITIALIZED if vault is not initialized.",
    )

    p_mig = subparsers.add_parser(
        "migrate",
        parents=[json_parent],
        help="Migrate from a legacy v2 flat memory directory.",
    )
    p_mig.add_argument("--from-v2", required=True, help="Path to legacy v2 memory directory.")
    p_mig.add_argument("--vault", default=None, help="Override destination vault directory.")
    p_mig.add_argument("--host", default=None, help="Explicit target host identifier.")
    p_mig.add_argument("--apply", action="store_true", help="Copy files into the sharded vault.")
    p_mig.add_argument("--force", action="store_true", help="Overwrite conflicting target files.")

    return parser


def dispatch_full_cli(raw_args: list[str], boot_started: float) -> int:
    """Parse full argparse CLI arguments and handle Spec S1 error envelopes."""
    json_mode = "--json" in raw_args
    parser = _build_parser()
    try:
        args = parser.parse_args(raw_args)
        json_mode = json_mode or bool(getattr(args, "json_output", False))
        cmd = getattr(args, "command", None)
        if cmd is None:
            if json_mode:
                from mipmem import __version__

                _emit_json({"success": True, "version": __version__, "command": None})
            else:
                parser.print_help()
            return 0
        if cmd == "init":
            return _handle_init(args, json_mode)
        if cmd == "config":
            return _handle_config(args, json_mode)
        if cmd == "health":
            return _handle_health_full(args, json_mode, boot_started)
        if cmd == "migrate":
            return _handle_migrate(args, json_mode)
        return 0
    except Exception as exc:
        from mipmem.core.diagnostics import MipmemError

        if isinstance(exc, MipmemError):
            if json_mode:
                _emit_json(exc.to_dict())
            else:
                sys.stderr.write(f"Error [{exc.code}]: {exc.message}\n")
            return 1
        raise
