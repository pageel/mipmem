"""Tests for XDG path resolution, Python 3.9-3.13 TOML codec parity, and workspace policy."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mipmem import config
from mipmem.config import (
    load_config,
    resolve_paths,
    resolve_workspace_policy,
    save_config,
    _parse_minimal_toml,
)
from mipmem.core.diagnostics import CONFIG_INVALID_TOML, MipmemError

try:
    import tomllib as _stdlib_tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    _stdlib_tomllib = None

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestXdgPathResolver(unittest.TestCase):
    """Verify Spec S1 & Fix-P1-4 priority: MIPMEM_* -> config -> XDG -> $HOME -> PLUGIN_DATA."""

    def test_explicit_mipmem_env_overrides_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            env = {
                "MIPMEM_CONFIG_DIR": str(base / "m_cfg"),
                "MIPMEM_VAULT_DIR": str(base / "m_vault"),
                "MIPMEM_CACHE_DIR": str(base / "m_cache"),
                "XDG_CONFIG_HOME": str(base / "xdg_cfg"),
                "XDG_DATA_HOME": str(base / "xdg_data"),
                "XDG_CACHE_HOME": str(base / "xdg_cache"),
                "HOME": str(base / "home"),
                "PLUGIN_DATA": str(base / "plugin_data"),
            }
            (base / "home").mkdir()
            with mock.patch.dict(os.environ, env, clear=True):
                paths = resolve_paths(configured_vault_dir=str(base / "toml_vault"))

            self.assertEqual(paths.config_dir, base / "m_cfg")
            self.assertEqual(paths.vault_dir, base / "m_vault")
            self.assertEqual(paths.cache_dir, base / "m_cache")
            self.assertEqual(paths.config_file, base / "m_cfg" / "config.toml")
            self.assertFalse(paths.used_plugin_fallback)

    def test_config_vault_overrides_xdg_when_mipmem_vault_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            (base / "home").mkdir()
            env = {
                "XDG_CONFIG_HOME": str(base / "xdg_cfg"),
                "XDG_DATA_HOME": str(base / "xdg_data"),
                "XDG_CACHE_HOME": str(base / "xdg_cache"),
                "HOME": str(base / "home"),
                "PLUGIN_DATA": str(base / "plugin_data"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                paths = resolve_paths(configured_vault_dir=str(base / "custom_vault"))

            self.assertEqual(paths.config_dir, base / "xdg_cfg" / "mipmem")
            self.assertEqual(paths.vault_dir, base / "custom_vault")
            self.assertEqual(paths.cache_dir, base / "xdg_cache" / "mipmem")
            self.assertFalse(paths.used_plugin_fallback)

    def test_writable_home_shares_vault_even_when_plugin_data_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            home_dir = base / "home"
            home_dir.mkdir()
            env = {
                "HOME": str(home_dir),
                "PLUGIN_DATA": str(base / "plugin_data"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                paths = resolve_paths()

            self.assertEqual(paths.config_dir, home_dir / ".config" / "mipmem")
            self.assertEqual(paths.vault_dir, home_dir / ".local" / "share" / "mipmem" / "vault")
            self.assertEqual(paths.cache_dir, home_dir / ".cache" / "mipmem")
            self.assertFalse(paths.used_plugin_fallback)

    def test_plugin_data_fallback_only_when_home_missing_or_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            plugin_dir = base / "plugin_data"
            env = {
                "HOME": str(base / "missing_home"),
                "PLUGIN_DATA": str(plugin_dir),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                paths = resolve_paths()

            self.assertEqual(paths.config_dir, plugin_dir / "config")
            self.assertEqual(paths.vault_dir, plugin_dir / "vault")
            self.assertEqual(paths.cache_dir, plugin_dir / "cache")
            self.assertTrue(paths.used_plugin_fallback)

            # Simulate existing but read-only $HOME.
            ro_home = base / "ro_home"
            ro_home.mkdir()
            env["HOME"] = str(ro_home)
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("mipmem.config._is_writable_dir", return_value=False),
            ):
                paths_ro = resolve_paths()
            self.assertEqual(paths_ro.vault_dir, plugin_dir / "vault")
            self.assertTrue(paths_ro.used_plugin_fallback)


class TestTomlCodecParity(unittest.TestCase):
    """Verify Python 3.9-3.10 _parse_minimal_toml matches tomllib and emits CP-CFG-01."""

    def test_minimal_parser_matches_tomllib_on_rich_config(self) -> None:
        tbd_marker = "[" + "TBD]"
        raw = (
            "# Top comment\n"
            "[core]\n"
            'host_id = "workstation-alpha"\n'
            'vault_path = "~/.local/share/mipmem/vault" # inline comment\n'
            'locale = "en"\n\n'
            "[sync]\n"
            'backend = "none"\n'
            f'vault_remote_url = "https://example.com/#section-sync" # keep hash in string\n'
            "auto_sync_on_stop = true\n"
            f'fallback_tag = "{tbd_marker}"\n\n'
            "[budget]\n"
            "l0_max_tokens = 300\n"
            "l1_max_tokens = 1_024\n"
            "topic_shift_novelty = 0.70\n\n"
            "[workspace_policies]\n"
            'default = "active"\n'
            'single_line = ["/path/a", "/path/b"]\n'
            "passive_paths = [\n"
            '  "/path/to/passive-workspace", # trailing comma + comment\n'
            '  "/path/with/\\"quotes\\"#hash",\n'
            "]\n"
            "disabled_paths = []\n"
        )
        parsed = _parse_minimal_toml(raw)
        if _stdlib_tomllib is not None:
            self.assertEqual(parsed, _stdlib_tomllib.loads(raw))
        self.assertEqual(parsed["budget"]["l1_max_tokens"], 1024)
        self.assertEqual(
            parsed["sync"]["vault_remote_url"],
            "https://example.com/#section-sync",
        )
        self.assertEqual(
            parsed["workspace_policies"]["passive_paths"],
            ["/path/to/passive-workspace", '/path/with/"quotes"#hash'],
        )

    def test_example_toml_parity_and_fallback_mode(self) -> None:
        example_raw = (REPO_ROOT / "config.example.toml").read_text(encoding="utf-8")
        minimal = _parse_minimal_toml(example_raw)
        if _stdlib_tomllib is not None:
            self.assertEqual(minimal, _stdlib_tomllib.loads(example_raw))
        self.assertEqual(minimal["core"]["host_id"], "workstation-alpha")
        self.assertEqual(minimal["budget"]["l0_max_tokens"], 300)
        self.assertEqual(minimal["workspace_policies"]["passive_paths"], [])
        self.assertEqual(minimal["workspace_policies"]["disabled_paths"], [])
        defaults = config.default_config()
        self.assertEqual(defaults["workspace_policies"]["passive_paths"], [])
        self.assertEqual(defaults["workspace_policies"]["disabled_paths"], [])

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_file = base / "config.toml"
            cache_dir = base / "cache"
            cfg_file.write_text(example_raw, encoding="utf-8")

            with (
                mock.patch.object(config, "_tomllib", None),
                mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False),
            ):
                loaded = load_config(cfg_file)

            self.assertEqual(loaded, minimal)
            log_path = cache_dir / "mipmem.log"
            self.assertTrue(log_path.exists())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("CP-CFG-01", log_text)
            self.assertIn("local_only", log_text)

    def test_save_config_roundtrip_and_invalid_toml_errors(self) -> None:
        tbd_marker = "[" + "TBD]"
        sample = {
            "core": {
                "host_id": "host-alpha",
                "vault_path": "~/.local/share/mipmem/vault",
                "locale": "en",
            },
            "sync": {
                "backend": "none",
                "vault_remote_url": tbd_marker,
                "auto_sync_on_stop": False,
            },
            "budget": {
                "l0_max_tokens": 300,
                "l1_max_tokens": 450,
                "topic_shift_novelty": 0.70,
            },
            "workspace_policies": {
                "default": "active",
                "passive_paths": ["/path/to/passive-workspace"],
                "disabled_paths": ["/path/to/secret#dir"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_file = base / "cfg" / "config.toml"
            with mock.patch.dict(
                os.environ,
                {"MIPMEM_CACHE_DIR": str(base / "cache")},
                clear=False,
            ):
                saved_path = save_config(sample, cfg_file)
                self.assertEqual(saved_path, cfg_file)
                self.assertEqual(load_config(cfg_file), sample)
                with mock.patch.object(config, "_tomllib", None):
                    self.assertEqual(load_config(cfg_file), sample)

            bad_cases = [
                ("[core\nhost_id = \"a\"\n", "line 1"),
                ("[core]\nbad_line_without_equals\n", "line 2"),
                ('[core]\nhost_id = "unterminated\n', "line 2"),
                ("[core]\nflag = True\n", "line 2"),
            ]
            for bad_raw, expected_line in bad_cases:
                bad_file = base / "bad.toml"
                bad_file.write_text(bad_raw, encoding="utf-8")
                with self.assertRaises(MipmemError) as ctx_native:
                    load_config(bad_file)
                self.assertEqual(ctx_native.exception.code, CONFIG_INVALID_TOML)

                with mock.patch.object(config, "_tomllib", None):
                    with self.assertRaises(MipmemError) as ctx_fallback:
                        load_config(bad_file)
                self.assertEqual(ctx_fallback.exception.code, CONFIG_INVALID_TOML)
                self.assertIn(expected_line, ctx_fallback.exception.message.lower())

    def test_sync_folder_path_default_roundtrip_and_legacy_compat(self) -> None:
        defaults = config.default_config()
        self.assertIn("folder_path", defaults["sync"])
        self.assertEqual(defaults["sync"]["folder_path"], "")

        example_raw = (REPO_ROOT / "config.example.toml").read_text(encoding="utf-8")
        parsed_example = _parse_minimal_toml(example_raw)
        self.assertEqual(parsed_example["sync"].get("folder_path"), "")
        if _stdlib_tomllib is not None:
            self.assertEqual(_stdlib_tomllib.loads(example_raw)["sync"].get("folder_path"), "")

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_file = base / "config.toml"
            cache_dir = base / "cache"
            tbd_marker = "[" + "TBD]"
            cfg_folder = config.default_config()
            cfg_folder["core"]["vault_path"] = str(base / "vault")
            cfg_folder["sync"]["backend"] = "folder"
            cfg_folder["sync"]["vault_remote_url"] = tbd_marker
            cfg_folder["sync"]["folder_path"] = str(base / "shared_sync")

            save_config(cfg_folder, cfg_file)
            with mock.patch.dict(os.environ, {"MIPMEM_CACHE_DIR": str(cache_dir)}, clear=False):
                loaded = load_config(cfg_file)
            self.assertEqual(loaded["sync"]["folder_path"], str(base / "shared_sync"))
            log_file = cache_dir / "mipmem.log"
            if log_file.is_file():
                self.assertNotIn('"local_only"', log_file.read_text(encoding="utf-8"))

            legacy_file = base / "legacy.toml"
            legacy_file.write_text(
                '[core]\nhost_id = "host-alpha"\n[sync]\nbackend = "none"\n',
                encoding="utf-8",
            )
            legacy_loaded = load_config(legacy_file)
            self.assertNotIn("folder_path", legacy_loaded["sync"])


class TestWorkspacePolicy(unittest.TestCase):
    """Verify 3-mode Workspace Policy evaluation, symlink normalization, and multi-root priority."""

    def test_single_path_nested_boundary_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            passive_dir = base / "passive"
            passive_nested = passive_dir / "Projects" / "sub"
            passive_other = base / "passive-other"
            secret_dir = base / "sensitive"
            active_dir = base / "work"
            symlink_to_passive = base / "passive_link"

            passive_nested.mkdir(parents=True)
            passive_other.mkdir(parents=True)
            secret_dir.mkdir(parents=True)
            active_dir.mkdir(parents=True)
            try:
                symlink_to_passive.symlink_to(passive_dir, target_is_directory=True)
                has_symlink = True
            except OSError:
                has_symlink = False

            cfg = {
                "workspace_policies": {
                    "default": "active",
                    "passive_paths": [str(passive_dir)],
                    "disabled_paths": [str(secret_dir)],
                }
            }

            self.assertEqual(resolve_workspace_policy(str(secret_dir / "sub"), cfg), "disabled")
            self.assertEqual(resolve_workspace_policy(passive_nested, cfg), "passive")
            self.assertEqual(resolve_workspace_policy(passive_other, cfg), "active")
            self.assertEqual(resolve_workspace_policy(active_dir, cfg), "active")
            if has_symlink:
                self.assertEqual(resolve_workspace_policy(symlink_to_passive, cfg), "passive")

    def test_multi_root_most_restrictive_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            passive_dir = base / "passive"
            disabled_dir = base / "disabled"
            active_dir = base / "active"
            for folder in (passive_dir, disabled_dir, active_dir):
                folder.mkdir()

            cfg = {
                "workspace_policies": {
                    "default": "active",
                    "passive_paths": [str(passive_dir)],
                    "disabled_paths": [str(disabled_dir)],
                }
            }

            self.assertEqual(
                resolve_workspace_policy([active_dir, str(passive_dir)], cfg),
                "passive",
            )
            self.assertEqual(
                resolve_workspace_policy([active_dir, passive_dir, disabled_dir], cfg),
                "disabled",
            )
            self.assertEqual(
                resolve_workspace_policy((passive_dir, disabled_dir), cfg),
                "disabled",
            )


if __name__ == "__main__":
    unittest.main()
