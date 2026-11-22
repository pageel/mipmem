# MipMem

<!-- ⚠️ SOURCE-VERIFIED — Cross-referenced with src/mipmem/, plugin.json, hooks.json, mcp_config.json, scripts/build_pyz.py on 2026-11-22 -->

**Zero-dependency, zero-LLM-cost 4-layer mipmapped cognitive memory engine with a conflict-free sharded multi-host vault, sub-5ms bilingual SQLite FTS5 recall, Native Antigravity Plugin, and Universal Local stdio MCP server.**

MipMem is built strictly on the **Python >= 3.9 standard library** (`dependencies = []`) and compiles into a deterministic, single-file `.pyz` bundle engineered for sub-25ms hot-path CLI and hook execution.

---

## Key Architecture & Safety Guarantees (`v0.3.0`)

- **Zero External Dependencies & Zero LLM Cost:** Runs anywhere Python `>= 3.9` is installed with zero runtime packages (`dependencies = []`) and zero embedding or LLM API calls.
- **4-Layer Mipmap Engine (`L0 → L3`):**
  - `L0` startup radar strictly bounded by `budget.l0_max_tokens` (default `300`).
  - `L1` structural skeleton cards with `#Lstart-Lend` pointers strictly bounded by `budget.l1_max_tokens` (default `450`), truncated on line boundaries without ever leaving unclosed Markdown fenced code blocks.
  - `L2` surgical section (`§k`) and line-range (`#Lstart-Lend`) slicing, plus lossless `L3` full-document access.
- **Dual CLI/IDE Transcript Adapters (`mipmem.adapters`):**
  - **Antigravity CLI Harvester (`agy_cli.py`):** Opens `conversation_summaries.db` strictly in SQLite **read-only URI mode** (`mode=ro`, `timeout=2.0`), skips subagents (`nesting_depth > 0` or non-empty `parent_conversation_id`), and falls back gracefully to JSONL.
  - **Antigravity IDE Harvester (`agy_ide.py`):** Streams `transcript.jsonl`, lazily expands steps carrying `truncated_fields` via sibling `transcript_full.jsonl`, tolerates corrupted lines, and **never** auto-scans `artifactDirectoryPath`.
- **Unified `user_markdown` Hook Engine & 3-Mode Workspace Policy:**
  - Runs `SessionStart`, `PreInvocation`, and `Stop` hooks fail-open (`{}` + exit `0`, bounded `4 MB` stdin cap, `CP-HK-01`).
  - Intercepts `/mem ...` commands on turn 1 across **both Antigravity CLI and IDE** with `metadata.user_markdown` + `terminationBehavior: "terminate"` (`0` LLM tokens, zero non-zero exit errors in CLI `-p` mode).
  - Enforces Most-Restrictive-Wins Workspace Policies (`disabled > passive > active`): `passive` suppresses automatic `L0`/`L1` injection while keeping `/mem`, `Stop` harvesting, and `mipmem mcp` active; `disabled` bypasses all hooks.
  - Skips `Stop` harvesting on `TERMINAL_CUSTOM_HOOK`, `continuationActive`, `disabled` workspaces, and subagents; writes idempotent, redacted session records and updates `hosts/<host_id>/latest_handoff.md` on `PRIMARY_WORK` sessions.
- **Universal Local Memory MCP Server (`mipmem mcp`):**
  - Pure stdlib JSON-RPC 2.0 server over `stdio` auto-detecting **both** `newline-delimited JSON` (NDJSON) and LSP-style `Content-Length` framing (`<= 4 MB`, zero stray logs on `stdout`, `CP-MCP-01`).
  - Exposes 6 canonical tools (`mem_recall`, `mem_bs_list`, `mem_bs_slice`, `mem_bs_save`, `mem_handoff`, `mem_fleet_status`) usable from Antigravity, Claude Code, Cursor, and Gemini CLI.
- **Isolated Native Antigravity Plugin Bundle:** Ships `plugin.json`, `hooks.json` (flat arrays), `mcp_config.json`, `rules/AGENTS.md` (plain Markdown, no frontmatter), and `skills/mem/SKILL.md` directly at the repository root — never modifies global `~/.agents/hooks.json`, `~/.bash_aliases`, or `~/.gemini/config/config.json`.
- **Fence-Aware Brainstorm AST, Bilingual SQLite FTS5 & Pre-Write Secret Redactor:**
  - Ignores `#` inside code fences, skips a single leading `H1` so `§1` targets the first `H2`, folds Vietnamese `đ`/`Đ` for FTS5 BM25 Bigram recall (`< 5ms`), resolves cross-host status updates via append-only `mutations.jsonl`, and scrubs secrets + `$HOME` before every Vault write or diagnostic log (`CP-CFG-01`, `CP-RED-01`, `CP-VLT-01`, `CP-IDX-01`, `CP-FTS-01`, `CP-MIP-01`, `CP-HK-01`, `CP-ADP-01`, `CP-MCP-01`).

---

## Quick Start

Always invoke the `.pyz` bundle explicitly with `python3` so execution works identically across Linux, macOS, WSL, and shared mounts regardless of filesystem executable bits:

```bash
# 1. Build (or rebuild) the deterministic zipapp bundle
python3 scripts/build_pyz.py

# 2. Verify runtime and vault status (< 25ms fast-path)
python3 ./bin/mipmem.pyz health --json

# 3. Initialize your XDG config and host-scoped vault shard
python3 ./bin/mipmem.pyz init --host workstation-alpha --json

# 4. Save a lossless brainstorm, index the vault, and run bilingual recall
python3 ./bin/mipmem.pyz bs save "API Strategy" --file notes.md --json
python3 ./bin/mipmem.pyz index --json
python3 ./bin/mipmem.pyz recall "api strategy" --limit 5 --json

# 5. Deep health probe (SQLite FTS5 capability, index counts & live query ms)
python3 ./bin/mipmem.pyz health --deep --json
```

You can also invoke the package directly from source during development:

```bash
PYTHONPATH=src python3 -m mipmem health --json
```

---

## Native Antigravity Plugin & Universal MCP Setup

### 1. Native Antigravity Plugin (CLI & IDE)

This repository root is a self-contained Native Antigravity Plugin (`plugin.json`, `hooks.json`, `mcp_config.json`, `rules/AGENTS.md`, `skills/mem/SKILL.md`, and `bin/mipmem.pyz`):

1. Clone or link the repository directory into your Antigravity plugins directory (e.g., `~/.gemini/config/plugins/mipmem` or `.agents/plugins/mipmem`) or register its path in `plugins.json`.
2. Ensure your vault is initialized once (`python3 ./bin/mipmem.pyz init --host workstation-alpha`).
3. Antigravity loads `hooks.json`, `mcp_config.json`, `rules/AGENTS.md`, and `skills/mem/SKILL.md` automatically in an isolated plugin scope without modifying `~/.agents/hooks.json` or `~/.bash_aliases`.

> **Safe Opt-In Artifact Capture:** `Stop` hooks never auto-scan `artifactDirectoryPath`. To persist a valuable design or research artifact into the Brainstorm Vault, confirm the agent's suggestion (`mem_bs_save(title=..., file_path=...)`) or run `/mem bs save "<title>" --file <path>` directly.

### 2. In-Chat `/mem` Slash Commands (0 LLM Tokens)

| Command | Description |
| :--- | :--- |
| `/mem help` | Display all `/mem` subcommands |
| `/mem status` | Show vault status, active host, known hosts, and FTS5 capability |
| `/mem recall <query> [--host <h>] [--limit <n>]` | Run EN/VI FTS5 BM25 Bigram recall |
| `/mem list [start-end]` | List recent sessions (default `1-10`, compact GFM table on CLI) |
| `/mem bs list [--status <s>] [--host <h>]` | List lossless brainstorm shards with LWW status |
| `/mem bs save <title> [--file <path>] [--tags a,b]` | Save redacted inline text or a UTF-8 file (`<= 2 MB`, non-symlink) |
| `/mem bs view <bs_id> [§k \| #Lstart-Lend \| --l1]` | View L1 outline card or surgical L2 section/line slice |
| `/mem bs status <bs_id> <status>` | Append a conflict-free status event to the active host journal |
| `/mem config [show \| get <k> \| set <k> <v>]` | View or update typed settings in `config.toml` |
| `/mem handoff [@<host_id>]` | Display the latest `Primary Work Anchor` handoff for a host |

### 3. Universal Local MCP Server (Claude Code, Cursor, Gemini CLI, Antigravity)

Point any stdio-compatible MCP client at `bin/mipmem.pyz mcp` (no `.env` secrets or external packages required):

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

## CLI Reference (`v0.3.0`)

| Command | Purpose |
| :--- | :--- |
| `mipmem init [--host <id>] [--vault <dir>] [--json]` | Initialize `config.toml` and create host-isolated vault directories (`0700`/`0600` best-effort) |
| `mipmem config [show \| get <k> \| set <k> <v>] [--json]` | Display, read, or validate & persist typed settings in `config.toml` |
| `mipmem health [--vault <dir>] [--require-vault] [--deep] [--json]` | Fast `< 25ms` status check, or `--deep` SQLite FTS5 capability, index count, and query latency probe |
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

## Directory Resolution & Configuration

MipMem resolves runtime directories consistently across CLI, hooks, and MCP integrations in strict precedence order:

1. Explicit overrides: `MIPMEM_CONFIG_DIR`, `MIPMEM_VAULT_DIR`, `MIPMEM_CACHE_DIR`
2. `[core].vault_path` in `config.toml` (for `vault_dir`)
3. XDG directories: `XDG_CONFIG_HOME/mipmem`, `XDG_DATA_HOME/mipmem/vault`, `XDG_CACHE_HOME/mipmem`
4. Writable home defaults: `~/.config/mipmem`, `~/.local/share/mipmem/vault`, `~/.cache/mipmem`
5. Fallback (`PLUGIN_DATA/{config,vault,cache}`) only when `$HOME` is unset or non-writable

Starter configuration template: copy [config.example.toml](config.example.toml) to `~/.config/mipmem/config.toml` and customize `[core].locale` (`"en"` | `"vi"`), `[budget]` (`l0_max_tokens`, `l1_max_tokens`, `topic_shift_novelty`), or `[workspace_policies]` (`active` | `passive` | `disabled`, resolved with a Most-Restrictive-Wins policy `disabled > passive > default`).

### Vault Sync & Zero-Secret Authentication

MipMem **never stores Git PATs, SSH private keys, or API secrets** inside `config.toml` or Vault files:

- Set `[sync].vault_remote_url` to an SSH or HTTPS remote, e.g. `git@github.com:example-user/my-mipmem-vault.git`.
- Delegate Git authentication entirely to your OS **SSH-Agent** (`ssh-add`) or **Git Credential Helper** (`git config --global credential.helper ...`).

---

## Repository Layout

```text
.
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md     # Standard OSS PR verification checklist
│   └── workflows/
│       └── ci.yml                   # 3-job GitHub Actions CI (Python 3.9–3.13 zero-dep matrix)
├── plugin.json                      # Native Antigravity Plugin manifest
├── hooks.json                       # Flat SessionStart / PreInvocation / Stop hook bindings
├── mcp_config.json                  # Local stdio MCP server binding (bin/mipmem.pyz mcp)
├── rules/
│   └── AGENTS.md                    # Concise plain-Markdown plugin rules & opt-in artifact guard
├── skills/
│   └── mem/
│       └── SKILL.md                 # /mem slash command & 6 MCP tools skill reference
├── config.example.toml              # Clean starter config template
├── pyproject.toml                   # Python >= 3.9, dependencies = []
├── LICENSE                          # MIT License
├── bin/mipmem.pyz                   # Deterministic single-file zipapp binary
├── scripts/
│   ├── build_pyz.py                 # Zipapp bundler
│   └── check_cleanroom.py           # Static secret/path/bytecode scanner
├── src/mipmem/
│   ├── __init__.py
│   ├── __main__.py                  # `python3 -m mipmem` entrypoint
│   ├── cli.py                       # Fast-path router (< 25ms health & hook no-op, zero heavy imports)
│   ├── cli_commands.py              # Lazy CLI subcommands (init/config/health/migrate/index/recall/bs/hook/mcp)
│   ├── config.py                    # XDG resolver, zero-dep TOML codec & policy evaluator
│   ├── core/
│   │   ├── diagnostics.py           # Rotating JSONL logger & S1/S2/S3 error taxonomy
│   │   ├── redactor.py              # Pre-write secret redactor & $HOME normalization
│   │   ├── vault.py                 # Sharded vault init, atomic I/O & mutation journal
│   │   ├── migrate_v2.py            # Non-destructive, idempotent v2 importer
│   │   ├── sessions.py              # Canonical Session Frontmatter v1 parser & vault writer
│   │   ├── salience.py              # Dual-Anchor Salience scorer & Brainstorm Bonus
│   │   ├── workstreams.py           # Cross-host Jaccard workstream threading & compaction
│   │   ├── brainstorms.py           # Fence-Aware Markdown AST (§1 skip H1) & sharded store
│   │   ├── mipmap.py                # Token-budgeted L0/L1 renderer & surgical L2 slicer
│   │   └── fts_recall.py            # Incremental FTS5 indexer, LWW projection, BM25 bigram recall & Topic-Shift gate
│   ├── adapters/
│   │   ├── __init__.py              # Lightweight package marker (zero eager imports)
│   │   ├── base.py                  # NormalizedConversation schema, TranscriptAdapter Protocol & surface detector
│   │   ├── agy_cli.py               # Read-only SQLite CLI harvester (mode=ro, skips subagents) + JSONL fallback
│   │   ├── agy_ide.py               # IDE transcript.jsonl + transcript_full.jsonl harvester (no artifact auto-scan)
│   │   ├── slash_mem.py             # Shell-free /mem dispatcher & safe UTF-8 file reader
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
└── tests/                           # Standard-library unittest test suite
```

---

## Development & Verification

Run all stdlib checks directly from the repository root without external runtime dependencies:

```bash
# Rebuild and verify zipapp integrity
python3 scripts/build_pyz.py
python3 -c "import zipfile; assert zipfile.ZipFile('bin/mipmem.pyz').testzip() is None"
python3 ./bin/mipmem.pyz health --json

# Validate Native Plugin bundle, run clean-room audit and full test suite
PYTHONPATH=src python3 -c "import mipmem; from mipmem.adapters.plugin_manifest import assert_valid_plugin_bundle; assert_valid_plugin_bundle('.', expected_version=mipmem.__version__)"
python3 scripts/check_cleanroom.py
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
```

### Continuous Integration & PR Quality Gates

Every pull request targeting `main` runs the automated workflow in [.github/workflows/ci.yml](.github/workflows/ci.yml) across three parallel jobs:

1. **Static Lint, Typecheck, Clean-Room & Plugin Bundle (`lint-typecheck-cleanroom`):** Runs `py_compile` across `src/mipmem/{core,i18n,adapters,mcp}`, `flake8`, `pyright`, `scripts/check_cleanroom.py`, and `assert_valid_plugin_bundle()`.
2. **Zero-Dep Test Matrix (`test-matrix`):** Runs the stdlib `unittest` suite across **Python `3.9`, `3.10`, `3.11`, `3.12`, and `3.13`** with **zero `pip install` steps**, proving `dependencies = []` on clean runners. An optional `macos-latest` smoke job is available via manual `workflow_dispatch`.
3. **Deterministic `.pyz` Verification (`pyz-reproducibility`):** Rebuilds `bin/mipmem.pyz`, checks `git diff --exit-code bin/mipmem.pyz` for byte-for-byte determinism, validates `ZipFile.testzip()`, and runs an isolated `/tmp` `health --json` smoke check.

*(Note: Branch Protection rulesets and GitHub Secret Scanning enforcement are activated when transitioning to a public `v1.0.0` release; during `v0.x` private incubation, GitHub Actions status checks and `scripts/check_cleanroom.py` provide full automated verification on every PR.)*

---

## License

Released under the [MIT License](LICENSE).
