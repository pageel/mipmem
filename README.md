# MipMem

<!-- ⚠️ SOURCE-VERIFIED — Cross-referenced with src/mipmem/, scripts/build_pyz.py on 2026-11-20 -->

**Zero-dependency, zero-LLM-cost 4-layer mipmapped cognitive memory engine with a conflict-free sharded multi-host vault and sub-5ms bilingual SQLite FTS5 recall.**

MipMem is built strictly on the **Python >= 3.9 standard library** (`dependencies = []`) and compiles into a deterministic, single-file `.pyz` bundle engineered for sub-25ms hot-path CLI execution.

---

## Key Architecture & Safety Guarantees (`v0.2.0`)

- **Zero External Dependencies & Zero LLM Cost:** Runs anywhere Python `>= 3.9` is installed with zero runtime packages (`dependencies = []`) and zero embedding or LLM API calls.
- **4-Layer Mipmap Engine (`L0 → L3`):**
  - `L0` startup radar strictly bounded by `budget.l0_max_tokens` (default `300`).
  - `L1` structural skeleton cards with `#Lstart-Lend` pointers strictly bounded by `budget.l1_max_tokens` (default `450`), truncated on line boundaries without ever leaving unclosed Markdown fenced code blocks.
  - `L2` surgical section (`§k`) and line-range (`#Lstart-Lend`) slicing, plus lossless `L3` full-document access.
- **Fence-Aware Brainstorm AST & Sharded Store:** Ignores `#` headings inside ```` ``` ```` / `~~~` code blocks, automatically skips a single leading `H1` document title so `§1` targets the first `H2` body section, stores shards under `shared/brainstorms/by-host/<host_id>/`, and resolves cross-host status transitions deterministically via append-only `hosts/<host_id>/events/mutations.jsonl` (`MAX((ts_utc, actor_host, event_id))`).
- **Incremental Bilingual SQLite FTS5 Recall (`< 5ms`):**
  - Maintains a gitignored, self-healing derived Read Model (`.cache/global_fts.db`, `PRAGMA journal_mode=WAL` with `DELETE` fallback, `busy_timeout=3000`, `PRAGMA user_version=1`).
  - Explicitly folds Vietnamese `đ`/`Đ` (`U+0111`/`U+0110`) to `d`/`D` before FTS5 indexing and `MATCH` (preserving original accented text in `*_meta` tables), with automatic `unicode61` tokenizer fallback on older SQLite builds.
  - Uses FTS5 syntax sanitization, an Anchor Gate, 6-column `bm25()` weighting, adjacent bigram `NEAR` (`x3.5`) phrase boost, Dual-Anchor Salience (`PRIMARY_WORK` vs `UTILITY_MICRO` + `+25` Brainstorm Bonus), gentle recency decay, and an agent-agnostic `RecallBackend` `Protocol` ready for local MCP and future opt-in extensions.
- **Per-Conversation Topic-Shift Gate & Workstreams:** Tracks active topic tokens per `conversation_id` in `.cache/topic_shift_state.json` (LRU `64`, atomic `os.replace`) and auto-threads cross-host sessions into workstreams (`.cache/merged_compacted_memory.md`).
- **Two-Layer Pre-Write Secret Redactor & Clean-Room Safety:** Scrubs secrets (`[REDACTED:<TYPE>]`) and normalizes `$HOME` to `~` before every Vault write and rotating JSONL diagnostic checkpoint (`CP-CFG-01`, `CP-RED-01`, `CP-VLT-01`, `CP-IDX-01`, `CP-FTS-01`, `CP-MIP-01`).
- **Deterministic `.pyz` Zipapp Bundle:** Packages pure `.py` source files (zero bytecode, fixed ZIP timestamps) into `bin/mipmem.pyz` while keeping `mipmem.cli::_fast_health()` `< 25ms` with zero `sqlite3` or heavy `core` imports on the hot path.

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

## CLI Reference (`v0.2.0`)

| Command | Purpose |
| :--- | :--- |
| `mipmem init [--host <id>] [--vault <dir>] [--json]` | Initialize `config.toml` and create host-isolated vault directories (`0700`/`0600` best-effort) |
| `mipmem config show [--json]` | Display active configuration loaded from `config.toml` or schema defaults |
| `mipmem config get <section.key> [--json]` | Read a single typed configuration setting |
| `mipmem config set <section.key> <value> [--json]` | Safely validate and coerce (`bool`/`int`/`float`/`list[str]`/`str`) a setting in `config.toml` |
| `mipmem health [--vault <dir>] [--require-vault] [--deep] [--json]` | Fast `< 25ms` status check, or `--deep` SQLite FTS5 capability, index count, and query latency probe |
| `mipmem migrate --from-v2 <dir> [--vault <dir>] [--host <id>] [--apply] [--force] [--json]` | Preview (`--dry-run` default) or idempotently import a legacy v2 flat memory directory |
| `mipmem index [--rebuild] [--locale en\|vi] [--vault <dir>] [--json]` | Incrementally sync (or `--rebuild`) `.cache/global_fts.db` from the Sharded Vault |
| `mipmem recall <query> [--host <id>] [--status <st>] [--limit <N>] [--scope all\|sessions\|brainstorms] [--locale en\|vi] [--json]` | Run sanitized EN/VI FTS5 BM25 Bigram recall and return ranked `L1` memory hits |
| `mipmem bs save <title> [--file <path> \| --content <text>] [--status <st>] [--tags a,b] [--host <id>] [--json]` | Save a redacted `L3` brainstorm shard + extended `.meta.json` into the active host shard |
| `mipmem bs list [--status <st>] [--host <id>] [--json]` | List cross-host brainstorm shards with CRDT-lite LWW `effective_status` |
| `mipmem bs view <bs_id> [--slice "§k" \| "#Lstart-Lend"] [--l1] [--json]` | Render an `L1` structural card, surgical `L2` section/line slice, or full `L3` document |
| `mipmem bs status <bs_id> <open\|decided\|implementing\|shipped\|archived> [--reason <text>] [--host <id>] [--json]` | Append a conflict-free status transition event to the actor host's `mutations.jsonl` |

Errors return a non-zero exit code; when `--json` is passed, errors emit a structured envelope on `stdout`:

```json
{"success": false, "error": {"code": "VAULT_NOT_INITIALIZED", "message": "..."}}
```

---

## Directory Resolution & Configuration

MipMem resolves runtime directories consistently across CLI and editor integrations in strict precedence order:

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
│   ├── cli.py                       # Fast-path router (< 25ms health, zero heavy imports)
│   ├── cli_commands.py              # Lazy-loaded CLI subcommands (init/config/health/migrate/index/recall/bs)
│   ├── config.py                    # XDG resolver, zero-dep TOML codec & policy evaluator
│   ├── core/
│   │   ├── diagnostics.py           # Rotating JSONL logger & error taxonomy
│   │   ├── redactor.py              # Pre-write secret redactor & $HOME normalization
│   │   ├── vault.py                 # Sharded vault init, atomic I/O & mutation journal
│   │   ├── migrate_v2.py            # Non-destructive, idempotent v2 importer
│   │   ├── sessions.py              # Canonical Session Frontmatter v1 parser & vault writer
│   │   ├── salience.py              # Dual-Anchor Salience scorer & Brainstorm Bonus
│   │   ├── workstreams.py           # Cross-host Jaccard workstream threading & compaction
│   │   ├── brainstorms.py           # Fence-Aware Markdown AST (§1 skip H1) & sharded store
│   │   ├── mipmap.py                # Token-budgeted L0/L1 renderer & surgical L2 slicer
│   │   └── fts_recall.py            # Incremental FTS5 indexer, LWW projection, BM25 bigram recall & Topic-Shift gate
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

# Run clean-room audit and full test suite
python3 scripts/check_cleanroom.py
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
```

### Continuous Integration & PR Quality Gates

Every pull request targeting `main` runs the automated workflow in [.github/workflows/ci.yml](.github/workflows/ci.yml) across three parallel jobs:

1. **Static Lint, Typecheck & Clean-Room (`lint-typecheck-cleanroom`):** Runs `py_compile`, `flake8`, `pyright`, and `scripts/check_cleanroom.py`.
2. **Zero-Dep Test Matrix (`test-matrix`):** Runs the stdlib `unittest` suite across **Python `3.9`, `3.10`, `3.11`, `3.12`, and `3.13`** with **zero `pip install` steps**, proving `dependencies = []` on clean runners. An optional `macos-latest` smoke job is available via manual `workflow_dispatch`.
3. **Deterministic `.pyz` Verification (`pyz-reproducibility`):** Rebuilds `bin/mipmem.pyz`, checks `git diff --exit-code bin/mipmem.pyz` for byte-for-byte determinism, validates `ZipFile.testzip()`, and runs an isolated `/tmp` `health --json` smoke check.

*(Note: Branch Protection rulesets and GitHub Secret Scanning enforcement are activated when transitioning to a public `v1.0.0` release; during `v0.x` private incubation, GitHub Actions status checks and `scripts/check_cleanroom.py` provide full automated verification on every PR.)*

---

## License

Released under the [MIT License](LICENSE).
