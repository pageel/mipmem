# MipMem

<!-- ⚠️ SOURCE-VERIFIED — Cross-referenced with src/mipmem/, scripts/build_pyz.py on 2026-11-18 -->

**Zero-dependency, zero-overhead cognitive memory engine with a conflict-free sharded multi-host vault.**

MipMem is built strictly on the **Python >= 3.9 standard library** (`dependencies = []`) and compiles into a deterministic, single-file `.pyz` bundle engineered for sub-25ms hot-path CLI execution.

---

## Key Architecture & Safety Guarantees (`v0.1.0`)

- **Zero External Dependencies:** Runs anywhere Python `>= 3.9` is installed with zero `pip install` runtime packages.
- **Sharded Multi-Host Vault:** Isolates every host's mutable state (`hosts/<host_id>/` and `shared/brainstorms/by-host/<host_id>/`) and records cross-host state transitions in append-only JSONL journals (`hosts/<host_id>/events/mutations.jsonl`) — avoiding shared mutable global JSON indexes that cause Git merge conflicts.
- **Two-Layer Pre-Write Secret Redactor:** Scans every vault payload and log line before disk I/O to scrub common API keys, GitHub tokens, AWS keys, JWT tokens, PEM private keys, and database URI passwords (`[REDACTED:<TYPE>]`), and normalizes the active `$HOME` prefix to `~`.
- **Rotating JSONL Diagnostics:** Records structured diagnostic checkpoints (`CP-CFG-01`, `CP-RED-01`, `CP-VLT-01`) into `~/.cache/mipmem/mipmem.log` (`2 MB x 2` backups) while keeping `stdout` strictly free of log noise.
- **Idempotent Legacy Migration:** `mipmem migrate --from-v2` defaults to non-writing `--dry-run` and copies/redacts/shards legacy flat memory directories only when `--apply` is passed, deduplicating subsequent runs via `hosts/<host_id>/migrated_v2_manifest.json`.
- **Deterministic `.pyz` Zipapp Bundle:** Packages pure `.py` source files (zero bytecode, fixed ZIP timestamps) into `bin/mipmem.pyz` with `< 25ms` cold-start on POSIX filesystems.

---

## Quick Start

Always invoke the `.pyz` bundle explicitly with `python3` so execution works identically across Linux, macOS, WSL, and shared mounts regardless of filesystem executable bits:

```bash
# 1. Build (or rebuild) the deterministic zipapp bundle
python3 scripts/build_pyz.py

# 2. Verify runtime and vault status
python3 ./bin/mipmem.pyz health --json

# 3. Initialize your XDG config and host-scoped vault shard
python3 ./bin/mipmem.pyz init --host workstation-alpha --json
```

You can also invoke the package directly from source during development:

```bash
PYTHONPATH=src python3 -m mipmem health --json
```

---

## CLI Reference (`v0.1.0`)

| Command | Purpose |
| :--- | :--- |
| `mipmem init [--host <id>] [--vault <dir>] [--json]` | Initialize `config.toml` and create host-isolated vault directories (`0700`/`0600` best-effort) |
| `mipmem config show [--json]` | Display active configuration loaded from `config.toml` or schema defaults |
| `mipmem config get <section.key> [--json]` | Read a single typed configuration setting |
| `mipmem config set <section.key> <value> [--json]` | Safely validate and coerce (`bool`/`int`/`float`/`list[str]`/`str`) a setting in `config.toml` |
| `mipmem health [--vault <dir>] [--require-vault] [--json]` | Check runtime status, vault manifest initialization, and cold-start latency |
| `mipmem migrate --from-v2 <dir> [--vault <dir>] [--host <id>] [--apply] [--force] [--json]` | Preview (`--dry-run` default) or idempotently import a legacy v2 flat memory directory |

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

Starter configuration template: copy [config.example.toml](config.example.toml) to `~/.config/mipmem/config.toml` and customize host settings or `[workspace_policies]` (`active` | `passive` | `disabled`, resolved with a Most-Restrictive-Wins policy `disabled > passive > default`).

### Vault Sync & Zero-Secret Authentication

MipMem **never stores Git PATs, SSH private keys, or API secrets** inside `config.toml` or Vault files:

- Set `[sync].vault_remote_url` to an SSH or HTTPS remote, e.g. `git@github.com:example-user/my-mipmem-vault.git`.
- Delegate Git authentication entirely to your OS **SSH-Agent** (`ssh-add`) or **Git Credential Helper** (`git config --global credential.helper ...`).

---

## Repository Layout

```text
.
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
│   ├── cli.py                       # Fast-path router (< 25ms health)
│   ├── cli_commands.py              # Lazy-loaded argparse & command handlers
│   ├── config.py                    # XDG resolver, zero-dep TOML codec & policy evaluator
│   └── core/
│       ├── diagnostics.py           # Rotating JSONL logger & Spec error taxonomy
│       ├── redactor.py              # Pre-write secret redactor & $HOME normalization
│       ├── vault.py                 # Sharded vault init, atomic I/O & mutation journal
│       └── migrate_v2.py            # Non-destructive, idempotent v2 importer
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

---

## License

Released under the [MIT License](LICENSE).
