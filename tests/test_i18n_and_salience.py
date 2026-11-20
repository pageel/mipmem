"""Unit tests for bilingual i18n, canonical sessions, salience, and workstreams."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from mipmem.core.diagnostics import VAULT_PATH_TRAVERSAL, MipmemError
from mipmem.core.salience import (
    BRAINSTORM_BONUS,
    TIER_PRIMARY_WORK,
    TIER_UTILITY_MICRO,
    SalienceResult,
    score_session_salience,
)
from mipmem.core.sessions import (
    VALID_SURFACES,
    SessionRecord,
    format_session_markdown,
    parse_session_markdown,
    write_session_record,
)
from mipmem.core.vault import init_vault
from mipmem.core.workstreams import WorkstreamCluster, WorkstreamResult, thread_workstreams
from mipmem.i18n import (
    LocaleLexicon,
    extract_anchor_tokens,
    extract_bigrams,
    get_lexicon,
    has_cue_match,
)
from mipmem.i18n.vi import fold_vietnamese_d, normalize_vi_text


class TestI18nLexiconAndVietnameseNormalizer(unittest.TestCase):
    """Verify Spec S2 i18n registry, đ/Đ FTS5 folding, and EN/VI lexicons."""

    def test_lexicon_registry_defaults_and_properties(self) -> None:
        en_lex = get_lexicon("en")
        vi_lex = get_lexicon("vi")
        fallback_lex = get_lexicon("unknown-locale")

        self.assertIsInstance(en_lex, LocaleLexicon)
        self.assertEqual(en_lex.locale, "en")
        self.assertEqual(vi_lex.locale, "vi")
        self.assertEqual(fallback_lex.locale, "en")
        self.assertAlmostEqual(en_lex.bigram_boost, 3.5)
        self.assertAlmostEqual(vi_lex.bigram_boost, 3.5)
        self.assertFalse(en_lex.strip_diacritics)
        self.assertTrue(vi_lex.strip_diacritics)
        self.assertIn("the", en_lex.stop_words)
        self.assertIn("cua", vi_lex.stop_words)
        self.assertTrue(len(en_lex.intent_cues) >= 5)
        self.assertTrue(len(vi_lex.brainstorm_cues) >= 4)

    def test_vietnamese_d_folding_matches_live_sqlite_fts5(self) -> None:
        self.assertEqual(fold_vietnamese_d("Bộ nhớ đa máy"), "Bộ nhớ da máy")
        self.assertEqual(fold_vietnamese_d("Định tuyến đồng bộ"), "Dịnh tuyến dồng bộ")
        self.assertEqual(
            normalize_vi_text("Bộ nhớ đa máy — Định tuyến đồng bộ!"),
            "bo nho da may dinh tuyen dong bo",
        )

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                'CREATE VIRTUAL TABLE docs_fts USING fts5(id UNINDEXED, title, body, '
                'tokenize="unicode61 remove_diacritics 2")'
            )
            raw_title = "Bộ nhớ đa máy"
            raw_body = "Kiến trúc đồng bộ định tuyến không xung đột"
            conn.execute(
                "INSERT INTO docs_fts VALUES (?, ?, ?)",
                ("doc-1", fold_vietnamese_d(raw_title), fold_vietnamese_d(raw_body)),
            )
            for query in ("bo nho", "da may", "đa máy", "dong bo", "dinh tuyen", "xung dot"):
                folded_q = fold_vietnamese_d(query)
                rows = conn.execute(
                    "SELECT id FROM docs_fts WHERE docs_fts MATCH ?",
                    (f'"{folded_q}"',),
                ).fetchall()
                self.assertEqual(rows, [("doc-1",)], f"Expected FTS5 hit for {query!r}")
        finally:
            conn.close()

    def test_vietnamese_compound_bigrams_and_cue_matching(self) -> None:
        vi_lex = get_lexicon("vi")
        tokens = extract_anchor_tokens("Đồng bộ bộ nhớ đa máy", vi_lex)
        self.assertEqual(tokens, ["dong", "bo", "bo", "nho", "da", "may"])
        bigrams = extract_bigrams(tokens)
        self.assertIn(("dong", "bo"), bigrams)
        self.assertIn(("bo", "nho"), bigrams)
        self.assertIn(("da", "may"), bigrams)
        self.assertTrue(has_cue_match("Thảo luận kiến trúc mới", vi_lex.brainstorm_cues, vi_lex))
        self.assertTrue(
            has_cue_match("Quyết định định hướng phiên trước", vi_lex.intent_cues, vi_lex)
        )
        self.assertFalse(has_cue_match("run unit tests now", vi_lex.brainstorm_cues, vi_lex))


class TestCanonicalSessionFrontmatter(unittest.TestCase):
    """Verify Canonical Session Markdown Frontmatter v1 parsing, formatting, and fallback."""

    def test_frontmatter_round_trip_with_special_chars_and_fenced_hr(self) -> None:
        self.assertEqual(VALID_SURFACES, frozenset({"cli", "ide", "mcp", "generic"}))
        body = (
            "# Kiến trúc: Đồng bộ đa máy\n\n"
            "## Recent Prompts\n"
            "- Design SQLite FTS5 Vietnamese folding\n"
            "- Verify multi-host workstreams\n\n"
            "## Last Agent Response\n"
            "Implemented fold_vietnamese_d and canonical frontmatter.\n\n"
            "```markdown\n"
            "---\n"
            "not_frontmatter: true\n"
            "---\n"
            "```\n"
        )
        record = SessionRecord(
            conversation_id="conv-2026-11-19-alpha",
            host_id="host-alpha",
            surface="cli",
            last_modified_time="2026-11-19T08:15:00.000Z",
            step_count=12,
            title='Kiến trúc: Bộ nhớ "đa máy" & FTS5',
            edited_files=["src/mipmem/core/sessions.py", "tests/test_i18n_and_salience.py"],
            touched_files=["src/mipmem/core/vault.py"],
            recent_prompts=[
                "Design SQLite FTS5 Vietnamese folding",
                "Verify multi-host workstreams",
            ],
            last_agent_response="Implemented fold_vietnamese_d and canonical frontmatter.",
            brainstorm_refs=["bs-arch-01"],
            body=body,
        )

        rendered = format_session_markdown(record)
        self.assertTrue(rendered.startswith("---\n"))
        parsed = parse_session_markdown(rendered)

        self.assertEqual(parsed.conversation_id, record.conversation_id)
        self.assertEqual(parsed.host_id, "host-alpha")
        self.assertEqual(parsed.surface, "cli")
        self.assertEqual(parsed.last_modified_time, "2026-11-19T08:15:00.000Z")
        self.assertEqual(parsed.step_count, 12)
        self.assertEqual(parsed.title, record.title)
        self.assertEqual(parsed.edited_files, record.edited_files)
        self.assertEqual(parsed.touched_files, record.touched_files)
        self.assertEqual(parsed.brainstorm_refs, ["bs-arch-01"])
        self.assertEqual(len(parsed.recent_prompts), 2)
        self.assertIn("fold_vietnamese_d", parsed.last_agent_response)
        self.assertIn("not_frontmatter: true", parsed.body)

    def test_unknown_surface_normalizes_to_generic_and_plain_v2_fallback(self) -> None:
        raw_with_bad_surface = (
            "---\n"
            "conversation_id: conv-surf\n"
            "host_id: host-beta\n"
            "surface: exotic-editor\n"
            "step_count: 4\n"
            "edited_files: a.py, b.py\n"
            "---\n\n"
            "# Surface Normalization Check\n"
        )
        parsed = parse_session_markdown(raw_with_bad_surface)
        self.assertEqual(parsed.surface, "generic")
        self.assertEqual(parsed.edited_files, ["a.py", "b.py"])
        self.assertEqual(parsed.title, "Surface Normalization Check")

        legacy_plain = (
            "# Migrated Legacy Session\n\n"
            "## Edited Files\n"
            "- src/legacy/module.py\n"
            "- tests/test_legacy.py\n\n"
            "## Recent Prompts\n"
            "- Fix startup timeout\n\n"
            "## Last Agent Response\n"
            "Reduced cold-start latency under 20ms.\n"
        )
        legacy = parse_session_markdown(
            legacy_plain,
            fallback_conversation_id="legacy-conv-01",
            fallback_host_id="host-alpha",
            fallback_last_modified="2026-10-01T12:00:00.000Z",
        )
        self.assertEqual(legacy.conversation_id, "legacy-conv-01")
        self.assertEqual(legacy.host_id, "host-alpha")
        self.assertEqual(legacy.surface, "generic")
        self.assertEqual(legacy.title, "Migrated Legacy Session")
        self.assertEqual(legacy.edited_files, ["src/legacy/module.py", "tests/test_legacy.py"])
        self.assertEqual(legacy.recent_prompts, ["Fix startup timeout"])
        self.assertIn("under 20ms", legacy.last_agent_response)

    def test_write_session_record_uses_redacted_vault_write_and_blocks_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            secret_token = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
            record = SessionRecord(
                conversation_id="conv-safe-01",
                host_id="host-alpha",
                surface="mcp",
                last_modified_time="2026-11-19T14:30:00.000Z",
                step_count=5,
                title="Vault Session Write",
                body=f"# Vault Session Write\n\nLeaked token: {secret_token}\n",
            )
            written = write_session_record(vault_root, record)
            expected = (
                vault_root / "hosts" / "host-alpha" / "sessions" / "2026-11" / "conv-safe-01.md"
            )
            self.assertEqual(written.resolve(), expected.resolve())
            disk_text = written.read_text(encoding="utf-8")
            self.assertNotIn(secret_token, disk_text)
            self.assertIn("[REDACTED:GITHUB_PAT]", disk_text)

            bad_record = SessionRecord(
                conversation_id="../escape",
                host_id="host-alpha",
                surface="cli",
                last_modified_time="2026-11-19T14:30:00.000Z",
            )
            with self.assertRaises(MipmemError) as ctx:
                write_session_record(vault_root, bad_record)
            self.assertEqual(ctx.exception.code, VAULT_PATH_TRAVERSAL)


class TestSalienceAndWorkstreams(unittest.TestCase):
    """Verify Dual-Anchor Salience scoring and cross-host Workstreams threading."""

    def test_dual_anchor_salience_tiers_and_brainstorm_bonus_anti_abuse(self) -> None:
        coding_primary = SessionRecord(
            conversation_id="conv-coding",
            host_id="host-alpha",
            surface="ide",
            last_modified_time="2026-11-19T09:00:00.000Z",
            step_count=10,
            title="Implement sharded vault architecture",
            edited_files=["src/mipmem/core/vault.py", "tests/test_vault.py"],
            touched_files=["src/mipmem/config.py"],
            body=(
                "Implemented atomic writes, symlink traversal guards, and mutation journal "
                "locking across multiple workstation hosts with deterministic redaction rules."
            ),
        )
        utility_micro = SessionRecord(
            conversation_id="conv-micro",
            host_id="host-alpha",
            surface="cli",
            last_modified_time="2026-11-19T09:10:00.000Z",
            step_count=2,
            title="Check git status",
            body="Ran git status and exited.",
        )
        substantive_vi_bs = SessionRecord(
            conversation_id="conv-bs-vi",
            host_id="host-beta",
            surface="cli",
            last_modified_time="2026-11-19T10:00:00.000Z",
            step_count=5,
            title="Thảo luận kiến trúc bộ nhớ đa máy",
            body=(
                "Thảo luận kỹ phương án đồng bộ đa máy dùng SQLite FTS5 và CRDT-lite "
                "để quyết định kiến trúc chuẩn cho giai đoạn tiếp theo không xung đột "
                "giữa các thiết bị người dùng."
            ),
        )
        trivial_bs_spam = SessionRecord(
            conversation_id="conv-bs-spam",
            host_id="host-beta",
            surface="cli",
            last_modified_time="2026-11-19T10:05:00.000Z",
            step_count=1,
            title="quick brainstorm",
            body="brainstorm?",
        )

        res_coding = score_session_salience(coding_primary, locale="en")
        res_micro = score_session_salience(utility_micro, locale="en")
        res_bs_vi = score_session_salience(substantive_vi_bs, locale="vi")
        res_spam = score_session_salience(trivial_bs_spam, locale="en")

        self.assertIsInstance(res_coding, SalienceResult)
        self.assertEqual(res_coding.tier, TIER_PRIMARY_WORK)
        self.assertEqual(res_micro.tier, TIER_UTILITY_MICRO)
        self.assertGreater(res_coding.score, res_micro.score)

        self.assertEqual(res_bs_vi.tier, TIER_PRIMARY_WORK)
        self.assertTrue(res_bs_vi.brainstorm_bonus_applied)
        self.assertGreaterEqual(res_bs_vi.score, BRAINSTORM_BONUS + 20.0)

        self.assertFalse(res_spam.brainstorm_bonus_applied)
        self.assertEqual(res_spam.tier, TIER_UTILITY_MICRO)
        self.assertLess(res_spam.score, res_coding.score)

    def test_cross_host_workstreams_jaccard_compaction_and_cache_output(self) -> None:
        s1 = SessionRecord(
            conversation_id="conv-vault-1",
            host_id="host-alpha",
            surface="ide",
            last_modified_time="2026-11-19T08:00:00.000Z",
            step_count=9,
            title="Build sharded vault atomic writes",
            edited_files=["src/mipmem/core/vault.py", "tests/test_vault.py"],
            touched_files=["src/mipmem/core/diagnostics.py"],
            body="Built atomic writes and host ownership checks for sharded vault storage.",
        )
        s2 = SessionRecord(
            conversation_id="conv-vault-2",
            host_id="host-beta",
            surface="cli",
            last_modified_time="2026-11-19T11:00:00.000Z",
            step_count=8,
            title="Harden vault mutation journal tests",
            edited_files=["src/mipmem/core/vault.py", "tests/test_vault.py"],
            body="Added concurrent mutation journal append tests across hosts.",
        )
        s3 = SessionRecord(
            conversation_id="conv-vi-1",
            host_id="host-alpha",
            surface="cli",
            last_modified_time="2026-11-19T07:00:00.000Z",
            step_count=6,
            title="Đồng bộ bộ nhớ đa máy",
            body=(
                "Thảo luận chiến lược đồng bộ bộ nhớ đa máy và định tuyến dữ liệu "
                "cho các phiên làm việc phân tán giữa hai máy."
            ),
        )
        s4 = SessionRecord(
            conversation_id="conv-vi-2",
            host_id="host-beta",
            surface="mcp",
            last_modified_time="2026-11-19T12:00:00.000Z",
            step_count=5,
            title="Tối ưu bộ nhớ đa máy",
            body=(
                "Cải thiện chỉ mục bộ nhớ đa máy giúp tăng tốc truy vấn trên máy phụ "
                "với định tuyến gọn nhẹ."
            ),
        )
        micro_old = SessionRecord(
            conversation_id="conv-micro-old",
            host_id="host-alpha",
            surface="cli",
            last_modified_time="2026-11-18T06:00:00.000Z",
            step_count=1,
            title="Check disk usage",
            body="df -h",
        )

        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")

            res_a = thread_workstreams(
                [s1, s2, s3, s4, micro_old],
                locale="vi",
                vault_root=vault_root,
            )
            res_b = thread_workstreams(
                [micro_old, s4, s2, s3, s1],
                locale="vi",
            )

            self.assertIsInstance(res_a, WorkstreamResult)
            self.assertEqual(
                res_a.session_to_workstream["conv-vault-1"],
                res_a.session_to_workstream["conv-vault-2"],
            )
            self.assertEqual(
                res_a.session_to_workstream["conv-vi-1"],
                res_a.session_to_workstream["conv-vi-2"],
            )
            self.assertNotEqual(
                res_a.session_to_workstream["conv-vault-1"],
                res_a.session_to_workstream["conv-vi-1"],
            )
            # Determinism across input ordering
            self.assertEqual(res_a.session_to_workstream, res_b.session_to_workstream)

            vault_cluster = next(
                c for c in res_a.clusters if "conv-vault-1" in c.session_ids
            )
            self.assertIsInstance(vault_cluster, WorkstreamCluster)
            self.assertEqual(sorted(vault_cluster.hosts), ["host-alpha", "host-beta"])
            self.assertIn("src/mipmem/core/vault.py", vault_cluster.shared_files)

            all_compacted = [cid for c in res_a.clusters for cid in c.compacted_micro_ids]
            self.assertIn("conv-micro-old", all_compacted)

            cache_file = vault_root / ".cache" / "merged_compacted_memory.md"
            self.assertEqual(res_a.output_path, cache_file.resolve())
            self.assertTrue(cache_file.is_file())
            written_md = cache_file.read_text(encoding="utf-8")
            self.assertIn("Compacted", written_md)
            self.assertIn("conv-vault-1", written_md)


if __name__ == "__main__":
    unittest.main()
