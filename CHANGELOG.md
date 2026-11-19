# Changelog

<!-- ⚠️ SOURCE-VERIFIED — Cross-referenced with src/mipmem/, scripts/build_pyz.py on 2026-11-18 -->

All notable changes to `mipmem` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
