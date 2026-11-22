"""Unit and isolation tests for the Native Antigravity Plugin bundle and validator."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import mipmem
from mipmem.core.diagnostics import PLUGIN_MANIFEST_INVALID

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_valid_bundle(root: Path, *, version: str = "0.2.0") -> None:
    """Create a minimal valid Native Antigravity Plugin bundle inside root."""
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "mem").mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "name": "mipmem",
                "displayName": "MipMem Cognitive Memory",
                "version": version,
                "description": "Zero-dependency local memory engine.",
                "author": {"name": "pageel", "url": "https://github.com/pageel"},
                "homepage": "https://github.com/pageel/mipmem",
                "repository": "https://github.com/pageel/mipmem",
                "suggestedPrompts": ["/mem status"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "hooks.json").write_text(
        json.dumps(
            {
                "mipmem": {
                    "SessionStart": [
                        {
                            "type": "command",
                            "command": "python3 ./bin/mipmem.pyz hook session-start",
                            "timeout": 10,
                        }
                    ],
                    "PreInvocation": [
                        {
                            "type": "command",
                            "command": "python3 ./bin/mipmem.pyz hook pre-invocation",
                            "timeout": 10,
                        }
                    ],
                    "Stop": [
                        {
                            "type": "command",
                            "command": "python3 ./bin/mipmem.pyz hook stop",
                            "timeout": 15,
                        }
                    ],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "mcp_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mipmem": {
                        "command": "python3",
                        "args": ["bin/mipmem.pyz", "mcp"],
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "rules" / "AGENTS.md").write_text(
        "# MipMem Agent Rules\n\n"
        "- Use `/mem` for zero-token memory lookup and L1/L2 progressive paging.\n"
        "- Always ask explicit user confirmation before saving artifacts via `mem_bs_save`.\n",
        encoding="utf-8",
    )
    (root / "skills" / "mem" / "SKILL.md").write_text(
        "---\n"
        "name: mem\n"
        "description: MipMem slash commands and local MCP memory tools.\n"
        "---\n\n"
        "# MipMem Skill\n\n"
        "Use `/mem` commands or MCP tools (`mem_recall`, `mem_bs_save`).\n"
        "Ask user confirmation before saving session artifacts with `mem_bs_save`.\n",
        encoding="utf-8",
    )


class TestPluginManifestValidatorAndBundle(unittest.TestCase):
    """Verify Antigravity plugin schema validator and repo bundle isolation (S3 §1.12)."""

    def test_validate_plugin_bundle_catches_all_schema_violations(self) -> None:
        from mipmem.adapters.plugin_manifest import (
            assert_valid_plugin_bundle,
            validate_plugin_bundle,
        )
        from mipmem.core.diagnostics import MipmemError

        # 1. Valid synthetic bundle passes
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _write_valid_bundle(root, version="0.2.0")
            ok_report = validate_plugin_bundle(root, expected_version="0.2.0")
            self.assertTrue(ok_report.valid, msg=str(ok_report.errors))
            assert_valid_plugin_bundle(root, expected_version="0.2.0")

            # 2. Unknown top-level field in plugin.json
            pj = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
            pj["unsupportedField"] = True
            (root / "plugin.json").write_text(json.dumps(pj), encoding="utf-8")
            bad_pj = validate_plugin_bundle(root, expected_version="0.2.0")
            self.assertFalse(bad_pj.valid)
            self.assertTrue(any("unsupportedField" in e for e in bad_pj.errors))
            with self.assertRaises(MipmemError) as ctx:
                assert_valid_plugin_bundle(root, expected_version="0.2.0")
            self.assertEqual(ctx.exception.code, PLUGIN_MANIFEST_INVALID)

        # 3. Nested matcher/hooks wrapper in flat event + missing python3 prefix
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _write_valid_bundle(root)
            (root / "hooks.json").write_text(
                json.dumps(
                    {
                        "mipmem": {
                            "SessionStart": [
                                {"matcher": "*", "hooks": [{"command": "./bin/mipmem.pyz hook"}]}
                            ],
                            "PreInvocation": [{"command": "./bin/mipmem.pyz hook pre-invocation"}],
                            "Stop": [{"command": "python3 ./bin/mipmem.pyz hook stop"}],
                        }
                    }
                ),
                encoding="utf-8",
            )
            bad_hooks = validate_plugin_bundle(root)
            self.assertFalse(bad_hooks.valid)
            self.assertTrue(
                any("flat" in e.lower() or "matcher" in e.lower() for e in bad_hooks.errors)
            )
            self.assertTrue(any("python3 ./bin/mipmem.pyz" in e for e in bad_hooks.errors))

        # 4. Invalid mcp_config.json args and YAML frontmatter inside rules/AGENTS.md
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _write_valid_bundle(root)
            (root / "mcp_config.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "mipmem": {
                                "command": "python3",
                                "args": ["${PLUGIN_ROOT}/bin/mipmem.pyz", "mcp"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "rules" / "AGENTS.md").write_text(
                "---\nname: invalid\n---\n# Rules\n",
                encoding="utf-8",
            )
            bad_combo = validate_plugin_bundle(root)
            self.assertFalse(bad_combo.valid)
            self.assertTrue(any("mcp_config.json" in e for e in bad_combo.errors))
            self.assertTrue(any("frontmatter" in e.lower() for e in bad_combo.errors))

    def test_actual_repo_plugin_bundle_valid_and_zero_global_side_effects(self) -> None:
        from mipmem.adapters.plugin_manifest import validate_plugin_bundle

        report = validate_plugin_bundle(REPO_ROOT, expected_version=mipmem.__version__)
        self.assertTrue(report.valid, msg="\n".join(report.errors))
        self.assertEqual(report.errors, [])

        # Simulate isolated user $HOME with pre-existing global hooks & bash aliases
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox_home = Path(tmp_dir)
            global_hooks = sandbox_home / ".agents" / "hooks.json"
            global_hooks.parent.mkdir(parents=True)
            sentinel_hooks = '{"user_custom": {"Stop": []}}\n'
            global_hooks.write_text(sentinel_hooks, encoding="utf-8")

            bash_aliases = sandbox_home / ".bash_aliases"
            sentinel_aliases = "alias ll='ls -la'\n"
            bash_aliases.write_text(sentinel_aliases, encoding="utf-8")

            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(sandbox_home),
                "PLUGIN_ROOT": str(REPO_ROOT),
                "PYTHONPATH": str(REPO_ROOT / "src"),
                "MIPMEM_CONFIG_DIR": str(sandbox_home / "cfg"),
                "MIPMEM_VAULT_DIR": str(sandbox_home / "vault"),
                "MIPMEM_CACHE_DIR": str(sandbox_home / "cache"),
            }
            proc = subprocess.run(
                [sys.executable, "-m", "mipmem", "hook", "pre-invocation"],
                input=json.dumps(
                    {
                        "conversationId": "c-iso",
                        "invocationNum": 1,
                        "lastUserInput": "/mem help",
                        "workspacePaths": [str(sandbox_home)],
                        "transcriptPath": str(sandbox_home / "antigravity" / "transcript.jsonl"),
                    }
                ),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
            resp = json.loads(proc.stdout)
            self.assertEqual(resp.get("terminationBehavior"), "terminate")
            self.assertEqual(global_hooks.read_text(encoding="utf-8"), sentinel_hooks)
            self.assertEqual(bash_aliases.read_text(encoding="utf-8"), sentinel_aliases)


if __name__ == "__main__":
    unittest.main()
