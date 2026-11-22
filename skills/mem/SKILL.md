---
name: mem
description: Manage MipMem 4-layer cognitive memory, zero-token /mem slash commands, lossless brainstorm vault slicing, and opt-in artifact capture via local MCP tools.
---

# MipMem Memory Skill (`/mem` & Local MCP)

Use this skill when recalling cross-host engineering context, reading or updating lossless brainstorm shards, checking multi-host handoffs, or saving user-confirmed artifacts into the local MipMem Vault.

## In-Chat `/mem` Slash Commands (0 LLM Tokens)

On Antigravity CLI and IDE, commands prefixed with `/mem` are intercepted by the `PreInvocation` hook and rendered directly as GFM Markdown (`0` LLM tokens consumed):

- `/mem help` — Display all supported commands.
- `/mem status` — Show active vault state, host ID, known hosts, and SQLite FTS5 status.
- `/mem recall <query> [--host <id>] [--limit <n>]` — Run EN/VI FTS5 BM25 Bigram recall.
- `/mem list [start-end]` — List recent sessions (default `1-10`).
- `/mem bs list [--status <status>] [--host <id>]` — List lossless brainstorm shards.
- `/mem bs save <title> [--file <path>] [--tags a,b]` — Save redacted content or a regular UTF-8 file (`<= 2 MB`, non-symlink) as a brainstorm shard.
- `/mem bs view <bs_id> [§k | #Lstart-Lend | --l1]` — Inspect L1 outline or L2 surgical slice.
- `/mem bs status <bs_id> <open|decided|implementing|shipped|archived>` — Append a conflict-free status event.
- `/mem config [show | get <section.key> | set <section.key> <value>]` — Inspect or update `config.toml`.
- `/mem handoff [@<host_id>]` — Show the latest `Primary Work Anchor` handoff for a host.

## Local MCP Tools (`mipmem mcp`)

When working through MCP (Antigravity, Claude Code, Cursor, Gemini CLI):

1. `mem_recall(query, host?, status?, scope?, limit?, offset?)` — Search sessions and brainstorms.
2. `mem_bs_list(status?, host?, limit?, offset?)` — List brainstorm shards with LWW status.
3. `mem_bs_slice(bs_id, selector?, l1?)` — Fetch L1 outline (`l1=true`) or surgical L2 section/line slice.
4. `mem_bs_save(title, content?, file_path?, status?, tags?)` — Persist a redacted brainstorm shard from inline text or a user-confirmed Markdown file.
5. `mem_handoff(host_id?)` — Read the latest host handoff document.
6. `mem_fleet_status()` — Inspect multi-host inventory and telemetry slots.

## Workspace Policies & Safe Opt-In Artifact Capture

- **Workspace Policy (`active` | `passive` | `disabled`):**
  - `active`: injects `L0 Radar` on `SessionStart` (`<= 300` tokens) and `L1` recall on topic shifts (`<= 450` tokens).
  - `passive`: disables automatic `L0`/`L1` injection while keeping `/mem`, `Stop` harvesting, and `mipmem mcp` active.
  - `disabled`: bypasses all hooks immediately.
- **Artifact Capture Rule:** Never auto-ingest files from `artifactDirectoryPath`. When a conversation yields a valuable design or research artifact, propose saving it and wait for explicit user confirmation before calling `mem_bs_save(title=..., file_path=...)`.
