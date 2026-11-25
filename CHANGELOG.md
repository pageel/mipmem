# Changelog

<!-- ⚠️ SOURCE-VERIFIED — Cross-referenced with src/mipmem/, plugin.json, hooks.json, mcp_config.json, scripts/, .github/workflows/ on 2026-11-25 -->

All notable changes to `mipmem` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-25

### Added

- **Per-Host Private Sub-Apps Drop-In Contract (`mipmem.core.subapps`):**
  - Added validated per-host telemetry reader (`load_subapp_telemetry()`) supporting `detail_file` (with legacy `detail_path` fallback) and strict `Path.resolve().is_relative_to(host_dir)` anti-traversal guards.
  - Added token-budgeted `L0 Radar` presenter (`format_l0_telemetry_lines()`) and unified fleet aggregator (`build_fleet_status()`, `format_fleet_markdown()`), surfacing real telemetry and cached sync alerts across `L0`, `/mem fleet`, `mipmem fleet --json`, and `mem_fleet_status()`.
  - Added RCE-safe local `apps.d` command delegator (`discover_app_delegates()`, `run_app_delegate()`) reading strictly from `<config_dir>/apps.d/*.json`, refusing Vault-embedded configs, untrusted UIDs, and `0o022` permissions, enforcing `shell=False` and `timeout_sec <= 15s`, redacting outputs, and blocking collisions with core `/mem` subcommands (`CP-SUB-01`).
- **Multi-Host Conflict-Free Vault Sync (`mipmem.sync`):**
  - Added lightweight `sync` package marker, `SyncBackend` `Protocol`, `SyncResult`, `[sync].folder_path` config key (backward-compatible), exclusive `fcntl.flock` helper (`acquire_sync_lock()`), and atomic `<cache_dir>/sync_status.json` telemetry store in `src/mipmem/sync/base.py`.
  - Added conflict-free `GitSyncBackend` (`src/mipmem/sync/git_backend.py`) with `GIT_TERMINAL_PROMPT=0`, single-writer staging, `git pull --rebase --autostash`, automatic `git rebase --abort` + `SYNC_CONFLICT_DETECTED` alert persistence, and detached `Stop`-hook background sync worker (`spawn_detached_sync()`, `0ms` UI wait).
  - Added single-writer `FolderSyncBackend` (`src/mipmem/sync/folder_backend.py`) for Syncthing and shared local directories with nested-vault and symlink-escape guards (`CP-SYN-01`), plus `/mem sync` in `adapters/slash_mem.py`.
- **Opt-In Atomic Self-Update, Lazy CLI Subcommands & Clean-Room/Release Auditors:**
  - Added install-mode detector (`zipapp`, `uv`, `pipx`, `pip`, `plugin`, `git-dev`), HTTPS allowlist guard to `pageel/mipmem` GitHub Releases, `SHA256SUMS` verifier, `ZipFile.testzip()` + pre-swap `health --json` smoke check, and atomic `os.replace()` updater (`src/mipmem/core/updater.py::run_self_update`, `CP-UPD-01`).
  - Added lazy CLI subcommands `fleet`, `sync`, and `self-update` plus expanded `health --deep` diagnostics (`sync_backend`, `last_sync_status`, `subapp_delegates_count`) in `src/mipmem/cli_commands.py` while keeping fast-path `cli.py` `< 25ms`.
  - Extended `scripts/check_cleanroom.py` with `scan_git_history()` (`--git-history`) and `.whl`/`.tar.gz` dist scanning (`--dist-dir`), and added `scripts/verify_release.py` (`validate_release_readiness()`).
- **Dual-Channel `v1.0.0` Packaging, OIDC Workflows & Bilingual Docs:**
  - Updated `pyproject.toml` (`version = "1.0.0"`, `Pageel Contributors` zero-PII metadata, `keywords`, `classifiers`, `[project.urls]`), `src/mipmem/__init__.py`, `plugin.json`, and rebuilt deterministic `bin/mipmem.pyz` (`100755`).
  - Added `.github/workflows/release.yml` (GitHub Release provenance attestation + `SHA256SUMS` + PyPI OIDC Trusted Publishing with PEP 740 attestations and zero static tokens) and upgraded `.github/workflows/ci.yml` (`src/mipmem/sync/*.py`, `verify_release.py`, and automatic `macos-latest` smoke runs for public repos).
  - Added Vietnamese companion guide `README.vi.md` and updated `README.md` with all 4 `v1.0.0` install channels, Sub-Apps, Sync, and opt-in self-update workflows.
  - Extended `core/diagnostics.py` with `S4_ERROR_CODES` (`7` codes, total `25`) and `CP-SUB-01` / `CP-SYN-01` / `CP-UPD-01`.

## [0.3.0] - 2026-11-22

### Added

- **Dual Antigravity CLI/IDE Transcript Adapters (`mipmem.adapters`):**
  - Added `NormalizedConversation`, `TranscriptAdapter` `Protocol`, and `detect_surface_from_path()` (`cli` | `ide` | `generic`) in `src/mipmem/adapters/base.py`.
  - Added read-only Antigravity CLI SQLite harvester (`src/mipmem/adapters/agy_cli.py::extract_cli_conversation`) opening `conversation_summaries.db` strictly with `mode=ro` (`timeout=2.0`), filtering subagents (`nesting_depth > 0` or non-empty `parent_conversation_id`), supporting JSONL fallback, and logging `CP-ADP-01`.
  - Added Antigravity IDE JSONL harvester (`src/mipmem/adapters/agy_ide.py::extract_ide_conversation`) extracting `USER_INPUT`, `PLANNER_RESPONSE`, and `edited_files`/`touched_files`, lazily expanding steps with `truncated_fields` from `transcript_full.jsonl`, tolerating corrupted JSON lines, and intentionally skipping `artifactDirectoryPath` auto-scans.
- **Unified `user_markdown` Hook Engine, `/mem` Dispatcher & Fast Hook Router:**
  - Added `render_user_markdown_response()`, `render_system_injection()`, bounded `4 MB` stdin reader, and fail-open `run_hook_safe()` in `src/mipmem/adapters/hook_io.py` (`CP-HK-01`).
  - Added shell-free local `/mem` command dispatcher (`src/mipmem/adapters/slash_mem.py::dispatch_slash_mem`) supporting `help`, `status`, `recall`, `list`, `bs [list|save|view|status]`, `config [show|get|set]`, and `handoff` with surface-aware GFM table widths and safe UTF-8 file guards (`<= 2 MB`, non-symlink, non-binary).
  - Added `handle_session_start()` (`L0 <= 300` on `active`, `{}` on `passive`/`disabled`), `handle_pre_invocation()` (0-LLM-token `/mem` intercept via `metadata.user_markdown` + `terminationBehavior: "terminate"` on both CLI & IDE; active-only `L1 <= 450` Topic-Shift/Intent Gate), and `handle_stop()` (skipping `TERMINAL_CUSTOM_HOOK`, `continuationActive`, `disabled`, and subagents; redacting secrets; idempotent session writes; `PRIMARY_WORK` `latest_handoff.md` updates; incremental FTS5 sync).
  - Added sub-25ms fast hook router (`src/mipmem/cli.py::_fast_hook_dispatch`) bypassing `argparse`/`json`/`sqlite3` on no-op hook skips while keeping `_fast_health()` untouched.
- **Universal Local Memory MCP Server (`mipmem mcp`):**
  - Added stdlib JSON-RPC 2.0 transport (`src/mipmem/mcp/server.py::StdioJsonRpcTransport` & `run_mcp_server`) auto-detecting both `NDJSON` and `Content-Length` stdio framing with a `4 MB` message cap, standard `-32700/-32600/-32601/-32602/-32603` error codes, and strict `stdout` purity (`CP-MCP-01`).
  - Added 6 lazy local MCP tools (`src/mipmem/mcp/tools.py::McpToolRegistry`): `mem_recall`, `mem_bs_list`, `mem_bs_slice`, `mem_bs_save` (inline content or user-confirmed `file_path` with safety guards + redaction), `mem_handoff`, and `mem_fleet_status` (`subapp_telemetry: []`), wired via `mipmem mcp` in `cli_commands.py`.
- **Isolated Native Antigravity Plugin Bundle & CI Validation:**
  - Added Native Antigravity Plugin root files (`plugin.json`, flat-array `hooks.json`, relative `mcp_config.json`, plain-Markdown `rules/AGENTS.md` enforcing opt-in artifact capture confirmation, and `skills/mem/SKILL.md`) plus automated validator (`src/mipmem/adapters/plugin_manifest.py::validate_plugin_bundle`).
  - Extended `core/diagnostics.py` with `S3_ERROR_CODES` (`7` codes, total `18`) and `CP-HK-01` / `CP-ADP-01` / `CP-MCP-01`; updated `.github/workflows/ci.yml` to compile-check `adapters/` & `mcp/` and enforce `assert_valid_plugin_bundle()`.

## [0.2.0] - 2026-11-20

### Added

- **Bilingual i18n Lexicons & Vietnamese `đ/Đ` FTS5 Folding (`mipmem.i18n`):**
  - Added `LocaleLexicon` registry (`get_lexicon("en"|"vi")`), anchor token extraction, adjacent bigram builder, and bilingual intent/brainstorm cues (`src/mipmem/i18n/{__init__,en,vi}.py`).
  - Added `fold_vietnamese_d()` (`NFC` + `đ→d`, `Đ→D`) and `normalize_vi_text()` so unaccented Vietnamese queries (`"da may"`, `"dong bo"`, `"dinh tuyen"`) match accurately on SQLite FTS5 `unicode61 remove_diacritics 2` while preserving accented display text in metadata tables.
- **Canonical Sessions, Dual-Anchor Salience & Cross-Host Workstreams:**
  - Added Canonical Session Markdown Frontmatter `v1` parser/formatter (`mipmem.core.sessions`) with agent-agnostic `surface` values (`cli|ide|mcp|generic`), backward-compatible plain-Markdown fallback for migrated `v2` sessions, and redacted atomic writes via `vault.py`.
  - Added Dual-Anchor Salience scorer (`mipmem.core.salience.score_session_salience`) separating `PRIMARY_WORK` vs `UTILITY_MICRO` with an anti-abuse `+25` Brainstorm Bonus.
  - Added deterministic cross-host Workstream auto-threading (`mipmem.core.workstreams.thread_workstreams`) using file/title Jaccard clustering and local compaction to `.cache/merged_compacted_memory.md`.
- **Fence-Aware Brainstorm AST, Sharded Store & 4-Layer Mipmap Engine:**
  - Added stateful Fence-Aware Markdown AST parser (`mipmem.core.brainstorms.parse_markdown_ast`) ignoring `#` inside ```` ``` ```` / `~~~` blocks and skipping a single leading `H1` document title so `§1` targets the first `H2` body section.
  - Added `save_brainstorm_shard()`, backward-compatible `.meta.json` reader (9-field `v0.1.0` sidecars computed on-the-fly), and conflict-free cross-host `update_brainstorm_status()` writing solely to the actor host's `mutations.jsonl`.
  - Added zero-dep `estimate_tokens()`, fence-safe token-budgeted `render_l0_radar()` (`<= 300`), `render_l1_skeleton()` (`<= 450`, `CP-MIP-01`), and surgical `slice_brainstorm_l2()` (`§k`, `#Lstart-Lend`, and full `L3`) in `mipmem.core.mipmap`.
- **Incremental SQLite FTS5 Engine, Bigram Recall, Topic-Shift Gate & Lazy CLI:**
  - Added incremental self-healing FTS5 indexer (`mipmem.core.fts_recall.sync_fts_index`, `CP-IDX-01`) implementing the 5-table + 2-FTS5-table + 3-index `.cache/global_fts.db` Read Model with `PRAGMA user_version=1`, `busy_timeout=3000`, `WAL → DELETE` fallback, `remove_diacritics 2 → unicode61` fallback, racy-safe directory cursor pruning (`< 10ms` incremental sync on 500 sessions), and deterministic CRDT-lite LWW projection (`MAX((ts_utc, actor_host, event_id))`).
  - Added `RecallBackend` `Protocol`, `SqliteFtsRecallBackend`, and `recall_memory()` (`CP-FTS-01`) with FTS5 syntax sanitization, Anchor Gate, 6-column `bm25()`, Bigram `NEAR` (`x3.5`), salience/recency re-ranking (`< 5ms` p99 on 500 sessions + 50 brainstorms), and prefix fallback.
  - Added per-conversation Topic-Shift Novelty Gate (`evaluate_topic_shift`, LRU `64` atomic JSON persistence) and extended `core/diagnostics.py` with `S2_ERROR_CODES` (`5` codes) and `CP-IDX-01` / `CP-FTS-01` / `CP-MIP-01`.
  - Added lazy CLI subcommands (`index`, `recall`, `bs [list|save|view|status]`, and `health --deep`) in `mipmem.cli_commands` while keeping `mipmem.cli::_fast_health()` untouched and `< 25ms`.

## [0.1.0] - 2026-11-18

### Added

- **Diagnostics & Clean-Room Foundation:**
  - Added rotating JSONL diagnostics logger (`mipmem.core.diagnostics.RotatingJsonLogger`, `2 MB x 2` backups) with canonical error taxonomy and JSON CLI error envelopes.
  - Added clean-room static scanner (`scripts/check_cleanroom.py`) inspecting source trees and `.pyz`/`.zip` archives for secrets, personal paths, forbidden binaries/databases, bytecode artifacts, and disallowed markers.
- **Two-Layer Pre-Write Secret Redactor & Configuration:**
  - Added pre-write secret scrubber (`mipmem.core.redactor.redact_text`) and `$HOME` path normalization integrated into all Vault writes and diagnostic logs (`CP-RED-01`).
  - Added unified XDG/environment directory resolver, Python `3.9–3.13` zero-dependency TOML codec (`tomllib` + minimal subset fallback), and Most-Restrictive-Wins 3-mode Workspace Policy evaluator (`mipmem.config`).
- **Sharded Multi-Host Vault & Mutation Journal:**
  - Added host-isolated Vault initializer (`init_vault`), symlink-aware traversal and single-writer ownership guards (`atomic_write_vault_file`), and append-only CRDT-lite JSONL journal appender (`append_mutation_event`, `CP-VLT-01`) with cross-platform file locking and NTFS/WSL `chmod` fallbacks.
- **Legacy v2 Migrator, Lazy CLI & Deterministic `.pyz` Bundle:**
  - Added idempotent, non-destructive migrator (`mipmem.core.migrate_v2.migrate_from_v2`) supporting `--dry-run` default previews, SHA-256 manifest deduplication, conflict protection, and 9-field `.meta.json` brainstorm sidecars.
  - Added lean fast-path CLI router (`mipmem.cli`) and lazy subcommand dispatcher (`mipmem.cli_commands`) supporting `init`, `config`, `health`, and `migrate`.
  - Added deterministic stdlib zipapp bundler (`scripts/build_pyz.py`) producing `bin/mipmem.pyz` with `< 25ms` cold-start on POSIX mounts.
