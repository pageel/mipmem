"""Tests for the two-layer pre-write secret redactor and log scrubbing."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mipmem.core.diagnostics import RotatingJsonLogger, register_default_scrubber
from mipmem.core.redactor import redact_text, redact_with_stats


class TestRedactor(unittest.TestCase):
    """Verify 8+ secret families, Markdown preservation, $HOME bounds, and CP-RED-01."""

    def setUp(self) -> None:
        self.addCleanup(register_default_scrubber, None)

    def test_redacts_all_secret_families_including_code_fences_and_tables(self) -> None:
        openai_key = "sk" + "-" + ("A" * 32)
        openai_proj = "sk" + "-proj-" + ("B" * 32)
        google_key = "AIza" + ("C" * 35)
        gh_pat = "ghp" + "_" + ("D" * 36)
        gh_fine = "github" + "_pat_" + ("E" * 28)
        gh_oauth = "gho" + "_" + ("F" * 36)
        aws_key = "AKIA" + ("G" * 16)
        jwt_tok = "eyJ" + ("a" * 12) + "." + "eyJ" + ("b" * 12) + "." + ("c" * 16)
        pem_hdr = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        pem_ftr = "-----END " + "OPENSSH PRIVATE KEY-----"
        pem_block = f"{pem_hdr}\nb3BlbnNzaC1rZXktdjE=\nline2secret\n{pem_ftr}"
        db_uri = "postgresql://dbuser:" + "s3cr3tPass" + "@db.example.com:5432/prod"

        sample = (
            "# Secrets Note\n\n"
            "| Service | Value |\n"
            "| :--- | :--- |\n"
            f"| OpenAI | `{openai_key}` |\n"
            f"| ProjectKey | {openai_proj} |\n"
            f"| Google | {google_key} |\n"
            f"| Database | {db_uri} |\n\n"
            "```python\n"
            f'GH_PAT = "{gh_pat}"\n'
            f'GH_FINE = "{gh_fine}"\n'
            f'GH_OAUTH = "{gh_oauth}"\n'
            f'AWS_ID = "{aws_key}"\n'
            f'JWT = "{jwt_tok}"\n'
            "```\n\n"
            "```pem\n"
            f"{pem_block}\n"
            "```\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            logger = RotatingJsonLogger(log_path=Path(tmp_dir) / "mipmem.log")
            result = redact_with_stats(sample, home_dir=Path("/home/alice"))
            self.assertGreaterEqual(result.total_secrets, 10)

            cleaned = redact_text(sample, home_dir=Path("/home/alice"), logger=logger)
            for raw_secret in (
                openai_key,
                openai_proj,
                google_key,
                gh_pat,
                gh_fine,
                gh_oauth,
                aws_key,
                jwt_tok,
                "b3BlbnNzaC1rZXktdjE=",
                "s3cr3tPass",
            ):
                self.assertNotIn(raw_secret, cleaned)

            # Markdown fence and table integrity preserved.
            self.assertIn("```python\n", cleaned)
            self.assertIn("```pem\n", cleaned)
            self.assertEqual(cleaned.count("```"), sample.count("```"))
            self.assertEqual(cleaned.count("|"), sample.count("|"))
            expected_uri = (
                "postgresql://dbuser:[REDACTED:URI_PASSWORD]@db.example.com:5432/prod"
            )
            self.assertIn(expected_uri, cleaned)
            self.assertEqual(
                redact_text(cleaned, home_dir=Path("/home/alice"), logger=logger),
                cleaned,
            )

    def test_home_path_boundary_normalization(self) -> None:
        text_alice = (
            "Read /home/alice/project/file.py and /home/alice\n"
            "Keep /home/alicex/project/file.py intact\n"
        )
        redacted_alice = redact_text(text_alice, home_dir=Path("/home/alice"))
        self.assertIn("~/project/file.py", redacted_alice)
        self.assertIn("and ~\n", redacted_alice)
        self.assertIn("/home/alicex/project/file.py", redacted_alice)

        with tempfile.TemporaryDirectory() as tmp_dir:
            short_home = Path(tmp_dir) / "an"
            sibling_home = Path(tmp_dir) / "anna" / "repo"
            sample = f"A={short_home}/cfg B={sibling_home}/src C={short_home}"
            redacted_short = redact_text(sample, home_dir=short_home)
            self.assertIn("A=~/cfg", redacted_short)
            self.assertIn(f"B={sibling_home}/src", redacted_short)
            self.assertIn("C=~", redacted_short)

    def test_cp_red_01_logging_only_on_secret_match(self) -> None:
        gh_pat = "ghp" + "_" + ("Z" * 36)
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file)

            clean_out = redact_text(
                "Nothing secret in /home/alice/notes.md",
                home_dir=Path("/home/alice"),
                logger=logger,
            )
            self.assertIn("~/notes.md", clean_out)
            self.assertFalse(log_file.exists())

            secret_out = redact_text(
                f"token={gh_pat}",
                home_dir=Path("/home/alice"),
                logger=logger,
            )
            self.assertNotIn(gh_pat, secret_out)
            self.assertTrue(log_file.exists())

            entries = [
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["event"], "CP-RED-01")
            self.assertEqual(entries[0]["component"], "redactor")
            counts = entries[0]["details"]["redacted_counts"]
            self.assertEqual(counts.get("GITHUB_PAT"), 1)
            self.assertNotIn(gh_pat, log_file.read_text(encoding="utf-8"))

    def test_diagnostics_default_scrubber_stops_secret_in_logs_without_recursion(self) -> None:
        api_key = "sk" + "-" + ("Q" * 32)
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir) / "cache"
            logger = RotatingJsonLogger()
            with mock.patch.dict(
                os.environ,
                {"MIPMEM_CACHE_DIR": str(cache_dir)},
                clear=False,
            ):
                logger.log_event("INFO", "test", "EVT-1", details={"leaked": api_key})

            raw_log = (cache_dir / "mipmem.log").read_text(encoding="utf-8")
            self.assertNotIn(api_key, raw_log)
            self.assertIn("[REDACTED:OPENAI_KEY]", raw_log)
            self.assertEqual(len(raw_log.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
