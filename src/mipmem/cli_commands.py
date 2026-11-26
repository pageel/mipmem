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


def _resolve_active_paths(
    vault_override: str | None = None,
    config_override: str | Path | None = None,
) -> Any:
    """Resolve XDG/env paths lazily using CLI --config, --vault, env, or config.toml."""
    from dataclasses import replace

    from mipmem.config import load_config, resolve_paths
    from mipmem.core.diagnostics import CLI_INVALID_ARGUMENT, MipmemError

    base_paths = resolve_paths()
    if config_override is not None:
        raw_cfg = str(config_override).strip()
        if not raw_cfg:
            raise MipmemError(CLI_INVALID_ARGUMENT, "Option --config requires a file path.")
        cfg_path = Path(raw_cfg).expanduser()
        if cfg_path.is_symlink() or not cfg_path.is_file():
            raise MipmemError(
                CLI_INVALID_ARGUMENT,
                f"Config file not found: {config_override!r}.",
            )
    else:
        cfg_path = base_paths.config_file

    if vault_override is not None:
        resolved = replace(base_paths, vault_dir=Path(vault_override).expanduser())
    else:
        configured_vault: str | None = None
        if not os.environ.get("MIPMEM_VAULT_DIR", "").strip() and cfg_path.is_file():
            cfg = load_config(cfg_path)
            core_tbl = cfg.get("core", {})
            if isinstance(core_tbl, dict):
                raw_vault = core_tbl.get("vault_path")
                if isinstance(raw_vault, str) and raw_vault.strip():
                    configured_vault = raw_vault.strip()
        resolved = resolve_paths(configured_vault_dir=configured_vault)

    return replace(resolved, config_file=cfg_path)


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
    from mipmem.core.diagnostics import RotatingJsonLogger
    from mipmem.core.vault import ensure_vault_onboarding_guides, init_vault

    target_vault_arg: str | None = getattr(args, "vault", None)
    refresh_guide = bool(getattr(args, "refresh_guide", False))
    paths = _resolve_active_paths(target_vault_arg)
    logger = RotatingJsonLogger(log_path=paths.cache_dir / "mipmem.log")
    layout = init_vault(paths.vault_dir, host_id=getattr(args, "host", None), logger=logger)
    guides_written: list[str] = []
    if refresh_guide:
        guides_written = ensure_vault_onboarding_guides(
            layout.vault_root,
            refresh=True,
            logger=logger,
        )

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
    if refresh_guide:
        payload["guides_written"] = guides_written
    if json_mode:
        _emit_json(payload)
    else:
        sys.stdout.write(
            f"Initialized mipmem vault at {layout.vault_root} (host_id={layout.host_id}).\n"
        )
        if guides_written:
            sys.stdout.write(f"Refreshed guides: {', '.join(guides_written)}\n")
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

    if bool(getattr(args, "deep", False)):
        from mipmem.core.fts_recall import (
            FTS_DB_REL_PATH,
            probe_fts5_capabilities,
            recall_memory,
            sync_fts_index,
        )

        fts_ok, fts_tok = probe_fts5_capabilities()
        indexed_sessions = 0
        indexed_brainstorms = 0
        fts_query_ms = 0.0
        if fts_ok and vault_initialized:
            db_file = Path(paths.vault_dir) / FTS_DB_REL_PATH
            sync_rep = sync_fts_index(paths.vault_dir)
            indexed_sessions = sync_rep.indexed_sessions
            indexed_brainstorms = sync_rep.indexed_brainstorms
            fts_tok = sync_rep.tokenizer_mode
            if db_file.is_file():
                probe_res = recall_memory(paths.vault_dir, "mipmem probe", limit=1)
                fts_query_ms = probe_res.duration_ms
        payload["fts5_supported"] = fts_ok
        payload["fts5_tokenizer"] = fts_tok
        payload["fts5_query_ms"] = fts_query_ms
        payload["indexed_sessions"] = indexed_sessions
        payload["indexed_brainstorms"] = indexed_brainstorms

        from mipmem.config import default_config, load_config
        from mipmem.core.subapps import discover_app_delegates
        from mipmem.sync.base import read_sync_status

        cfg = load_config(paths.config_file) if paths.config_file.is_file() else default_config()
        sync_tbl = cfg.get("sync", {}) if isinstance(cfg, dict) else {}
        sync_backend = (
            str(sync_tbl.get("backend") or "none").strip().lower()
            if isinstance(sync_tbl, dict)
            else "none"
        ) or "none"
        delegates = discover_app_delegates(paths.config_dir, vault_root=paths.vault_dir)
        payload["sync_backend"] = sync_backend
        payload["last_sync_status"] = read_sync_status(paths.cache_dir)
        payload["subapp_delegates_count"] = len(delegates)

    if json_mode:
        _emit_json(payload)
    else:
        status = "initialized" if vault_initialized else "uninitialized"
        extra = ""
        if bool(getattr(args, "deep", False)):
            extra = (
                f" | fts5={payload['fts5_supported']} ({payload['fts5_tokenizer']}) "
                f"| sessions={payload['indexed_sessions']} "
                f"| brainstorms={payload['indexed_brainstorms']} "
                f"| fts_ms={payload['fts5_query_ms']:.2f}ms "
                f"| sync={payload['sync_backend']} "
                f"| delegates={payload['subapp_delegates_count']}"
            )
        sys.stdout.write(
            f"mipmem {__version__} | vault={status} ({paths.vault_dir}) "
            f"| {cold_start_ms:.2f}ms{extra}\n"
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


def _resolve_host_and_locale(
    paths: Any,
    explicit_host: str | None,
    explicit_locale: str | None,
    cfg_override: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Resolve active host_id and locale from CLI flags, config.toml, or defaults."""
    from mipmem.config import load_config
    from mipmem.core.vault import default_host_id

    if cfg_override is not None:
        cfg: dict[str, Any] = cfg_override
    else:
        cfg = load_config(paths.config_file) if paths.config_file.is_file() else {}
    core_tbl = cfg.get("core", {}) if isinstance(cfg, dict) else {}

    host_id = explicit_host
    if not host_id and isinstance(core_tbl, dict):
        cfg_host = core_tbl.get("host_id")
        if isinstance(cfg_host, str) and cfg_host.strip():
            host_id = cfg_host.strip()
    if not host_id:
        host_id = default_host_id()

    locale = explicit_locale
    if not locale and isinstance(core_tbl, dict):
        cfg_locale = core_tbl.get("locale")
        if isinstance(cfg_locale, str) and cfg_locale.strip():
            locale = cfg_locale.strip()
    if not locale:
        locale = "en"

    return host_id, locale


def _require_initialized_vault(vault_dir: Path) -> None:
    """Raise VAULT_NOT_INITIALIZED if vault_dir lacks a valid vault_manifest.json."""
    from mipmem.core.diagnostics import VAULT_NOT_INITIALIZED, MipmemError
    from mipmem.core.vault import is_vault_initialized

    if not is_vault_initialized(vault_dir):
        raise MipmemError(
            VAULT_NOT_INITIALIZED,
            f"Vault not initialized at {vault_dir}. Run 'mipmem init --host <name>'.",
        )


def _handle_index(args: argparse.Namespace, json_mode: bool) -> int:
    """Incrementally sync or rebuild .cache/global_fts.db from the Sharded Vault."""
    from mipmem.core.fts_recall import sync_fts_index

    paths = _resolve_active_paths(getattr(args, "vault", None))
    _require_initialized_vault(paths.vault_dir)
    _, locale = _resolve_host_and_locale(paths, None, getattr(args, "locale", None))
    report = sync_fts_index(
        paths.vault_dir,
        locale=locale,
        rebuild=bool(getattr(args, "rebuild", False)),
    )
    if json_mode:
        _emit_json({"success": True, "report": report.to_dict()})
    else:
        sys.stdout.write(
            f"Indexed {report.files_indexed}/{report.files_scanned} file(s) "
            f"(removed={report.files_removed}, sessions={report.indexed_sessions}, "
            f"brainstorms={report.indexed_brainstorms}, {report.duration_ms:.2f}ms)\n"
        )
    return 0


def _handle_recall(args: argparse.Namespace, json_mode: bool) -> int:
    """Execute FTS5 bigram recall and return ranked L1 memory hits."""
    from mipmem.core.fts_recall import recall_memory

    paths = _resolve_active_paths(getattr(args, "vault", None))
    _require_initialized_vault(paths.vault_dir)
    _, locale = _resolve_host_and_locale(paths, None, getattr(args, "locale", None))

    result = recall_memory(
        paths.vault_dir,
        str(args.query),
        locale=locale,
        host_id=getattr(args, "host", None),
        status=getattr(args, "status", None),
        limit=int(getattr(args, "limit", 5)),
        scope=str(getattr(args, "scope", "all")),
    )
    if json_mode:
        _emit_json({"success": True, "result": result.to_dict()})
    else:
        if not result.hits:
            sys.stdout.write("No matching memories found.\n")
        else:
            for hit in result.hits:
                sys.stdout.write(f"{hit.l1_card}\n\n")
    return 0


def _read_brainstorm_input_file(raw_file: str) -> str:
    """Safely read a regular input Markdown file bounded by MAX_BRAINSTORM_BYTES."""
    from mipmem.core.brainstorms import MAX_BRAINSTORM_BYTES
    from mipmem.core.diagnostics import CLI_INVALID_ARGUMENT, MipmemError

    candidate = Path(raw_file).expanduser()
    if candidate.is_symlink():
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Refusing to read symlink brainstorm source file: {raw_file!r}.",
        )
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Brainstorm input file not found: {raw_file!r}.",
        )
    if resolved.stat().st_size > MAX_BRAINSTORM_BYTES:
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            f"Brainstorm file exceeds {MAX_BRAINSTORM_BYTES} bytes limit.",
        )
    return resolved.read_text(encoding="utf-8")


def _handle_bs(args: argparse.Namespace, json_mode: bool) -> int:
    """Handle `mipmem bs [list|save|view|status]` commands lazily."""
    from mipmem.core.brainstorms import (
        load_brainstorm_shard,
        save_brainstorm_shard,
        update_brainstorm_status,
    )
    from mipmem.core.diagnostics import CLI_INVALID_ARGUMENT, MipmemError
    from mipmem.core.fts_recall import list_indexed_brainstorms, sync_fts_index
    from mipmem.core.mipmap import render_l1_skeleton, slice_brainstorm_l2

    paths = _resolve_active_paths(getattr(args, "vault", None))
    _require_initialized_vault(paths.vault_dir)
    action = getattr(args, "bs_action", None) or "list"

    if action == "list":
        items = list_indexed_brainstorms(
            paths.vault_dir,
            status=getattr(args, "status", None),
            host_id=getattr(args, "host", None),
            auto_sync=True,
        )
        if json_mode:
            _emit_json({"success": True, "brainstorms": items})
        else:
            for item in items:
                sys.stdout.write(
                    f"- {item['bs_id']} [{item['effective_status']}] "
                    f"({item['origin_host']}) {item['title']}\n"
                )
        return 0

    if action == "save":
        host_id, locale = _resolve_host_and_locale(
            paths, getattr(args, "host", None), None
        )
        file_arg = getattr(args, "file", None)
        content_arg = getattr(args, "content", None)
        if file_arg:
            content = _read_brainstorm_input_file(str(file_arg))
        elif content_arg is not None:
            content = str(content_arg)
        else:
            content = f"# {args.title}\n"

        raw_tags = getattr(args, "tags", None)
        tag_list = (
            [t.strip() for t in str(raw_tags).split(",") if t.strip()]
            if raw_tags
            else None
        )
        shard = save_brainstorm_shard(
            paths.vault_dir,
            str(args.title),
            content,
            host_id=host_id,
            status=str(getattr(args, "status", "open")),
            tags=tag_list,
        )
        sync_fts_index(paths.vault_dir, locale=locale)
        if json_mode:
            _emit_json({"success": True, "brainstorm": shard.to_meta_dict()})
        else:
            sys.stdout.write(
                f"Saved brainstorm {shard.bs_id} ({shard.line_count} lines) "
                f"to {shard.md_rel_path}\n"
            )
        return 0

    if action == "view":
        shard, content = load_brainstorm_shard(paths.vault_dir, str(args.bs_id))
        slice_sel = getattr(args, "slice", None)
        if bool(getattr(args, "l1", False)) and not slice_sel:
            l1 = render_l1_skeleton(shard)
            if json_mode:
                _emit_json({"success": True, "mipmap": l1.to_dict()})
            else:
                sys.stdout.write(l1.content)
            return 0
        sliced = slice_brainstorm_l2(content, selector=slice_sel, bs_id=shard.bs_id)
        if json_mode:
            _emit_json({"success": True, "slice": sliced.to_dict()})
        else:
            sys.stdout.write(sliced.content)
        return 0

    if action == "status":
        host_id, locale = _resolve_host_and_locale(
            paths, getattr(args, "host", None), None
        )
        event = update_brainstorm_status(
            paths.vault_dir,
            str(args.bs_id),
            str(args.new_status),
            actor_host=host_id,
            reason=str(getattr(args, "reason", "") or ""),
        )
        sync_fts_index(paths.vault_dir, locale=locale)
        if json_mode:
            _emit_json({"success": True, "event": event})
        else:
            sys.stdout.write(
                f"Recorded status={args.new_status} for {args.bs_id} "
                f"(actor={host_id}, event={event['event_id']})\n"
            )
        return 0

    raise MipmemError(CLI_INVALID_ARGUMENT, f"Unsupported bs action: {action!r}.")


def _handle_fleet(args: argparse.Namespace, json_mode: bool) -> int:
    """Display multi-host vault inventory, Sub-App telemetry, and cached sync status."""
    from mipmem.core.diagnostics import RotatingJsonLogger
    from mipmem.core.subapps import build_fleet_status, format_fleet_markdown

    paths = _resolve_active_paths(getattr(args, "vault", None))
    _require_initialized_vault(paths.vault_dir)
    host_id, _ = _resolve_host_and_locale(paths, getattr(args, "host", None), None)
    logger = RotatingJsonLogger(log_path=paths.cache_dir / "mipmem.log")
    fleet = build_fleet_status(
        paths.vault_dir,
        active_host_id=host_id,
        cache_dir=paths.cache_dir,
        logger=logger,
    )
    if json_mode:
        _emit_json({"success": True, "fleet": fleet})
    else:
        sys.stdout.write(format_fleet_markdown(fleet, surface="cli").rstrip() + "\n")
    return 0


def _handle_sync(args: argparse.Namespace, json_mode: bool) -> int:
    """Execute pull, push, or full conflict-free vault synchronization."""
    from mipmem.config import default_config, load_config
    from mipmem.core.diagnostics import CLI_INVALID_ARGUMENT, MipmemError, RotatingJsonLogger
    from mipmem.sync.base import is_sync_configured, resolve_sync_backend

    pull_only = bool(getattr(args, "pull_only", False))
    push_only = bool(getattr(args, "push_only", False))
    detached = bool(getattr(args, "detached", False))
    if_configured = bool(getattr(args, "if_configured", False))
    if pull_only and push_only:
        raise MipmemError(
            CLI_INVALID_ARGUMENT,
            "Options --pull-only and --push-only are mutually exclusive.",
        )

    paths = _resolve_active_paths(
        getattr(args, "vault", None),
        config_override=getattr(args, "config", None),
    )
    cfg = load_config(paths.config_file) if paths.config_file.is_file() else default_config()
    if if_configured:
        configured, backend_name, _ = is_sync_configured(cfg)
        if not configured:
            if detached:
                return 0
            if json_mode:
                _emit_json(
                    {
                        "success": True,
                        "skipped": True,
                        "reason": "sync_unconfigured",
                        "backend": backend_name,
                    }
                )
            else:
                sys.stdout.write(
                    f"Sync skipped: backend {backend_name!r} is not configured.\n"
                )
            return 0

    _require_initialized_vault(paths.vault_dir)
    host_id, locale = _resolve_host_and_locale(
        paths,
        getattr(args, "host", None),
        None,
        cfg_override=cfg,
    )
    logger = RotatingJsonLogger(log_path=paths.cache_dir / "mipmem.log")

    backend = resolve_sync_backend(
        cfg,
        vault_dir=paths.vault_dir,
        cache_dir=paths.cache_dir,
        host_id=host_id,
        locale=locale,
        logger=logger,
    )
    if pull_only:
        result = backend.pull()
    elif push_only:
        result = backend.push()
    else:
        result = backend.sync()

    if not result.ok and result.error_code:
        if detached:
            return 1
        raise MipmemError(result.error_code, result.message or "Vault sync failed.")

    if detached:
        return 0 if result.ok else 1

    if json_mode:
        _emit_json({"success": True, "result": result.to_dict()})
    else:
        sys.stdout.write(
            f"Sync [{result.backend}/{result.direction}] ok "
            f"(files={result.files_changed}, {result.duration_ms:.2f}ms)\n"
        )
    return 0


# @para-doc [#csa-cli-join-and-refresh-guide]
def _handle_join(args: argparse.Namespace, json_mode: bool) -> int:
    """Clone an existing private Git vault, enforce collision guards, index FTS5, and sync."""
    from mipmem.config import resolve_paths
    from mipmem.core.diagnostics import (
        CLI_INVALID_ARGUMENT,
        VAULT_PATH_TRAVERSAL,
        MipmemError,
    )
    from mipmem.sync.git_backend import join_git_vault

    base_paths = resolve_paths()
    raw_vault = getattr(args, "vault", None)
    if raw_vault is not None:
        raw_vault_str = str(raw_vault).strip()
        if not raw_vault_str:
            raise MipmemError(CLI_INVALID_ARGUMENT, "Option --vault requires a directory path.")
        vault_candidate = Path(raw_vault_str).expanduser()
        if vault_candidate.is_symlink():
            raise MipmemError(
                VAULT_PATH_TRAVERSAL,
                "Security block: vault root cannot be a symlink.",
            )
        target_vault = vault_candidate
    else:
        target_vault = base_paths.vault_dir

    raw_cfg = getattr(args, "config", None)
    if raw_cfg is not None:
        raw_cfg_str = str(raw_cfg).strip()
        if not raw_cfg_str:
            raise MipmemError(CLI_INVALID_ARGUMENT, "Option --config requires a file path.")
        cfg_candidate = Path(raw_cfg_str).expanduser()
        if cfg_candidate.is_symlink() or cfg_candidate.is_dir():
            raise MipmemError(
                CLI_INVALID_ARGUMENT,
                f"Invalid config file path: {raw_cfg!r}.",
            )
        target_cfg = cfg_candidate
    else:
        target_cfg = base_paths.config_file

    result = join_git_vault(
        str(args.vault_remote_url),
        vault_dir=target_vault,
        cache_dir=base_paths.cache_dir,
        config_file=target_cfg,
        host_id=getattr(args, "host", None),
        adopt_host=bool(getattr(args, "adopt_host", False)),
        locale=getattr(args, "locale", None),
        auto_sync_on_stop=not bool(getattr(args, "no_sync_on_stop", False)),
        skip_sync=bool(getattr(args, "skip_sync", False)),
    )
    payload: dict[str, Any] = {"success": True, **result.to_dict()}
    if json_mode:
        _emit_json(payload)
    else:
        sys.stdout.write(
            f"Joined vault at {result.vault_dir} as host_id={result.host_id} "
            f"(existing_hosts={len(result.existing_hosts)}, "
            f"sessions={result.indexed_sessions}, "
            f"brainstorms={result.indexed_brainstorms}, {result.duration_ms:.2f}ms)\n"
        )
    return 0


def _handle_self_update(args: argparse.Namespace, json_mode: bool) -> int:
    """Execute opt-in release verification and atomic zipapp self-update."""
    from mipmem import __version__
    from mipmem.config import resolve_paths
    from mipmem.core.diagnostics import RotatingJsonLogger
    from mipmem.core.updater import run_self_update

    paths = resolve_paths()
    logger = RotatingJsonLogger(log_path=paths.cache_dir / "mipmem.log")
    result = run_self_update(
        target_version=getattr(args, "version", None),
        check_only=bool(getattr(args, "check", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        current_version=__version__,
        logger=logger,
    )
    if json_mode:
        _emit_json({"success": True, "result": result.to_dict()})
    else:
        sys.stdout.write(f"[{result.install_mode}] {result.message}\n")
    return 0


# @para-doc [#csa-cli-fleet-sync-update]
def _register_phase4_subcommands(
    subparsers: Any,
    json_parent: argparse.ArgumentParser,
    parser_cls: type[argparse.ArgumentParser],
) -> None:
    """Register lazy Spec S4 CLI subcommands (fleet, sync, self-update)."""
    _ = parser_cls
    p_fleet = subparsers.add_parser(
        "fleet",
        parents=[json_parent],
        help="Show multi-host vault status, sub-app telemetry, and sync alerts.",
    )
    p_fleet.add_argument("--host", default=None, help="Override active host_id label.")
    p_fleet.add_argument("--vault", default=None, help="Override vault directory path.")

    p_sync = subparsers.add_parser(
        "sync",
        parents=[json_parent],
        help="Run configured single-writer vault synchronization (git or folder).",
    )
    p_sync.add_argument("--pull-only", action="store_true", help="Pull remote shards only.")
    p_sync.add_argument("--push-only", action="store_true", help="Push active host shard only.")
    p_sync.add_argument(
        "--detached",
        action="store_true",
        help="Quiet background worker mode invoked by Stop hook.",
    )
    p_sync.add_argument(
        "--if-configured",
        action="store_true",
        help="Exit 0 with a skipped result when [sync] backend is unconfigured.",
    )
    p_sync.add_argument("--config", default=None, help="Override config.toml file path.")
    p_sync.add_argument("--host", default=None, help="Explicit active host_id override.")
    p_sync.add_argument("--vault", default=None, help="Override vault directory path.")

    p_upd = subparsers.add_parser(
        "self-update",
        parents=[json_parent],
        help="Opt-in release check and verified atomic zipapp self-update.",
    )
    p_upd.add_argument(
        "--check",
        action="store_true",
        help="Check release availability and upgrade channel without modifying files.",
    )
    p_upd.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify release manifest and SHA256SUMS without swapping binary.",
    )
    p_upd.add_argument(
        "--version",
        default=None,
        help="Target release version tag (e.g. v1.0.0).",
    )

    p_join = subparsers.add_parser(
        "join",
        parents=[json_parent],
        help="Clone a private vault, check host_id collisions, rebuild FTS5, and sync.",
    )
    p_join.add_argument(
        "vault_remote_url",
        help="Private vault Git remote URL (https://, ssh://, git@...) or local repo path.",
    )
    p_join.add_argument("--host", default=None, help="Explicit host_id for this machine.")
    p_join.add_argument(
        "--adopt-host",
        action="store_true",
        help="Reclaim an existing hosts/<host_id>/ shard after OS reinstall.",
    )
    p_join.add_argument(
        "--locale",
        choices=("en", "vi"),
        default=None,
        help="Lexicon locale stored in config.toml.",
    )
    p_join.add_argument(
        "--no-sync-on-stop",
        action="store_true",
        help="Set sync.auto_sync_on_stop=false in config.toml.",
    )
    p_join.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip initial pull/push after clone and FTS5 index rebuild.",
    )
    p_join.add_argument("--vault", default=None, help="Override target local vault directory.")
    p_join.add_argument("--config", default=None, help="Override target config.toml path.")


# @para-doc [#csa-cli-memory-commands]
def _register_memory_subcommands(
    subparsers: Any,
    json_parent: argparse.ArgumentParser,
    parser_cls: type[argparse.ArgumentParser],
) -> None:
    """Register lazy Spec S2 CLI subcommands (index, recall, bs) on the root parser."""
    p_idx = subparsers.add_parser(
        "index",
        parents=[json_parent],
        help="Incrementally sync or rebuild the local FTS5 search index.",
    )
    p_idx.add_argument("--rebuild", action="store_true", help="Drop and rebuild global_fts.db.")
    p_idx.add_argument("--locale", default=None, help="Override lexicon locale (en|vi).")
    p_idx.add_argument("--vault", default=None, help="Override vault directory path.")

    p_rec = subparsers.add_parser(
        "recall",
        parents=[json_parent],
        help="Search cross-host memory using SQLite FTS5 BM25 Bigram recall.",
    )
    p_rec.add_argument("query", help="Search query text (EN or VI, accented or unaccented).")
    p_rec.add_argument("--host", default=None, help="Filter hits by origin host_id.")
    p_rec.add_argument("--status", default=None, help="Filter brainstorm hits by status.")
    p_rec.add_argument("--limit", type=int, default=5, help="Maximum hits to return.")
    p_rec.add_argument(
        "--scope",
        default="all",
        choices=("all", "sessions", "brainstorms"),
        help="Search scope.",
    )
    p_rec.add_argument("--locale", default=None, help="Override lexicon locale (en|vi).")
    p_rec.add_argument("--vault", default=None, help="Override vault directory path.")

    p_bs = subparsers.add_parser(
        "bs",
        parents=[json_parent],
        help="Manage lossless brainstorm shards and surgical L2 slices.",
    )
    p_bs.add_argument("--vault", default=None, help="Override vault directory path.")
    bs_subs = p_bs.add_subparsers(dest="bs_action", parser_class=parser_cls)

    p_bs_list = bs_subs.add_parser("list", parents=[json_parent], help="List brainstorm shards.")
    p_bs_list.add_argument("--status", default=None, help="Filter by effective LWW status.")
    p_bs_list.add_argument("--host", default=None, help="Filter by origin host_id.")
    p_bs_list.add_argument("--vault", default=None, help="Override vault directory path.")

    p_bs_save = bs_subs.add_parser("save", parents=[json_parent], help="Save a brainstorm shard.")
    p_bs_save.add_argument("title", help="Brainstorm title.")
    p_bs_save.add_argument("--file", default=None, help="Path to source Markdown file.")
    p_bs_save.add_argument("--content", default=None, help="Inline Markdown content.")
    p_bs_save.add_argument("--status", default="open", help="Initial status (default: open).")
    p_bs_save.add_argument("--tags", default=None, help="Comma-separated tags.")
    p_bs_save.add_argument("--host", default=None, help="Explicit origin host_id.")
    p_bs_save.add_argument("--vault", default=None, help="Override vault directory path.")

    p_bs_view = bs_subs.add_parser("view", parents=[json_parent], help="View or slice brainstorm.")
    p_bs_view.add_argument("bs_id", help="Brainstorm identifier.")
    p_bs_view.add_argument(
        "--slice",
        default=None,
        help="Surgical L2 selector (§k or #Lstart-Lend).",
    )
    p_bs_view.add_argument("--l1", action="store_true", help="Render L1 structural card.")
    p_bs_view.add_argument("--vault", default=None, help="Override vault directory path.")

    p_bs_st = bs_subs.add_parser(
        "status",
        parents=[json_parent],
        help="Record a conflict-free status change in actor host journal.",
    )
    p_bs_st.add_argument("bs_id", help="Brainstorm identifier.")
    p_bs_st.add_argument(
        "new_status",
        help="New status (open|decided|implementing|shipped|archived).",
    )
    p_bs_st.add_argument("--reason", default="", help="Optional status change reason.")
    p_bs_st.add_argument("--host", default=None, help="Explicit actor host_id.")
    p_bs_st.add_argument("--vault", default=None, help="Override vault directory path.")


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
    p_init.add_argument(
        "--refresh-guide",
        action="store_true",
        help="Regenerate host-agnostic vault AGENTS.md and README.md guides.",
    )

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
    p_health.add_argument(
        "--deep",
        action="store_true",
        help="Probe SQLite FTS5 capability, tokenizer, index counts, and query latency.",
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

    p_hook = subparsers.add_parser(
        "hook",
        help="Execute Antigravity plugin lifecycle hooks over stdin/stdout JSON.",
    )
    p_hook.add_argument(
        "hook_event",
        choices=("session-start", "pre-invocation", "stop"),
        help="Lifecycle hook event name.",
    )

    p_mcp = subparsers.add_parser(
        "mcp",
        help="Run the universal local stdio MCP JSON-RPC 2.0 server.",
    )
    p_mcp.add_argument("--vault", default=None, help="Override vault directory path.")

    _register_memory_subcommands(subparsers, json_parent, _CliArgumentParser)
    _register_phase4_subcommands(subparsers, json_parent, _CliArgumentParser)

    return parser


def _log_detached_cli_error(raw_args: list[str], exc: Exception) -> None:
    """Persist a redacted CP-SYN-01 failure record when a --detached invocation errors."""
    try:
        from mipmem.config import resolve_paths
        from mipmem.core.diagnostics import (
            CP_SYN_01,
            SYNC_TRANSPORT_FAILED,
            MipmemError,
            RotatingJsonLogger,
        )
        from mipmem.core.redactor import redact_text, register_default_scrubber

        register_default_scrubber(redact_text)
        paths = resolve_paths()
        logger = RotatingJsonLogger(log_path=paths.cache_dir / "mipmem.log")
        err_code = exc.code if isinstance(exc, MipmemError) else SYNC_TRANSPORT_FAILED
        err_msg = (
            exc.message
            if isinstance(exc, MipmemError)
            else (str(exc) or exc.__class__.__name__)
        )
        logger.log_event(
            "ERROR",
            "cli",
            CP_SYN_01,
            error_code=err_code,
            details={
                "direction": "detached_cli",
                "ok": False,
                "exception": exc.__class__.__name__,
                "message": err_msg,
                "argv_len": len(raw_args),
            },
        )
    except Exception:
        pass


def dispatch_full_cli(raw_args: list[str], boot_started: float) -> int:
    """Parse full argparse CLI arguments and handle Spec S1/S2 error envelopes."""
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
        if cmd == "index":
            return _handle_index(args, json_mode)
        if cmd == "recall":
            return _handle_recall(args, json_mode)
        if cmd == "bs":
            return _handle_bs(args, json_mode)
        if cmd == "fleet":
            return _handle_fleet(args, json_mode)
        if cmd == "sync":
            return _handle_sync(args, json_mode)
        if cmd == "join":
            return _handle_join(args, json_mode)
        if cmd == "self-update":
            return _handle_self_update(args, json_mode)
        if cmd == "hook":
            from mipmem.adapters.hook_io import run_hook_safe

            return run_hook_safe(str(args.hook_event))
        if cmd == "mcp":
            from mipmem.mcp.server import run_mcp_server

            vault_override = getattr(args, "vault", None)
            return run_mcp_server(vault_root=vault_override)
        return 0
    except Exception as exc:
        from mipmem.core.diagnostics import MipmemError

        detached_mode = "--detached" in raw_args
        if detached_mode:
            _log_detached_cli_error(raw_args, exc)

        if isinstance(exc, MipmemError):
            if json_mode:
                _emit_json(exc.to_dict())
            else:
                sys.stderr.write(f"Error [{exc.code}]: {exc.message}\n")
            return 1
        if detached_mode:
            return 1
        raise
