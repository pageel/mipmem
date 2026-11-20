"""Tests for clean-room rotating JSONL diagnostics and error taxonomy."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mipmem.core import diagnostics
from mipmem.core.diagnostics import (
    ALL_ERROR_CODES,
    BRAINSTORM_NOT_FOUND,
    CONFIG_INVALID_TOML,
    CP_FTS_01,
    CP_IDX_01,
    CP_MIP_01,
    FTS_INDEX_CORRUPT,
    FTS_UNAVAILABLE,
    INVALID_BRAINSTORM_STATUS,
    INVALID_SLICE_SELECTOR,
    S2_ERROR_CODES,
    SPEC_ERROR_CODES,
    MipmemError,
    RotatingJsonLogger,
    register_default_scrubber,
)


class TestDiagnostics(unittest.TestCase):
    """Verify Spec S1 §9 logging schema, scrubber hooks, and log rotation."""

    def setUp(self) -> None:
        self.addCleanup(register_default_scrubber, None)

    def test_lazy_env_resolution_without_import_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir) / "mipmem-cache"
            logger = RotatingJsonLogger()
            self.assertFalse(cache_dir.exists())

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                importlib.reload(diagnostics)
                resolved = logger.resolve_log_path()
                self.assertEqual(resolved, cache_dir / "mipmem.log")
                self.assertFalse(cache_dir.exists())

    def test_writes_six_field_jsonl_with_quiet_stdio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir) / "cache"
            logger = RotatingJsonLogger()
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()

            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                with (
                    contextlib.redirect_stdout(stdout_buf),
                    contextlib.redirect_stderr(stderr_buf),
                ):
                    written = logger.log_event(
                        "INFO",
                        "config",
                        "CP-CFG-01",
                        error_code=None,
                        duration_ms=1.2345,
                        details={"mode": "local_only"},
                    )

            self.assertEqual(stdout_buf.getvalue(), "")
            self.assertEqual(stderr_buf.getvalue(), "")
            self.assertEqual(written, cache_dir / "mipmem.log")
            lines = (cache_dir / "mipmem.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            for key in (
                "timestamp",
                "level",
                "component",
                "event",
                "error_code",
                "duration_ms",
            ):
                self.assertIn(key, payload)
            self.assertTrue(payload["timestamp"].endswith("Z"))
            self.assertEqual(payload["level"], "INFO")
            self.assertEqual(payload["component"], "config")
            self.assertEqual(payload["event"], "CP-CFG-01")
            self.assertIsNone(payload["error_code"])
            self.assertEqual(payload["duration_ms"], 1.234)
            self.assertEqual(payload["details"], {"mode": "local_only"})

    def test_scrubber_callback_and_global_registration(self) -> None:
        secret_sample = "tok-" + "synthetic-value"

        def mask_secret(text: str) -> str:
            return text.replace(secret_sample, "[REDACTED:TEST]")

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "mipmem.log"
            register_default_scrubber(mask_secret)
            logger = RotatingJsonLogger(log_path=log_file)
            logger.log_event("WARN", "redactor", "CP-RED-01", details={"raw": secret_sample})

            content = log_file.read_text(encoding="utf-8")
            self.assertNotIn(secret_sample, content)
            self.assertIn("[REDACTED:TEST]", content)

    def test_rotation_keeps_two_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file, max_bytes=220, backup_count=2)

            for idx in range(10):
                logger.log_event(
                    "INFO",
                    "vault",
                    "CP-VLT-01",
                    duration_ms=float(idx),
                    details={"seq": idx, "note": "x" * 20},
                )

            self.assertTrue(log_file.exists())
            self.assertTrue((Path(tmp_dir) / "mipmem.log.1").exists())
            self.assertTrue((Path(tmp_dir) / "mipmem.log.2").exists())
            self.assertFalse((Path(tmp_dir) / "mipmem.log.3").exists())
            self.assertLessEqual(log_file.stat().st_size, 220)

    def test_error_taxonomy_and_envelope(self) -> None:
        self.assertEqual(len(SPEC_ERROR_CODES), 6)
        err = MipmemError(CONFIG_INVALID_TOML, "Invalid syntax at line 3")
        self.assertEqual(
            err.to_dict(),
            {
                "success": False,
                "error": {
                    "code": "CONFIG_INVALID_TOML",
                    "message": "Invalid syntax at line 3",
                },
            },
        )

    def test_s2_error_codes_and_checkpoints(self) -> None:
        self.assertEqual(
            S2_ERROR_CODES,
            (
                FTS_UNAVAILABLE,
                FTS_INDEX_CORRUPT,
                BRAINSTORM_NOT_FOUND,
                INVALID_SLICE_SELECTOR,
                INVALID_BRAINSTORM_STATUS,
            ),
        )
        self.assertEqual(len(ALL_ERROR_CODES), 11)
        self.assertEqual((CP_IDX_01, CP_FTS_01, CP_MIP_01), ("CP-IDX-01", "CP-FTS-01", "CP-MIP-01"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file)
            for cp in (CP_IDX_01, CP_FTS_01, CP_MIP_01):
                logger.log_event("INFO", "fts_recall", cp, details={"ok": True})
            lines = log_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 3)
            events = [json.loads(line)["event"] for line in lines]
            self.assertEqual(events, ["CP-IDX-01", "CP-FTS-01", "CP-MIP-01"])


if __name__ == "__main__":
    unittest.main()
