"""Unit tests for Fence-Aware Brainstorm AST, Sharded Store, and 4-Layer Mipmap."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from mipmem.core.brainstorms import (
    VALID_BRAINSTORM_STATUSES,
    discover_brainstorm_shards,
    load_brainstorm_shard,
    parse_markdown_ast,
    save_brainstorm_shard,
    update_brainstorm_status,
)
from mipmem.core.diagnostics import (
    BRAINSTORM_NOT_FOUND,
    CP_MIP_01,
    INVALID_BRAINSTORM_STATUS,
    INVALID_SLICE_SELECTOR,
    MipmemError,
    RotatingJsonLogger,
)
from mipmem.core.mipmap import (
    MipmapOutput,
    SliceResult,
    estimate_tokens,
    render_l0_radar,
    render_l1_skeleton,
    slice_brainstorm_l2,
)
from mipmem.core.sessions import SessionRecord
from mipmem.core.vault import atomic_write_vault_file, init_vault


class TestBrainstormAstAndShardedStore(unittest.TestCase):
    """Verify Fence-Aware Markdown AST (§1 skip H1) and Sharded Brainstorm Store."""

    def test_ast_ignores_headings_inside_backtick_and_tilde_fences_and_skips_single_h1(
        self,
    ) -> None:
        doc = (
            "# Multi-Host Memory Architecture\n"
            "\n"
            "Summary sentence describing sharded vault sync across devices.\n"
            "\n"
            "## Problem Statement\n"
            "```python\n"
            "# Not a heading inside python fence\n"
            "## Also not a heading\n"
            "print('ok')\n"
            "```\n"
            "### Sub-problem Details\n"
            "~~~bash\n"
            "# Shell comment must be ignored\n"
            "echo 'hello'\n"
            "~~~\n"
            "\n"
            "## Decisions\n"
            "- Decision: Use fold_vietnamese_d before SQLite FTS5 indexing.\n"
            "- Chốt: Giữ Zero-Dep Python stdlib.\n"
        )
        ast = parse_markdown_ast(doc)

        self.assertTrue(ast.skipped_document_h1)
        self.assertEqual(ast.title, "Multi-Host Memory Architecture")
        self.assertEqual(ast.line_count, 19)
        self.assertGreater(ast.est_tokens, 10)
        self.assertIn("sharded vault sync", ast.l0_scent)

        # §1 must be first H2 (Problem Statement), NOT H1; fenced '#' lines ignored
        self.assertEqual(len(ast.toc), 3)
        self.assertEqual(ast.toc[0].index, 1)
        self.assertEqual(ast.toc[0].level, 2)
        self.assertEqual(ast.toc[0].title, "Problem Statement")
        self.assertEqual(ast.toc[0].start_line, 5)
        self.assertEqual(ast.toc[0].end_line, 16)

        self.assertEqual(ast.toc[1].index, 2)
        self.assertEqual(ast.toc[1].level, 3)
        self.assertEqual(ast.toc[1].title, "Sub-problem Details")
        self.assertEqual(ast.toc[1].start_line, 11)
        self.assertEqual(ast.toc[1].end_line, 16)

        self.assertEqual(ast.toc[2].index, 3)
        self.assertEqual(ast.toc[2].level, 2)
        self.assertEqual(ast.toc[2].title, "Decisions")
        self.assertEqual(ast.toc[2].start_line, 17)
        self.assertEqual(ast.toc[2].end_line, 19)

        self.assertEqual(len(ast.decisions), 2)

    def test_ast_without_h1_keeps_first_heading_as_section_one(self) -> None:
        doc = "## Alpha\nLine A\n## Beta\nLine B\n"
        ast = parse_markdown_ast(doc, fallback_title="Fallback Title")
        self.assertFalse(ast.skipped_document_h1)
        self.assertEqual(ast.title, "Alpha")
        self.assertEqual([(s.index, s.title, s.start_line, s.end_line) for s in ast.toc], [
            (1, "Alpha", 1, 2),
            (2, "Beta", 3, 4),
        ])

    def test_save_load_legacy_9_field_compat_and_cross_host_status_mutation(self) -> None:
        self.assertEqual(
            VALID_BRAINSTORM_STATUSES,
            frozenset({"open", "decided", "implementing", "shipped", "archived"}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            init_vault(vault_root, host_id="host-beta")

            secret = "ghp" + "_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
            body = (
                "# API Caching Strategy\n\n"
                "Explore local SQLite FTS5 caching.\n\n"
                "## Proposal\n"
                f"Never leak token {secret}\n"
            )
            shard = save_brainstorm_shard(
                vault_root,
                "API Caching Strategy",
                body,
                host_id="host-alpha",
                status="open",
                tags=["fts5", "cache"],
            )
            self.assertEqual(shard.origin_host, "host-alpha")
            self.assertEqual(shard.status, "open")
            self.assertEqual(len(shard.toc), 1)
            self.assertEqual(shard.toc[0].title, "Proposal")

            md_text = shard.md_path.read_text(encoding="utf-8")
            meta_raw = json.loads(shard.meta_path.read_text(encoding="utf-8"))
            self.assertNotIn(secret, md_text)
            self.assertIn("[REDACTED:GITHUB_PAT]", md_text)
            self.assertEqual(meta_raw["schema_version"], 1)
            self.assertIn("toc", meta_raw)
            self.assertIn("l0_scent", meta_raw)
            self.assertIn("est_tokens", meta_raw)

            # Cross-host status mutation writes ONLY to host-beta mutations.jsonl (MP5)
            alpha_mtime_before = shard.meta_path.stat().st_mtime_ns
            event = update_brainstorm_status(
                vault_root,
                shard.bs_id,
                "shipped",
                actor_host="host-beta",
                reason="Shipped in v0.2.0",
            )
            self.assertEqual(event["actor_host"], "host-beta")
            self.assertEqual(event["target_type"], "brainstorm_status")
            self.assertEqual(event["target_id"], shard.bs_id)
            self.assertEqual(event["payload"]["status"], "shipped")
            self.assertEqual(shard.meta_path.stat().st_mtime_ns, alpha_mtime_before)
            beta_journal = vault_root / "hosts" / "host-beta" / "events" / "mutations.jsonl"
            self.assertTrue(beta_journal.is_file())

            # Invalid status rejected with INVALID_BRAINSTORM_STATUS
            with self.assertRaises(MipmemError) as err_status:
                update_brainstorm_status(
                    vault_root, shard.bs_id, "invalid-state", actor_host="host-beta"
                )
            self.assertEqual(err_status.exception.code, INVALID_BRAINSTORM_STATUS)

            # Missing brainstorm rejected with BRAINSTORM_NOT_FOUND
            with self.assertRaises(MipmemError) as err_missing:
                update_brainstorm_status(
                    vault_root, "nonexistent-bs-99", "decided", actor_host="host-beta"
                )
            self.assertEqual(err_missing.exception.code, BRAINSTORM_NOT_FOUND)

            # Backward-compatible load of S1 migrate_v2 9-field .meta.json (missing toc/l0_scent)
            legacy_md_rel = "shared/brainstorms/by-host/host-alpha/legacy-note.md"
            legacy_meta_rel = "shared/brainstorms/by-host/host-alpha/legacy-note.meta.json"
            legacy_md = "# Legacy Plan\n\nOverview text.\n\n## Step One\nDetails here.\n"
            atomic_write_vault_file(vault_root, legacy_md_rel, legacy_md, actor_host="host-alpha")
            legacy_meta_9 = {
                "schema_version": 1,
                "bs_id": "legacy-note",
                "title": "Legacy Plan",
                "origin_host": "host-alpha",
                "status": "open",
                "line_count": 6,
                "sha256": "abc123",
                "created_at": "2026-10-01T00:00:00.000Z",
                "updated_at": "2026-10-01T00:00:00.000Z",
            }
            atomic_write_vault_file(
                vault_root,
                legacy_meta_rel,
                json.dumps(legacy_meta_9, indent=2) + "\n",
                actor_host="host-alpha",
            )

            loaded_legacy, loaded_text = load_brainstorm_shard(vault_root, "legacy-note")
            self.assertEqual(loaded_legacy.bs_id, "legacy-note")
            self.assertEqual(len(loaded_legacy.toc), 1)
            self.assertEqual(loaded_legacy.toc[0].title, "Step One")
            self.assertGreater(loaded_legacy.est_tokens, 0)
            self.assertIn("Overview text", loaded_legacy.l0_scent)
            self.assertEqual(loaded_text, legacy_md)

            discovered = discover_brainstorm_shards(vault_root)
            self.assertEqual(len(discovered), 2)


def _has_unclosed_code_fence(markdown_text: str) -> bool:
    """Return True if any backtick/tilde code fence remains open at EOF."""
    open_char: str | None = None
    open_len = 0
    for line in markdown_text.splitlines():
        stripped = line.lstrip()
        if open_char is None:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                ch = stripped[0]
                count = len(stripped) - len(stripped.lstrip(ch))
                open_char = ch
                open_len = count
        else:
            if stripped.startswith(open_char * open_len) and stripped.strip(open_char) == "":
                open_char = None
                open_len = 0
    return open_char is not None


class TestMipmapRendererAndSlicer(unittest.TestCase):
    """Verify L0/L1 token-budget renderer (fence-safe) and L2 section/line slicer."""

    def test_estimate_tokens_and_l0_radar_strict_budget_with_checkpoint_log(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreaterEqual(estimate_tokens("hello world"), 3)

        sessions = [
            SessionRecord(
                conversation_id=f"conv-{idx:02d}",
                host_id="host-alpha" if idx % 2 == 0 else "host-beta",
                surface="cli",
                last_modified_time=f"2026-11-19T10:{idx:02d}:00.000Z",
                step_count=10,
                title=f"Architecture Session {idx}: Multi-host Vault Indexing & Recall Pipeline",
                edited_files=[f"src/mipmem/core/mod_{idx}.py"],
                body="Long architectural summary about cross-host synchronization and recall.",
            )
            for idx in range(25)
        ]

        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            shards = [
                save_brainstorm_shard(
                    vault_root,
                    f"Design Topic {idx}: Distributed Indexing",
                    f"# Design Topic {idx}\n\nSummary paragraph number {idx}.\n\n## Details\nBody.",
                    host_id="host-alpha",
                )
                for idx in range(12)
            ]
            log_file = Path(tmp) / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file)

            out = render_l0_radar(
                sessions=sessions,
                brainstorms=shards,
                max_tokens=300,
                logger=logger,
            )
            self.assertIsInstance(out, MipmapOutput)
            self.assertEqual(out.layer, "L0")
            self.assertEqual(out.budget_tokens, 300)
            self.assertLessEqual(out.est_tokens, 300)
            self.assertEqual(out.est_tokens, estimate_tokens(out.content))
            self.assertTrue(out.truncated)
            self.assertFalse(_has_unclosed_code_fence(out.content))

            tight = render_l0_radar(sessions=sessions, brainstorms=shards, max_tokens=90)
            self.assertLessEqual(estimate_tokens(tight.content), 90)
            self.assertFalse(_has_unclosed_code_fence(tight.content))

            log_lines = [
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    entry.get("event") == CP_MIP_01
                    and entry.get("component") == "mipmap"
                    and entry.get("details", {}).get("layer") == "L0"
                    for entry in log_lines
                )
            )

    def test_l1_skeleton_enforces_450_cap_and_never_leaves_unclosed_code_fence(self) -> None:
        code_lines = "\n".join(
            f"value_{i} = compute_salience_rank(doc_{i}, bigram_boost=3.5)"
            for i in range(60)
        )
        session = SessionRecord(
            conversation_id="conv-code-heavy",
            host_id="host-alpha",
            surface="ide",
            last_modified_time="2026-11-19T12:00:00.000Z",
            step_count=14,
            title="Heavy Code Fence Session",
            edited_files=["src/mipmem/core/fts_recall.py"],
            recent_prompts=["Optimize BM25 6-column weights"],
            body=f"# Heavy Code Fence Session\n\n```python\n{code_lines}\n```\n",
        )

        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file)
            out_default = render_l1_skeleton(session, max_tokens=450, logger=logger)
            self.assertEqual(out_default.layer, "L1")
            self.assertLessEqual(estimate_tokens(out_default.content), 450)
            self.assertTrue(out_default.truncated)
            self.assertFalse(_has_unclosed_code_fence(out_default.content))

            out_tight = render_l1_skeleton(session, max_tokens=110)
            self.assertLessEqual(estimate_tokens(out_tight.content), 110)
            self.assertFalse(_has_unclosed_code_fence(out_tight.content))

            # Brainstorm L1 skeleton includes #Lstart-Lend TOC pointers within <= 450 tokens
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            many_sections = ["# Large Brainstorm", "", "Executive summary.", ""]
            for idx in range(1, 45):
                many_sections.append(f"## Section {idx}: Architectural Subsystem Design")
                many_sections.append(f"- Decision: Adopt deterministic strategy {idx}.")
                many_sections.append("Details for this subsystem.")
            shard = save_brainstorm_shard(
                vault_root,
                "Large Brainstorm",
                "\n".join(many_sections) + "\n",
                host_id="host-alpha",
            )
            bs_l1 = render_l1_skeleton(shard, max_tokens=450)
            self.assertLessEqual(estimate_tokens(bs_l1.content), 450)
            self.assertIn("§1", bs_l1.content)
            self.assertIn("#L", bs_l1.content)
            self.assertFalse(_has_unclosed_code_fence(bs_l1.content))

    def test_l2_section_and_line_range_slicer_and_invalid_selector_errors(self) -> None:
        doc = (
            "# Document Title H1\n"
            "Intro line 2\n"
            "## First Section\n"
            "Line 4 body\n"
            "```python\n"
            "# Fenced comment line 6\n"
            "x = 1\n"
            "```\n"
            "### Nested Subsection\n"
            "Line 10 detail\n"
            "## Second Section\n"
            "Line 12 conclusion\n"
        )

        # §1 targets First Section (H2) including Nested Subsection (H3), NOT H1 or Second Section
        s1 = slice_brainstorm_l2(doc, selector="§1", bs_id="bs-demo")
        self.assertIsInstance(s1, SliceResult)
        self.assertEqual(s1.layer, "L2")
        self.assertEqual(s1.start_line, 3)
        self.assertEqual(s1.end_line, 10)
        self.assertIn("## First Section", s1.content)
        self.assertIn("### Nested Subsection", s1.content)
        self.assertNotIn("# Document Title H1", s1.content)
        self.assertNotIn("## Second Section", s1.content)

        # §2 targets Nested Subsection only
        s2 = slice_brainstorm_l2(doc, selector="§2", bs_id="bs-demo")
        self.assertEqual((s2.start_line, s2.end_line), (9, 10))
        self.assertIn("Line 10 detail", s2.content)

        # §3 targets Second Section
        s3 = slice_brainstorm_l2(doc, selector="§3", bs_id="bs-demo")
        self.assertEqual((s3.start_line, s3.end_line), (11, 12))
        self.assertIn("Line 12 conclusion", s3.content)

        # Explicit line range #L5-L8
        lr = slice_brainstorm_l2(doc, selector="#L5-L8", bs_id="bs-demo")
        self.assertEqual(lr.layer, "L2")
        self.assertEqual((lr.start_line, lr.end_line), (5, 8))
        self.assertEqual(len(lr.content.splitlines()), 4)

        # Empty / None selector returns full L3
        full = slice_brainstorm_l2(doc, selector=None, bs_id="bs-demo")
        self.assertEqual(full.layer, "L3")
        self.assertEqual((full.start_line, full.end_line), (1, 12))
        self.assertEqual(full.content, doc)

        # Invalid selectors must raise INVALID_SLICE_SELECTOR
        for bad in ("§0", "§99", "#L10-L5", "#L0-L3", "#L1-L500", "invalid-slice"):
            with self.assertRaises(MipmemError) as ctx:
                slice_brainstorm_l2(doc, selector=bad, bs_id="bs-demo")
            self.assertEqual(
                ctx.exception.code,
                INVALID_SLICE_SELECTOR,
                f"Expected INVALID_SLICE_SELECTOR for {bad!r}",
            )


if __name__ == "__main__":
    unittest.main()
