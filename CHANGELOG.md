# Changelog

<!-- ⚠️ SOURCE-VERIFIED — Cross-referenced with src/mipmem/, scripts/build_pyz.py on 2026-11-20 -->

All notable changes to `mipmem` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
