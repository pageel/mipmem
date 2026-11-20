"""Integration, concurrency, security, and performance tests for SQLite FTS5 Recall."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from mipmem.core.brainstorms import save_brainstorm_shard, update_brainstorm_status
from mipmem.core.diagnostics import (
    CP_FTS_01,
    CP_IDX_01,
    CP_MIP_01,
    FTS_UNAVAILABLE,
    MipmemError,
    RotatingJsonLogger,
)
from mipmem.core.fts_recall import (
    FTS_DB_REL_PATH,
    SCHEMA_USER_VERSION,
    TOPIC_SHIFT_REL_PATH,
    IndexSyncReport,
    RecallBackend,
    RecallHit,
    RecallResult,
    SqliteFtsRecallBackend,
    TopicShiftDecision,
    evaluate_topic_shift,
    recall_memory,
    sync_fts_index,
)
from mipmem.core.sessions import SessionRecord, write_session_record
from mipmem.core.vault import append_mutation_event, init_vault


class TestFtsIncrementalIndexer(unittest.TestCase):
    """Verify Sysdesign §3 DDL, incremental cursor, LWW determinism, fallbacks, and self-heal."""

    def test_schema_ddl_pragmas_incremental_sync_orphans_and_cp_idx_01(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            init_vault(vault_root, host_id="host-beta")

            s1 = SessionRecord(
                conversation_id="conv-alpha-1",
                host_id="host-alpha",
                surface="cli",
                last_modified_time="2026-11-19T10:00:00.000Z",
                step_count=10,
                title="Bộ nhớ đa máy và đồng bộ định tuyến",
                edited_files=["src/mipmem/core/vault.py"],
                recent_prompts=["Thiết kế đồng bộ đa máy"],
                last_agent_response="Đã hoàn tất định tuyến không xung đột.",
                body="# Bộ nhớ đa máy\n\nChi tiết đồng bộ đa máy.",
            )
            s1_path = write_session_record(vault_root, s1)
            bs1 = save_brainstorm_shard(
                vault_root,
                "Chiến lược đồng bộ",
                "# Chiến lược đồng bộ\n\nLưu trữ đa máy.\n\n## Chi tiết\nNội dung đầy đủ.\n",
                host_id="host-alpha",
                status="open",
            )

            log_file = Path(tmp) / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file)
            report1 = sync_fts_index(vault_root, locale="vi", logger=logger)
            self.assertIsInstance(report1, IndexSyncReport)
            self.assertGreaterEqual(report1.files_indexed, 2)
            self.assertEqual(report1.files_removed, 0)
            self.assertFalse(report1.self_healed)
            self.assertIn(report1.journal_mode.lower(), ("wal", "delete"))

            db_path = vault_root / FTS_DB_REL_PATH
            self.assertTrue(db_path.is_file())

            conn = sqlite3.connect(str(db_path))
            try:
                user_ver = conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(user_ver, SCHEMA_USER_VERSION)

                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                for expected_table in (
                    "vault_sync_cursor",
                    "session_meta",
                    "brainstorm_meta",
                    "mutation_events",
                    "subapp_telemetry",
                    "session_fts",
                    "brainstorm_fts",
                ):
                    self.assertIn(expected_table, tables)

                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    ).fetchall()
                }
                for expected_idx in (
                    "idx_session_host_time",
                    "idx_bs_status_updated",
                    "idx_mutation_target_ts",
                ):
                    self.assertIn(expected_idx, indexes)

                # Original Vietnamese diacritics preserved in *_meta, đ folded in *_fts
                meta_title = conn.execute(
                    "SELECT title FROM session_meta WHERE conversation_id = ?",
                    ("conv-alpha-1",),
                ).fetchone()[0]
                self.assertEqual(meta_title, "Bộ nhớ đa máy và đồng bộ định tuyến")

                fts_hits = conn.execute(
                    'SELECT conversation_id FROM session_fts WHERE session_fts MATCH \'"da may"\''
                ).fetchall()
                self.assertEqual(fts_hits, [("conv-alpha-1",)])
            finally:
                conn.close()

            # Second sync with no changes indexes 0 files
            report2 = sync_fts_index(vault_root, locale="vi", logger=logger)
            self.assertEqual(report2.files_indexed, 0)
            self.assertEqual(report2.files_removed, 0)

            # Orphan removal when session file is deleted
            s1_path.unlink()
            report3 = sync_fts_index(vault_root, locale="vi", logger=logger)
            self.assertGreaterEqual(report3.files_removed, 1)
            conn = sqlite3.connect(str(db_path))
            try:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM session_meta WHERE conversation_id = ?",
                    ("conv-alpha-1",),
                ).fetchone()[0]
                self.assertEqual(remaining, 0)
                bs_row = conn.execute(
                    "SELECT bs_id FROM brainstorm_meta WHERE bs_id = ?",
                    (bs1.bs_id,),
                ).fetchone()
                self.assertIsNotNone(bs_row)
            finally:
                conn.close()

            # CP-IDX-01 emitted
            entries = [
                json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    entry.get("event") == CP_IDX_01
                    and entry.get("component") == "fts_indexer"
                    for entry in entries
                )
            )

    def test_crdt_lite_lww_is_deterministic_across_hosts_and_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            init_vault(vault_root, host_id="host-beta")

            shard = save_brainstorm_shard(
                vault_root,
                "CRDT Convergence Test",
                "# CRDT Convergence Test\n\n## Notes\nDetails.\n",
                host_id="host-alpha",
                status="open",
            )
            # Append competing events with identical timestamp to test actor_host tie-break
            # host-beta > host-alpha lexicographically, so host-beta must always win at same ts
            same_ts = "2099-01-01T12:00:00.000Z"
            alpha_journal = vault_root / "hosts" / "host-alpha" / "events" / "mutations.jsonl"
            beta_journal = vault_root / "hosts" / "host-beta" / "events" / "mutations.jsonl"
            alpha_event = {
                "event_id": "evt-zzzzzzzzzzzz",
                "ts_utc": same_ts,
                "actor_host": "host-alpha",
                "target_type": "brainstorm_status",
                "target_id": shard.bs_id,
                "payload": {"status": "decided"},
            }
            beta_event = {
                "event_id": "evt-000000000001",
                "ts_utc": same_ts,
                "actor_host": "host-beta",
                "target_type": "brainstorm_status",
                "target_id": shard.bs_id,
                "payload": {"status": "shipped"},
            }
            alpha_journal.write_text(json.dumps(alpha_event) + "\n", encoding="utf-8")
            beta_journal.write_text(json.dumps(beta_event) + "\n", encoding="utf-8")

            report = sync_fts_index(vault_root)
            self.assertGreaterEqual(report.mutations_applied, 2)

            db_path = vault_root / FTS_DB_REL_PATH
            conn = sqlite3.connect(str(db_path))
            try:
                eff_status = conn.execute(
                    "SELECT effective_status FROM brainstorm_meta WHERE bs_id = ?",
                    (shard.bs_id,),
                ).fetchone()[0]
                self.assertEqual(eff_status, "shipped")
            finally:
                conn.close()

            # A strictly newer mutation from host-alpha overrides host-beta
            update_brainstorm_status(
                vault_root,
                shard.bs_id,
                "archived",
                actor_host="host-alpha",
            )
            # Force newer timestamp on latest line to exceed 2099-01-01
            lines = alpha_journal.read_text(encoding="utf-8").splitlines()
            newest = json.loads(lines[-1])
            newest["ts_utc"] = "2099-01-02T00:00:00.000Z"
            lines[-1] = json.dumps(newest)
            alpha_journal.write_text("\n".join(lines) + "\n", encoding="utf-8")

            sync_fts_index(vault_root)
            conn = sqlite3.connect(str(db_path))
            try:
                eff_status_2 = conn.execute(
                    "SELECT effective_status FROM brainstorm_meta WHERE bs_id = ?",
                    (shard.bs_id,),
                ).fetchone()[0]
                self.assertEqual(eff_status_2, "archived")
            finally:
                conn.close()

    def test_self_healing_on_corrupt_db_and_fallbacks_for_tokenizer_and_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            write_session_record(
                vault_root,
                SessionRecord(
                    conversation_id="conv-heal",
                    host_id="host-alpha",
                    surface="cli",
                    last_modified_time="2026-11-19T12:00:00.000Z",
                    step_count=8,
                    title="Định tuyến phục hồi tự động",
                    edited_files=["src/mipmem/core/fts_recall.py"],
                    body="Tự động phục hồi chỉ mục khi tệp DB bị hỏng.",
                ),
            )
            sync_fts_index(vault_root, locale="vi")

            # Corrupt DB with random garbage bytes -> next sync auto-rebuilds with 0 exceptions
            db_path = vault_root / FTS_DB_REL_PATH
            db_path.write_bytes(b"NOT_A_VALID_SQLITE_DATABASE_HEADER_CORRUPT_BYTES" * 20)

            healed_report = sync_fts_index(vault_root, locale="vi")
            self.assertTrue(healed_report.self_healed)
            self.assertEqual(healed_report.indexed_sessions, 1)

            # Fallback from remove_diacritics 2 -> unicode61 still matches unaccented Vietnamese
            fallback_report = sync_fts_index(
                vault_root,
                locale="vi",
                rebuild=True,
                preferred_tokenizer="unicode61 remove_diacritics 99",
            )
            self.assertEqual(fallback_report.tokenizer_mode, "unicode61")

            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute(
                    'SELECT conversation_id FROM session_fts '
                    'WHERE session_fts MATCH \'"dinh tuyen"\''
                ).fetchall()
                self.assertEqual(rows, [("conv-heal",)])
            finally:
                conn.close()

            # WAL -> DELETE journal fallback simulation
            with mock.patch(
                "mipmem.core.fts_recall._enable_wal_mode",
                side_effect=sqlite3.OperationalError("simulated NTFS WAL error"),
            ):
                del_report = sync_fts_index(vault_root, rebuild=True)
            self.assertEqual(del_report.journal_mode.lower(), "delete")

            # Missing FTS5 raises FTS_UNAVAILABLE
            with mock.patch(
                "mipmem.core.fts_recall._create_fts_virtual_tables",
                side_effect=sqlite3.OperationalError("no such module: fts5"),
            ):
                with self.assertRaises(MipmemError) as ctx:
                    sync_fts_index(vault_root, rebuild=True)
            self.assertEqual(ctx.exception.code, FTS_UNAVAILABLE)

    def test_incremental_sync_under_10ms_on_500_sessions_and_multithreaded_no_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            init_vault(vault_root, host_id="host-beta")

            for idx in range(500):
                host = "host-alpha" if idx % 2 == 0 else "host-beta"
                month = ((idx // 2) % 12) + 1
                day = (idx % 20) + 1
                rec = SessionRecord(
                    conversation_id=f"conv-bench-{idx:04d}",
                    host_id=host,
                    surface="cli",
                    last_modified_time=f"2026-{month:02d}-{day:02d}T10:00:00.000Z",
                    step_count=6 if idx % 3 == 0 else 2,
                    title=f"Session {idx}: Vault indexing worker module",
                    edited_files=[f"src/mod_{idx % 20}.py"] if idx % 3 == 0 else [],
                    body=f"Benchmark payload {idx} covering sharded storage and recall.",
                )
                write_session_record(vault_root, rec)

            backend = SqliteFtsRecallBackend(vault_root)
            first = backend.sync()
            self.assertEqual(first.indexed_sessions, 500)
            time.sleep(0.06)

            # Warm cached backend connection once then measure 5 single-file incremental syncs
            backend.sync()

            target_month = ((251 // 2) % 12) + 1
            samples_ms: list[float] = []
            for step in range(5):
                updated2 = SessionRecord(
                    conversation_id="conv-bench-0251",
                    host_id="host-beta",
                    surface="cli",
                    last_modified_time=f"2026-{target_month:02d}-20T12:0{step}:00.000Z",
                    step_count=10 + step,
                    title=f"Session 251 Updated {step}: Incremental timing check",
                    edited_files=["src/mipmem/core/mipmap.py"],
                    body=f"Single-file incremental cursor benchmark edit {step}.",
                )
                write_session_record(vault_root, updated2)

                t0 = time.perf_counter()
                inc = backend.sync()
                samples_ms.append((time.perf_counter() - t0) * 1000.0)
                self.assertEqual(inc.files_indexed, 1)
            backend.close()

            samples_ms.sort()
            median_ms = samples_ms[len(samples_ms) // 2]
            self.assertLess(
                median_ms,
                10.0,
                msg=(
                    f"Incremental sync median={median_ms:.2f}ms "
                    f"samples={samples_ms} (target < 10ms)"
                ),
            )

            # Concurrent threads syncing/reading must experience 0 'database is locked' errors
            errors: list[Exception] = []

            def _worker(worker_id: int) -> None:
                try:
                    for step in range(4):
                        append_mutation_event(
                            vault_root,
                            "host-alpha" if worker_id % 2 == 0 else "host-beta",
                            "global_pin",
                            f"pin-{worker_id}-{step}",
                            {"pinned": True},
                        )
                        sync_fts_index(vault_root)
                except Exception as exc:  # pragma: no cover - asserted empty
                    errors.append(exc)

            threads = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [])


class TestBigramRecallAndTopicShiftGate(unittest.TestCase):
    """Verify RecallBackend Protocol, 6-col BM25 Bigram x3.5, <5ms p99, and Topic-Shift LRU."""

    def test_protocol_sanitizer_anchor_gate_vi_diacritics_bigram_salience_and_filters(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            init_vault(vault_root, host_id="host-beta")

            backend = SqliteFtsRecallBackend(vault_root)
            self.assertIsInstance(backend, RecallBackend)

            # 1. Exact bigram vs scattered tokens
            write_session_record(
                vault_root,
                SessionRecord(
                    conversation_id="conv-bigram-exact",
                    host_id="host-alpha",
                    surface="cli",
                    last_modified_time="2026-11-19T10:00:00.000Z",
                    step_count=10,
                    title="Design sharded vault layout",
                    edited_files=["src/mipmem/core/vault.py"],
                    body="Implementing sharded vault isolation for multi-host storage.",
                ),
            )
            write_session_record(
                vault_root,
                SessionRecord(
                    conversation_id="conv-bigram-scattered",
                    host_id="host-alpha",
                    surface="cli",
                    last_modified_time="2026-11-19T10:00:00.000Z",
                    step_count=10,
                    title="Design sharded architecture for remote backup vault",
                    edited_files=["src/mipmem/core/vault.py"],
                    body=(
                        "First section talks about sharded partitions across workers. "
                        "Many unrelated words separate the two terms completely before "
                        "eventually mentioning the backup vault directory later."
                    ),
                ),
            )

            # 2. Same lexical content, PRIMARY_WORK vs UTILITY_MICRO
            write_session_record(
                vault_root,
                SessionRecord(
                    conversation_id="conv-tier-primary",
                    host_id="host-beta",
                    surface="ide",
                    last_modified_time="2026-11-19T11:00:00.000Z",
                    step_count=12,
                    title="Refactor telemetry schema validation",
                    edited_files=["src/telemetry.py", "tests/test_telemetry.py"],
                    body="Refactored telemetry schema validation rules cleanly.",
                ),
            )
            write_session_record(
                vault_root,
                SessionRecord(
                    conversation_id="conv-tier-micro",
                    host_id="host-beta",
                    surface="cli",
                    last_modified_time="2026-11-19T11:00:00.000Z",
                    step_count=1,
                    title="Refactor telemetry schema validation",
                    body="Refactored telemetry schema validation rules cleanly.",
                ),
            )

            # 3. Vietnamese đ/Đ session + brainstorm with LWW shipped status
            write_session_record(
                vault_root,
                SessionRecord(
                    conversation_id="conv-vi-da-may",
                    host_id="host-beta",
                    surface="cli",
                    last_modified_time="2026-11-19T12:00:00.000Z",
                    step_count=8,
                    title="Bộ nhớ đa máy và đồng bộ định tuyến",
                    edited_files=["src/mipmem/i18n/vi.py"],
                    body="Tối ưu định tuyến đồng bộ bộ nhớ đa máy không xung đột.",
                ),
            )
            bs = save_brainstorm_shard(
                vault_root,
                "Chiến lược đồng bộ đa máy",
                (
                    "# Chiến lược đồng bộ đa máy\n\n"
                    "Tóm tắt kiến trúc.\n\n## Chi tiết\nBộ nhớ đa máy.\n"
                ),
                host_id="host-alpha",
                status="open",
            )
            update_brainstorm_status(vault_root, bs.bs_id, "shipped", actor_host="host-beta")
            sync_fts_index(vault_root, locale="vi")

            log_file = Path(tmp) / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file)

            # FTS5 syntax injection fuzz & stop-word Anchor Gate -> 0 crashes, 0 hits
            for malicious in (
                '" OR * NEAR/0 )(',
                "title:secret -drop AND NOT (foo*",
                '"""',
                "   ",
                "the and of to in",
            ):
                res_bad = recall_memory(vault_root, malicious, locale="en", logger=logger)
                self.assertEqual(res_bad.hits, [], f"Expected 0 hits for {malicious!r}")

            # Bigram NEAR x3.5 ranks exact phrase above scattered tokens
            res_bi = recall_memory(vault_root, "sharded vault", locale="en", logger=logger)
            self.assertIsInstance(res_bi, RecallResult)
            self.assertGreaterEqual(len(res_bi.hits), 2)
            self.assertIsInstance(res_bi.hits[0], RecallHit)
            self.assertEqual(res_bi.hits[0].item_id, "conv-bigram-exact")
            self.assertTrue(res_bi.hits[0].bigram_matched)

            # PRIMARY_WORK outranks UTILITY_MICRO for identical lexical match
            res_tier = recall_memory(
                vault_root, "telemetry schema validation", locale="en", host_id="host-beta"
            )
            self.assertGreaterEqual(len(res_tier.hits), 2)
            self.assertEqual(res_tier.hits[0].item_id, "conv-tier-primary")
            self.assertEqual(res_tier.hits[1].item_id, "conv-tier-micro")

            # Vietnamese đ/Đ unaccented recall preserves original accented title in result
            for vi_q in ("da may", "dong bo", "dinh tuyen"):
                res_vi = recall_memory(vault_root, vi_q, locale="vi", logger=logger)
                self.assertGreaterEqual(len(res_vi.hits), 1, f"Expected VI hit for {vi_q!r}")
                titles = [hit.title for hit in res_vi.hits]
                self.assertTrue(
                    any("Bộ nhớ đa máy" in t or "đồng bộ đa máy" in t for t in titles),
                    f"Expected accented Vietnamese title preserved for {vi_q!r}, got {titles}",
                )

            # Status filter returns LWW-converged shipped brainstorm
            res_bs = recall_memory(
                vault_root, "dong bo da may", locale="vi", status="shipped", scope="brainstorms"
            )
            self.assertEqual(len(res_bs.hits), 1)
            self.assertEqual(res_bs.hits[0].item_id, bs.bs_id)
            self.assertEqual(res_bs.hits[0].status, "shipped")

            # Prefix fallback activates when exact match returns 0 rows
            res_pref = recall_memory(vault_root, "telemetr", locale="en")
            self.assertTrue(res_pref.used_prefix_fallback)
            self.assertGreaterEqual(len(res_pref.hits), 1)

            # CP-FTS-01 emitted and does NOT log raw malicious query strings
            raw_log = log_file.read_text(encoding="utf-8")
            self.assertNotIn("title:secret", raw_log)
            log_entries = [json.loads(line) for line in raw_log.splitlines()]
            self.assertTrue(
                any(
                    e.get("event") == CP_FTS_01 and e.get("component") == "fts_recall"
                    for e in log_entries
                )
            )

    def test_500_sessions_and_50_brainstorms_recall_p99_under_5ms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            init_vault(vault_root, host_id="host-beta")

            for idx in range(500):
                host = "host-alpha" if idx % 2 == 0 else "host-beta"
                month = ((idx // 2) % 12) + 1
                day = (idx % 20) + 1
                is_target = idx in (42, 199, 350)
                title = (
                    f"Bộ nhớ đa máy kiến trúc FTS5 #{idx}"
                    if is_target
                    else f"Routine worker task {idx} module alpha"
                )
                body = (
                    "Thiết kế đồng bộ đa máy định tuyến SQLite FTS5 tốc độ cao."
                    if is_target
                    else f"Standard processing payload number {idx} with generic content."
                )
                write_session_record(
                    vault_root,
                    SessionRecord(
                        conversation_id=f"conv-p99-{idx:04d}",
                        host_id=host,
                        surface="cli",
                        last_modified_time=f"2026-{month:02d}-{day:02d}T10:00:00.000Z",
                        step_count=10 if is_target else 3,
                        title=title,
                        edited_files=["src/mipmem/core/fts_recall.py"] if is_target else [],
                        body=body,
                    ),
                )

            for idx in range(50):
                host = "host-alpha" if idx % 2 == 0 else "host-beta"
                save_brainstorm_shard(
                    vault_root,
                    f"Brainstorm Topic {idx}",
                    f"# Brainstorm Topic {idx}\n\nSummary {idx}.\n\n## Plan\nDetails {idx}.\n",
                    host_id=host,
                )

            sync_fts_index(vault_root, locale="vi")

            # Warm SQLite page cache once then measure 25 queries for p99
            backend = SqliteFtsRecallBackend(vault_root)
            warm = backend.recall("dong bo da may", locale="vi", limit=5)
            self.assertGreaterEqual(len(warm.hits), 1)

            latencies_ms: list[float] = []
            for _ in range(25):
                res = backend.recall("dong bo da may", locale="vi", limit=5)
                self.assertGreaterEqual(len(res.hits), 1)
                latencies_ms.append(res.duration_ms)

            latencies_ms.sort()
            p99_idx = min(len(latencies_ms) - 1, int(len(latencies_ms) * 0.99))
            p99_ms = latencies_ms[p99_idx]
            self.assertLess(
                p99_ms,
                5.0,
                msg=(
                    f"FTS5 recall p99={p99_ms:.2f}ms >= 5.0ms "
                    f"(samples={latencies_ms[:5]}..{latencies_ms[-3:]})"
                ),
            )

    def test_topic_shift_gate_per_conversation_isolation_lru_64_and_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault_root = Path(tmp) / "vault"
            init_vault(vault_root, host_id="host-alpha")
            log_file = Path(tmp) / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file)

            # First turn triggers for conv-a
            d1 = evaluate_topic_shift(
                vault_root, "conv-a", "sharded vault sqlite fts5 indexing", logger=logger
            )
            self.assertIsInstance(d1, TopicShiftDecision)
            self.assertTrue(d1.triggered)
            self.assertEqual(d1.novelty_score, 1.0)

            # Repeating same topic on conv-a -> blocked (Novelty < 0.70)
            d2 = evaluate_topic_shift(
                vault_root, "conv-a", "sharded vault sqlite fts5", logger=logger
            )
            self.assertFalse(d2.triggered)
            self.assertLess(d2.novelty_score, 0.70)

            # Independent conversation conv-b is unaffected by conv-a
            d_b = evaluate_topic_shift(
                vault_root, "conv-b", "sharded vault sqlite fts5", logger=logger
            )
            self.assertTrue(d_b.triggered)
            self.assertEqual(d_b.novelty_score, 1.0)

            # Explicit intent cue triggers even on low-novelty topic continuation
            d3 = evaluate_topic_shift(
                vault_root,
                "conv-a",
                "remember architecture decision on sharded vault sqlite fts5",
                logger=logger,
            )
            self.assertTrue(d3.triggered)
            self.assertTrue(d3.intent_matched)

            # Abrupt topic shift triggers (Novelty >= 0.70)
            d4 = evaluate_topic_shift(
                vault_root, "conv-a", "kubernetes ingress tls certificate renewal", logger=logger
            )
            self.assertTrue(d4.triggered)
            self.assertGreaterEqual(d4.novelty_score, 0.70)

            # LRU cap at 64 conversations
            for idx in range(70):
                evaluate_topic_shift(vault_root, f"conv-lru-{idx:03d}", f"topic alpha_{idx}")
            state_path = vault_root / TOPIC_SHIFT_REL_PATH
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertLessEqual(len(state_data["conversations"]), 64)
            self.assertIn("conv-lru-069", state_data["conversations"])

            # Concurrent updates from 2 threads cause 0 errors and valid JSON on disk
            errors: list[Exception] = []

            def _shift_worker(prefix: str) -> None:
                try:
                    for step in range(10):
                        evaluate_topic_shift(
                            vault_root, f"{prefix}-{step}", f"concurrent query {prefix} {step}"
                        )
                except Exception as exc:  # pragma: no cover - asserted empty
                    errors.append(exc)

            t1 = threading.Thread(target=_shift_worker, args=("win-a",))
            t2 = threading.Thread(target=_shift_worker, args=("win-b",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            self.assertEqual(errors, [])
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertLessEqual(len(final_state["conversations"]), 64)

            entries = [
                json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    e.get("event") == CP_MIP_01
                    and e.get("details", {}).get("conversation_id") == "conv-a"
                    for e in entries
                )
            )


if __name__ == "__main__":
    unittest.main()
