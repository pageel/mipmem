# MipMem

[![CI Quality Gate](https://github.com/pageel/mipmem/actions/workflows/ci.yml/badge.svg)](https://github.com/pageel/mipmem/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[English](README.md) | [Tiếng Việt](README.vi.md)

<!-- ⚠️ SOURCE-VERIFIED — Cross-referenced with src/mipmem/, plugin.json, hooks.json, mcp_config.json, scripts/, .github/workflows/ on 2026-11-25 -->

**Zero-dependency, zero-LLM-cost 4-layer mipmapped cognitive memory engine with a conflict-free sharded multi-host vault, multi-host Git/folder sync, RCE-safe private sub-apps, opt-in self-update, sub-5ms bilingual SQLite FTS5 recall, Native Antigravity Plugin, and Universal Local stdio MCP server.**

MipMem is built strictly on the **Python >= 3.9 standard library** (`dependencies = []`) and ships across both PyPI and standalone single-file `.pyz` zipapp bundles engineered for sub-25ms hot-path CLI and hook execution.

---

## Key Architecture & Safety Guarantees (`v1.0.0`)

- **Zero External Dependencies & Zero LLM Cost:** Runs anywhere Python `>= 3.9` is installed with zero runtime packages (`dependencies = []`) and zero embedding or LLM API calls.
- **4-Layer Mipmap Engine (`L0 → L3`):**
  - `L0` startup radar strictly bounded by `budget.l0_max_tokens` (default `300`), incorporating collapsed or alert-driven Sub-App telemetry and sync conflict warnings.
  - `L1` structural skeleton cards with `#Lstart-Lend` pointers strictly bounded by `budget.l1_max_tokens` (default `450`), truncated on line boundaries without ever leaving unclosed Markdown fenced code blocks.
  - `L2` surgical section (`§k`) and line-range (`#Lstart-Lend`) slicing, plus lossless `L3` full-document access.
- **Per-Host Private Sub-Apps Contract (`core/subapps.py`):**
  - Keeps core OS-agnostic while allowing machine-specific health monitors (`hosts/<host_id>/telemetry/<app_id>.status.json` + safe `detail_file` path guard inside `host_dir`) to surface in `L0 Radar`, `/mem fleet`, `mipmem fleet --json`, and `mem_fleet_status`.
  - Allows machine-local `/mem <app>` delegates via `<config_dir>/apps.d/*.json` only (never read from the synced Vault), verifying POSIX UID ownership, non-group/world-writable permissions (`mode & 0o022 == 0`), `shell=False`, timeout caps (`5s` default, `15s` hard max), secret redaction, and collision protection against core `/mem` commands.
- **Conflict-Free Multi-Host Vault & Git/Folder Sync (`mipmem.sync`):**
  - Strict single-writer layout (`hosts/<host_id>/`, `shared/brainstorms/by-host/<host_id>/`) with local gitignored `.cache/global_fts.db`.
  - **`GitSyncBackend`:** Uses `GIT_TERMINAL_PROMPT=0` (OS SSH-Agent / Credential Helper, zero stored PATs), `fcntl.flock` on `<cache_dir>/sync.lock`, `core.filemode=false`, single-writer staging, `git pull --rebase --autostash`, automatic `git rebase --abort` + `sync_status.json` conflict alert on manual shared edits, and detached `Stop`-hook sync (`start_new_session=True`, `0ms` foreground UI block).
  - **`FolderSyncBackend`:** Bi-directional atomic single-writer sync across Syncthing, Tailscale Drive, or shared local directories (`[sync].folder_path`), ignoring `.cache/` and `.git/`, preventing nested-vault recursion, and skipping escaping symlinks.
- **Opt-In Verified Self-Update (`core/updater.py`):**
  - Strictly opt-in (`mipmem self-update [--check | --dry-run | --version <tag>]`). Enforces HTTPS allowlists to official `pageel/mipmem` GitHub Releases, verifies `SHA256SUMS` (`hmac.compare_digest`), checks `ZipFile.testzip()`, runs pre-swap `health --json` in isolation, atomically swaps via `os.replace()`, and refuses modifying `uv`/`pipx`/`pip`/Plugin/git-dev installs (printing the channel-appropriate upgrade command instead).
- **Dual CLI/IDE Transcript Adapters & Unified `user_markdown` Hooks (`mipmem.adapters`):**
  - **CLI & IDE Harvesters:** Opens `conversation_summaries.db` read-only (`mode=ro`, skips subagents) and streams `transcript.jsonl` + lazy `transcript_full.jsonl` expansion (never auto-scanning `artifactDirectoryPath`).
  - **Fail-Open Hooks & `/mem` Intercept:** Runs `SessionStart`, `PreInvocation`, and `Stop` fail-open (`{}` + exit `0`), intercepts `/mem ...` via `metadata.user_markdown` + `terminationBehavior: "terminate"`, and enforces `disabled > passive > active` workspace policies.
- **Universal Local Memory MCP Server (`mipmem mcp`):**
  - Pure stdlib JSON-RPC 2.0 `stdio` server auto-detecting `NDJSON` and `Content-Length` framing (`<= 4 MB`, zero stray stdout logs, `CP-MCP-01`) with 6 canonical tools (`mem_recall`, `mem_bs_list`, `mem_bs_slice`, `mem_bs_save`, `mem_handoff`, `mem_fleet_status`).

---

## Installation (4 Channels)

### 1. PyPI (`uv`, `pipx`, or `pip`)

```bash
# Recommended CLI tool installs (isolated environments)
uv tool install mipmem
# or
pipx install mipmem
# or standard pip
python3 -m pip install mipmem

mipmem health --json
mipmem init --host workstation-alpha --json
```

### 2. Standalone `.pyz` Zipapp (GitHub Releases)

Download `mipmem.pyz` and `SHA256SUMS` from [GitHub Releases](https://github.com/pageel/mipmem/releases):

```bash
sha256sum -c SHA256SUMS --ignore-missing
python3 ./mipmem.pyz health --json
python3 ./mipmem.pyz init --host workstation-alpha --json

# Check for or apply opt-in updates later (zipapp mode only)
python3 ./mipmem.pyz self-update --check --json
python3 ./mipmem.pyz self-update
```

Always invoke `.pyz` files explicitly with `python3` so execution stays portable across Linux, macOS, WSL, and shared mounts regardless of filesystem executable bits.

### 3. Native Antigravity Plugin (CLI & IDE)

This repository root is a self-contained Native Antigravity Plugin (`plugin.json`, `hooks.json`, `mcp_config.json`, `rules/AGENTS.md`, `skills/mem/SKILL.md`, and `bin/mipmem.pyz`):

1. Clone or link the repository directory into your Antigravity plugins directory (e.g., `~/.gemini/config/plugins/mipmem` or `.agents/plugins/mipmem`) or register its path in `plugins.json`.
2. Initialize your vault once (`python3 ./bin/mipmem.pyz init --host workstation-alpha`).
3. Antigravity loads `hooks.json`, `mcp_config.json`, `rules/AGENTS.md`, and `skills/mem/SKILL.md` automatically in an isolated plugin scope without modifying `~/.agents/hooks.json` or `~/.bash_aliases`.

> **Safe Opt-In Artifact Capture:** `Stop` hooks never auto-scan `artifactDirectoryPath`. To persist a design or research artifact into the Brainstorm Vault, confirm the agent's suggestion (`mem_bs_save(title=..., file_path=...)`) or run `/mem bs save "<title>" --file <path>` directly.

### 4. Universal Local MCP Server (Claude Code, Cursor, Gemini CLI, Antigravity)

Point any stdio-compatible MCP client at `mipmem mcp` (or `bin/mipmem.pyz mcp`):

```json
{
  "mcpServers": {
    "mipmem": {
      "command": "python3",
      "args": ["/path/to/mipmem/bin/mipmem.pyz", "mcp"]
    }
  }
}
```

Exposed MCP tools: `mem_recall`, `mem_bs_list`, `mem_bs_slice`, `mem_bs_save`, `mem_handoff`, and `mem_fleet_status`.

---

## In-Chat `/mem` Slash Commands (0 LLM Tokens)

| Command | Description |
| :--- | :--- |
| `/mem help` | Display core `/mem` subcommands and any trusted local `apps.d` delegates |
| `/mem status` | Show vault status, active host, known hosts, and FTS5 capability |
| `/mem fleet` | Show multi-host session/brainstorm inventory, Sub-App telemetry, and sync state |
| `/mem sync [--pull-only \| --push-only]` | Run configured Git or Folder vault synchronization |
| `/mem recall <query> [--host <h>] [--limit <n>]` | Run EN/VI FTS5 BM25 Bigram recall |
| `/mem list [start-end]` | List recent sessions (default `1-10`, compact GFM table on CLI) |
| `/mem bs list [--status <s>] [--host <h>]` | List lossless brainstorm shards with LWW status |
| `/mem bs save <title> [--file <path>] [--tags a,b]` | Save redacted inline text or a UTF-8 file (`<= 2 MB`, non-symlink) |
| `/mem bs view <bs_id> [§k \| #Lstart-Lend \| --l1]` | View L1 outline card or surgical L2 section/line slice |
| `/mem bs status <bs_id> <status>` | Append a conflict-free status event to the active host journal |
| `/mem config [show \| get <k> \| set <k> <v>]` | View or update typed settings in `config.toml` |
| `/mem handoff [@<host_id>]` | Display the latest `Primary Work Anchor` handoff for a host |
| `/mem <app> [args...]` | Execute a trusted local `~/.config/mipmem/apps.d/<app>.json` delegate |

---

## CLI Reference (`v1.0.0`)

| Command | Purpose |
| :--- | :--- |
| `mipmem init [--host <id>] [--vault <dir>] [--json]` | Initialize `config.toml` and create host-isolated vault directories (`0700`/`0600` best-effort) |
| `mipmem config [show \| get <k> \| set <k> <v>] [--json]` | Display, read, or validate & persist typed settings in `config.toml` |
| `mipmem health [--vault <dir>] [--require-vault] [--deep] [--json]` | Fast `< 25ms` check, or `--deep` FTS5, index, `sync_backend`, `last_sync_status`, and `subapp_delegates_count` probe |
| `mipmem fleet [--host <id>] [--vault <dir>] [--json]` | Output multi-host inventory, Sub-App telemetry, and cached sync status |
| `mipmem sync [--pull-only \| --push-only \| --detached] [--host <id>] [--vault <dir>] [--json]` | Execute conflict-free Git or Folder vault synchronization |
| `mipmem self-update [--check \| --dry-run \| --version <tag>] [--json]` | Check for releases or atomically update a standalone zipapp (`SHA256SUMS` + `testzip()` + pre-swap `health`) |
| `mipmem migrate --from-v2 <dir> [--vault <dir>] [--host <id>] [--apply] [--force] [--json]` | Preview (`--dry-run` default) or idempotently import a legacy v2 flat memory directory |
| `mipmem index [--rebuild] [--locale en\|vi] [--vault <dir>] [--json]` | Incrementally sync (or `--rebuild`) `.cache/global_fts.db` from the Sharded Vault |
| `mipmem recall <query> [--host <id>] [--status <st>] [--limit <N>] [--scope all\|sessions\|brainstorms] [--locale en\|vi] [--json]` | Run sanitized EN/VI FTS5 BM25 Bigram recall and return ranked `L1` memory hits |
| `mipmem bs [list \| save \| view \| status] [...] [--json]` | Manage lossless brainstorm shards, surgical `L2` slices, and conflict-free status events |
| `mipmem hook <session-start \| pre-invocation \| stop>` | Run Antigravity lifecycle hooks over `stdin`/`stdout` JSON with sub-25ms no-op skip & fail-open `{}` |
| `mipmem mcp [--vault <dir>]` | Run the universal local stdio JSON-RPC 2.0 MCP server (`NDJSON` & `Content-Length`) |

Errors on standard CLI commands return a non-zero exit code; when `--json` is passed, errors emit a structured envelope on `stdout`:

```json
{"success": false, "error": {"code": "VAULT_NOT_INITIALIZED", "message": "..."}}
```

---

## Directory Resolution, Configuration & Vault Sync

MipMem resolves runtime directories consistently across CLI, hooks, and MCP integrations in strict precedence order:

1. Explicit overrides: `MIPMEM_CONFIG_DIR`, `MIPMEM_VAULT_DIR`, `MIPMEM_CACHE_DIR`
2. `[core].vault_path` in `config.toml` (for `vault_dir`)
3. XDG directories: `XDG_CONFIG_HOME/mipmem`, `XDG_DATA_HOME/mipmem/vault`, `XDG_CACHE_HOME/mipmem`
4. Writable home defaults: `~/.config/mipmem`, `~/.local/share/mipmem/vault`, `~/.cache/mipmem`
5. Fallback (`PLUGIN_DATA/{config,vault,cache}`) only when `$HOME` is unset or non-writable

Starter configuration template: copy [config.example.toml](config.example.toml) to `~/.config/mipmem/config.toml` and customize:

- `[core].locale`: `"en"` or `"vi"` (enables Vietnamese `đ/Đ` FTS5 folding and lexicon packs)
- `[budget]`: `l0_max_tokens`, `l1_max_tokens`, `topic_shift_novelty`
- `[workspace_policies]`: `active` | `passive` | `disabled` (`disabled > passive > default`)
- `[sync]`:
  - `backend = "git"` + `vault_remote_url = "git@github.com:example-user/my-mipmem-vault.git"` + `auto_sync_on_stop = true` (uses OS SSH-Agent or Git Credential Helper; zero PATs stored)
  - or `backend = "folder"` + `folder_path = "/path/to/shared-sync-folder"` (for Syncthing, Tailscale Drive, or local shared folders)

---

## Repository Layout

```text
.
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md     # Standard OSS PR verification checklist
│   └── workflows/
│       ├── ci.yml                   # Lint, pyright, clean-room, verify_release, Python 3.9–3.13 + macOS matrix & .pyz determinism
│       └── release.yml              # Tag-triggered Dual-Channel Release: GitHub Releases (provenance + SHA256SUMS) & PyPI OIDC + PEP 740
├── plugin.json                      # Native Antigravity Plugin manifest (v1.0.0)
├── hooks.json                       # Flat SessionStart / PreInvocation / Stop hook bindings
├── mcp_config.json                  # Local stdio MCP server binding (bin/mipmem.pyz mcp)
├── rules/
│   └── AGENTS.md                    # Concise plain-Markdown plugin rules & opt-in artifact guard
├── skills/
│   └── mem/
│       └── SKILL.md                 # /mem slash command & 6 MCP tools skill reference
├── config.example.toml              # Clean starter config template
├── pyproject.toml                   # Python >= 3.9, dependencies = [], Pageel Contributors metadata
├── README.md                        # English-first OSS documentation
├── README.vi.md                     # Vietnamese community guide
├── CHANGELOG.md                     # Keep a Changelog history through v1.0.0
├── LICENSE                          # MIT License
├── bin/mipmem.pyz                   # Deterministic single-file zipapp binary (v1.0.0)
├── scripts/
│   ├── build_pyz.py                 # Deterministic zipapp bundler
│   ├── check_cleanroom.py           # Tree, archive (.pyz/.zip/.whl/.tar.gz), and --git-history clean-room scanner
│   └── verify_release.py            # Dual-channel release parity, OIDC, and SHA256SUMS validator
├── src/mipmem/
│   ├── __init__.py
│   ├── __main__.py                  # `python3 -m mipmem` entrypoint
│   ├── cli.py                       # Fast-path router (< 25ms health & hook no-op, zero heavy imports)
│   ├── cli_commands.py              # Lazy CLI subcommands (init/config/health/fleet/sync/self-update/migrate/index/recall/bs/hook/mcp)
│   ├── config.py                    # XDG resolver, zero-dep TOML codec & policy evaluator
│   ├── core/
│   │   ├── diagnostics.py           # Rotating JSONL logger & S1–S4 error taxonomy (25 codes, CP-* checkpoints)
│   │   ├── redactor.py              # Pre-write secret redactor & $HOME normalization
│   │   ├── vault.py                 # Sharded vault init, atomic I/O & mutation journal
│   │   ├── migrate_v2.py            # Non-destructive, idempotent v2 importer
│   │   ├── sessions.py              # Canonical Session Frontmatter v1 parser & vault writer
│   │   ├── salience.py              # Dual-Anchor Salience scorer & Brainstorm Bonus
│   │   ├── workstreams.py           # Cross-host Jaccard workstream threading & compaction
│   │   ├── brainstorms.py           # Fence-Aware Markdown AST (§1 skip H1) & sharded store
│   │   ├── mipmap.py                # Token-budgeted L0/L1 renderer & surgical L2 slicer
│   │   ├── fts_recall.py            # Incremental FTS5 indexer, LWW projection, BM25 bigram recall & Topic-Shift gate
│   │   ├── subapps.py               # Per-host telemetry reader, L0/fleet presenter & RCE-safe apps.d delegator
│   │   └── updater.py               # Opt-in release manifest guard & atomic zipapp self-updater
│   ├── sync/
│   │   ├── __init__.py              # Lightweight package marker (zero eager imports)
│   │   ├── base.py                  # SyncBackend Protocol, acquire_sync_lock & atomic sync_status.json store
│   │   ├── git_backend.py           # Conflict-free GitSyncBackend & detached Stop-hook worker
│   │   └── folder_backend.py        # Single-writer FolderSyncBackend (Syncthing/shared dir)
│   ├── adapters/
│   │   ├── __init__.py              # Lightweight package marker (zero eager imports)
│   │   ├── base.py                  # NormalizedConversation schema, TranscriptAdapter Protocol & surface detector
│   │   ├── agy_cli.py               # Read-only SQLite CLI harvester (mode=ro, skips subagents) + JSONL fallback
│   │   ├── agy_ide.py               # IDE transcript.jsonl + transcript_full.jsonl harvester (no artifact auto-scan)
│   │   ├── slash_mem.py             # Shell-free /mem dispatcher (fleet, sync, bs, handoff & apps.d delegates)
│   │   ├── hook_io.py               # Unified user_markdown renderer, SessionStart/PreInvocation/Stop & fail-open runner
│   │   └── plugin_manifest.py       # Automated Native Plugin bundle validator
│   ├── mcp/
│   │   ├── __init__.py
│   │   ├── server.py                # Auto-framing stdio JSON-RPC 2.0 server (NDJSON & Content-Length)
│   │   └── tools.py                 # 6 lazy local memory MCP tools (mem_recall..mem_fleet_status)
│   └── i18n/
│       ├── __init__.py              # LocaleLexicon registry & tokenizer/bigram helpers
│       ├── en.py                    # English stop-words, intent & brainstorm cues
│       └── vi.py                    # Vietnamese đ/Đ FTS5 folder, normalizer & lexicon cues
└── tests/                           # Zero-dependency standard-library unittest test suite
```

---

## Development & Verification

Run all stdlib checks directly from the repository root without external runtime dependencies:

```bash
# Rebuild and verify zipapp integrity
python3 scripts/build_pyz.py
python3 -c "import zipfile; assert zipfile.ZipFile('bin/mipmem.pyz').testzip() is None"
python3 ./bin/mipmem.pyz health --deep --json

# Validate Native Plugin bundle, Clean-Room Git history, Release readiness & full unittest suite
PYTHONPATH=src python3 -c "import mipmem; from mipmem.adapters.plugin_manifest import assert_valid_plugin_bundle; assert_valid_plugin_bundle('.', expected_version=mipmem.__version__)"
python3 scripts/check_cleanroom.py --git-history
python3 scripts/verify_release.py .
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
```

### Continuous Integration & Dual-Channel OIDC Releases

- **CI Quality Gate ([.github/workflows/ci.yml](.github/workflows/ci.yml)):** Runs `py_compile` (across all modules including `sync/`), `flake8`, `pyright`, `scripts/check_cleanroom.py`, `scripts/verify_release.py .`, `assert_valid_plugin_bundle()`, zero-dep `unittest` across **Python `3.9–3.13`** plus **`macos-latest`**, and byte-for-byte `.pyz` determinism checks.
- **OIDC Release Pipeline ([.github/workflows/release.yml](.github/workflows/release.yml)):** Triggered by `v*` tags (`fetch-depth: 0`), rebuilds & verifies deterministic `bin/mipmem.pyz`, builds `.whl`/`.tar.gz`, generates `SHA256SUMS`, runs `check_cleanroom.py --git-history --dist-dir dist` and `verify_release.py . --dist-dir dist`, publishes GitHub Releases with build provenance attestations, and publishes to PyPI via **OIDC Trusted Publishing** (`id-token: write`, PEP 740 `attestations: true`, zero static tokens).

---

## License

Released under the [MIT License](LICENSE).
