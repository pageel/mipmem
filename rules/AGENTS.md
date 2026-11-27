# MipMem Cognitive Memory Rules

Use MipMem to retrieve past cross-host decisions, sessions, and lossless brainstorm shards with minimal token overhead.

## Progressive Memory Paging (L0 -> L1 -> L2 -> L3)

- Treat the startup `L0 Radar` (<= 300 tokens) as a compact index only.
- When you need prior architectural context or past work, call `mem_recall` or `mem_bs_list` first to inspect `L1` outline cards and `#Lstart-Lend` pointers.
- Use `mem_bs_slice` with a surgical section (`§k`) or line-range (`#Lstart-Lend`) selector instead of loading entire documents whenever possible.
- Users can invoke in-chat `/mem` (or plugin-qualified `/mipmem:mem`) commands (`/mem status`, `/mem recall ...`, `/mem bs view <id> §2`, `/mem handoff`) directly with zero LLM tokens.

## Opt-In Artifact Saving (User Confirmation Required)

- MipMem's `Stop` hook intentionally does NOT auto-scan `artifactDirectoryPath` so temporary `task.md` or `walkthrough.md` scratch files never pollute the Brainstorm Vault.
- When the session produces a high-value reusable document (such as an architectural decision, design brainstorm, specification, or deep research artifact), proactively suggest saving it to the Brainstorm Vault and ask for explicit user confirmation first.
- Only after the user confirms, invoke `mem_bs_save(title=..., file_path=..., tags=...)` (or let the user run `/mem bs save <title> --file <path>`). Never silently ingest unconfirmed files.
