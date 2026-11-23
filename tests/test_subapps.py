"""Tests for per-host private sub-apps telemetry, L0/fleet summary, and apps.d delegation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Optional
import unittest
from unittest import mock

from mipmem.core.diagnostics import (
    CP_SUB_01,
    SUBAPP_EXEC_FAILED,
    SUBAPP_EXEC_TIMEOUT,
    SUBAPP_UNTRUSTED_PERMS,
    MipmemError,
    RotatingJsonLogger,
)
from mipmem.core.fts_recall import sync_fts_index
from mipmem.core.subapps import (
    MAX_DELEGATE_TIMEOUT_SEC,
    SubAppTelemetry,
    build_fleet_status,
    discover_app_delegates,
    format_fleet_markdown,
    format_l0_telemetry_lines,
    load_subapp_telemetry,
    run_app_delegate,
)
from mipmem.core.vault import init_vault


def _write_telemetry_json(
    vault_dir: Path,
    host_id: str,
    file_stem: str,
    payload: dict[str, object],
) -> Path:
    """Write a synthetic telemetry JSON status file under hosts/<host_id>/telemetry/."""
    telemetry_dir = vault_dir / "hosts" / host_id / "telemetry"
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    target = telemetry_dir / f"{file_stem}.status.json"
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


class TestSubAppTelemetryReader(unittest.TestCase):
    """Verify safe reading of telemetry/*.status.json and detail path guards (Task 1.1)."""

    def test_loads_detail_file_and_legacy_detail_path_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            log_path = root / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_path)
            init_vault(vault_dir, host_id="host-alpha")
            init_vault(vault_dir, host_id="host-beta")

            alpha_detail = vault_dir / "hosts" / "host-alpha" / "telemetry" / "sys-update.detail.md"
            alpha_detail.parent.mkdir(parents=True, exist_ok=True)
            alpha_detail.write_text("# sys-update details\nAll good.\n", encoding="utf-8")

            beta_detail = vault_dir / "hosts" / "host-beta" / "telemetry" / "backup_job.detail.md"
            beta_detail.parent.mkdir(parents=True, exist_ok=True)
            beta_detail.write_text("# backup_job details\nPending.\n", encoding="utf-8")

            _write_telemetry_json(
                vault_dir,
                "host-beta",
                "backup_job",
                {
                    "schema_version": "1",
                    "app_id": "backup_job",
                    "status": "warn",
                    "badge": "1 pending",
                    "l0_summary": "1 snapshot pending push",
                    "alert_on_l0": True,
                    "detail_path": "telemetry/backup_job.detail.md",
                    "updated_at": "2026-11-23T09:00:00Z",
                },
            )
            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "sys-update",
                {
                    "schema_version": "1",
                    "app_id": "sys-update",
                    "status": "ok",
                    "badge": "0 pending",
                    "l0_summary": "OS packages current",
                    "alert_on_l0": False,
                    "detail_file": "telemetry/sys-update.detail.md",
                    "detail_path": "telemetry/ignored-fallback.detail.md",
                    "updated_at": "2026-11-23T09:05:00Z",
                },
            )

            items = load_subapp_telemetry(vault_dir, logger=logger)
            self.assertEqual(
                [(item.host_id, item.app_id) for item in items],
                [("host-alpha", "sys-update"), ("host-beta", "backup_job")],
            )
            self.assertEqual(items[0].status, "ok")
            self.assertEqual(items[0].detail_file, "telemetry/sys-update.detail.md")
            self.assertFalse(items[0].alert_on_l0)
            self.assertEqual(items[1].status, "warn")
            self.assertEqual(items[1].detail_file, "telemetry/backup_job.detail.md")
            self.assertTrue(items[1].alert_on_l0)
            self.assertEqual(items[0].to_dict()["app_id"], "sys-update")

            host_filtered = load_subapp_telemetry(vault_dir, host_id="host-beta")
            self.assertEqual(len(host_filtered), 1)
            self.assertEqual(host_filtered[0].app_id, "backup_job")

            log_records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertTrue(
                any(
                    rec["event"] == CP_SUB_01
                    and rec.get("details", {}).get("action") == "telemetry_scan"
                    for rec in log_records
                )
            )

    def test_blocks_detail_path_traversal_absolute_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            outside = root / "outside_secret.txt"
            outside.write_text("TOP SECRET\n", encoding="utf-8")
            init_vault(vault_dir, host_id="host-alpha")

            telemetry_dir = vault_dir / "hosts" / "host-alpha" / "telemetry"
            telemetry_dir.mkdir(parents=True, exist_ok=True)
            (telemetry_dir / "evil-link.md").symlink_to(outside)

            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "app-rel-escape",
                {"app_id": "app-rel-escape", "status": "ok", "detail_file": "../../etc/passwd"},
            )
            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "app-abs-escape",
                {"app_id": "app-abs-escape", "status": "ok", "detail_file": "/etc/passwd"},
            )
            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "app-symlink-escape",
                {
                    "app_id": "app-symlink-escape",
                    "status": "warn",
                    "detail_file": "telemetry/evil-link.md",
                },
            )
            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "app-dot-self",
                {"app_id": "app-dot-self", "status": "ok", "detail_file": "."},
            )

            items = load_subapp_telemetry(vault_dir, host_id="host-alpha")
            self.assertEqual(len(items), 4)
            for item in items:
                self.assertIsNone(item.detail_file, msg=f"Leaked detail_file for {item.app_id}")

    def test_skips_invalid_slugs_statuses_corrupt_json_and_symlink_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            init_vault(vault_dir, host_id="host-alpha")
            telemetry_dir = vault_dir / "hosts" / "host-alpha" / "telemetry"
            telemetry_dir.mkdir(parents=True, exist_ok=True)

            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "valid-app",
                {"app_id": "valid-app", "status": "OK", "badge": "clean"},
            )
            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "bad-slug",
                {"app_id": "../evil", "status": "ok"},
            )
            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "UpperCase",
                {"status": "ok"},
            )
            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "bad-status",
                {"app_id": "bad-status", "status": "exploded"},
            )
            (telemetry_dir / "corrupt.status.json").write_text("{invalid json", encoding="utf-8")
            external = root / "ext.status.json"
            external.write_text(json.dumps({"app_id": "sym-app", "status": "ok"}), encoding="utf-8")
            (telemetry_dir / "sym-app.status.json").symlink_to(external)

            items = load_subapp_telemetry(vault_dir, host_id="host-alpha")
            self.assertEqual([(item.app_id, item.status) for item in items], [("valid-app", "ok")])

    def test_fts_index_telemetry_prefers_detail_file_and_blocks_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_dir = Path(tmp_dir) / "vault"
            init_vault(vault_dir, host_id="host-alpha")
            telemetry_dir = vault_dir / "hosts" / "host-alpha" / "telemetry"
            telemetry_dir.mkdir(parents=True, exist_ok=True)
            (telemetry_dir / "disk-check.detail.md").write_text("OK\n", encoding="utf-8")
            (telemetry_dir / "legacy-app.detail.md").write_text("OK\n", encoding="utf-8")

            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "disk-check",
                {
                    "app_id": "disk-check",
                    "status": "ok",
                    "badge": "42% used",
                    "l0_summary": "Root SSD healthy",
                    "detail_file": "telemetry/disk-check.detail.md",
                },
            )
            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "legacy-app",
                {
                    "app_id": "legacy-app",
                    "status": "warn",
                    "badge": "legacy",
                    "l0_summary": "Using detail_path",
                    "detail_path": "telemetry/legacy-app.detail.md",
                },
            )
            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "evil-app",
                {
                    "app_id": "evil-app",
                    "status": "warn",
                    "detail_file": "../../etc/shadow",
                    "detail_path": "/etc/passwd",
                },
            )

            sync_fts_index(vault_dir)
            db_path = vault_dir / ".cache" / "global_fts.db"
            conn = sqlite3.connect(str(db_path))
            try:
                rows = dict(
                    conn.execute(
                        "SELECT app_id, detail_path FROM subapp_telemetry "
                        "WHERE host_id = 'host-alpha'"
                    ).fetchall()
                )
            finally:
                conn.close()

            self.assertEqual(rows["disk-check"], "telemetry/disk-check.detail.md")
            self.assertEqual(rows["legacy-app"], "telemetry/legacy-app.detail.md")
            self.assertIsNone(rows["evil-app"])


class TestL0TelemetryAndFleetPresenter(unittest.TestCase):
    """Verify L0 radar budget/collapse rules, build_fleet_status, and GFM table rendering."""

    def test_format_l0_empty_collapsed_and_alert_rules_within_budget(self) -> None:
        from mipmem.core.mipmap import render_l0_radar
        from mipmem.core.sessions import SessionRecord

        self.assertEqual(format_l0_telemetry_lines([]), [])
        base_sessions = [
            SessionRecord(
                conversation_id=f"conv-fill-{i:02d}",
                host_id="host-alpha",
                surface="ide",
                last_modified_time=f"2026-11-23T0{i % 9}:00:00Z",
                step_count=12,
                title=f"Long multi-host engineering discussion session number {i} for budget test",
                edited_files=["src/mipmem/core/subapps.py"],
                recent_prompts=["Check telemetry rendering budget limits"],
                last_agent_response="Completed step.",
            )
            for i in range(12)
        ]

        empty_l0 = render_l0_radar(sessions=base_sessions[:2], subapp_telemetry=[])
        self.assertNotIn("Sub-App", empty_l0.content)

        ok_items = [
            SubAppTelemetry(
                host_id="host-alpha",
                app_id="sys-update",
                status="ok",
                badge="0 pending",
                l0_summary="Packages up to date",
                alert_on_l0=False,
                detail_file=None,
                updated_at="2026-11-23T09:00:00Z",
            ),
            SubAppTelemetry(
                host_id="host-beta",
                app_id="backup-check",
                status="ok",
                badge="synced",
                l0_summary="Backup current",
                alert_on_l0=False,
                detail_file=None,
                updated_at="2026-11-23T09:01:00Z",
            ),
        ]
        collapsed_lines = format_l0_telemetry_lines(ok_items)
        self.assertEqual(len(collapsed_lines), 1)
        self.assertIn("2 ok", collapsed_lines[0])

        mixed_items = ok_items + [
            SubAppTelemetry(
                host_id="host-alpha",
                app_id="disk-watch",
                status="critical",
                badge="96% full",
                l0_summary="Root partition 96% used",
                alert_on_l0=False,
                detail_file="telemetry/disk-watch.detail.md",
                updated_at="2026-11-23T09:05:00Z",
            )
        ]
        alert_lines = format_l0_telemetry_lines(
            mixed_items,
            sync_alert="Git rebase conflict on shared/pinned_global.md",
        )
        self.assertTrue(any("SYNC WARN" in line for line in alert_lines))
        self.assertTrue(any("disk-watch" in line and "critical" in line for line in alert_lines))

        l0_capped = render_l0_radar(
            sessions=base_sessions,
            subapp_telemetry=mixed_items,
            sync_alert="Git rebase conflict detected",
        )
        self.assertLessEqual(l0_capped.est_tokens, 300)
        self.assertIn("disk-watch", l0_capped.content)
        self.assertIn("SYNC WARN", l0_capped.content)

    def test_build_fleet_status_populates_telemetry_and_sync_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            vault_dir = root / "vault"
            cache_dir = root / "cache"
            cache_dir.mkdir(parents=True)
            init_vault(vault_dir, host_id="host-alpha")
            init_vault(vault_dir, host_id="host-beta")

            _write_telemetry_json(
                vault_dir,
                "host-alpha",
                "sys-update",
                {
                    "app_id": "sys-update",
                    "status": "warn",
                    "badge": "3 updates",
                    "l0_summary": "3 security patches available",
                    "alert_on_l0": True,
                },
            )
            _write_telemetry_json(
                vault_dir,
                "host-beta",
                "backup",
                {
                    "app_id": "backup",
                    "status": "ok",
                    "badge": "clean",
                    "l0_summary": "Snapshot synced",
                },
            )

            no_sync_fleet = build_fleet_status(vault_dir, active_host_id="host-alpha")
            self.assertIsNone(no_sync_fleet["sync_status"])
            self.assertEqual(len(no_sync_fleet["subapp_telemetry"]), 2)

            (cache_dir / "sync_status.json").write_text(
                json.dumps(
                    {
                        "backend": "git",
                        "conflict": True,
                        "error_code": "SYNC_CONFLICT_DETECTED",
                        "last_sync_utc": "2026-11-23T10:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            fleet = build_fleet_status(
                vault_dir,
                active_host_id="host-alpha",
                cache_dir=cache_dir,
            )
            self.assertTrue(fleet["vault_initialized"])
            self.assertEqual(fleet["sync_status"]["error_code"], "SYNC_CONFLICT_DETECTED")
            md = format_fleet_markdown(fleet, surface="ide")
            self.assertIn("host-alpha", md)
            self.assertIn("sys-update", md)
            self.assertIn("SYNC_CONFLICT_DETECTED", md)


class TestAppsDCommandDelegator(unittest.TestCase):
    """Verify RCE-safe local apps.d discovery, permission guards, and timeouts (Task 1.3)."""

    def _create_trusted_app(
        self,
        cfg_dir: Path,
        subcmd: str,
        script_body: str,
        *,
        extra_manifest: Optional[dict[str, object]] = None,
    ) -> tuple[Path, Path]:
        apps_dir = cfg_dir / "apps.d"
        bin_dir = cfg_dir / "bin"
        apps_dir.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(apps_dir, 0o700)

        script_path = bin_dir / f"{subcmd}.py"
        script_path.write_text("#!/usr/bin/env python3\n" + script_body, encoding="utf-8")
        os.chmod(script_path, 0o700)

        payload: dict[str, object] = {
            "subcommand": subcmd,
            "executable": str(script_path),
            "args": ["--mode=check"],
            "description": f"Test {subcmd} subapp",
            "timeout_sec": 5,
        }
        if extra_manifest:
            payload.update(extra_manifest)
        manifest_path = apps_dir / f"{subcmd}.json"
        manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        return manifest_path, script_path

    def test_runs_trusted_local_delegate_with_shell_false_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg_dir = root / "config"
            vault_dir = root / "vault"
            log_path = root / "mipmem.log"
            init_vault(vault_dir, host_id="host-alpha")
            logger = RotatingJsonLogger(log_path=log_path)
            secret = "ghp_" + "C" * 24
            self._create_trusted_app(
                cfg_dir,
                "sys-check",
                "import sys\n"
                f"print('args=' + repr(sys.argv[1:]) + ' tok={secret}')\n",
            )

            delegates = discover_app_delegates(cfg_dir, vault_root=vault_dir, logger=logger)
            self.assertIn("sys-check", delegates)
            out = run_app_delegate(
                "sys-check",
                ["$(echo pwned); rm -rf /tmp/fake"],
                config_dir=cfg_dir,
                vault_root=vault_dir,
                logger=logger,
            )
            self.assertIn("$(echo pwned); rm -rf /tmp/fake", out)
            self.assertIn("[REDACTED:GITHUB_PAT]", out)
            self.assertNotIn(secret, out)

            entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertTrue(
                any(
                    rec["event"] == CP_SUB_01
                    and rec.get("details", {}).get("action") == "delegate_exec"
                    and rec.get("details", {}).get("app_id") == "sys-check"
                    for rec in entries
                )
            )

    def test_rejects_vault_embedded_symlink_bad_perms_and_wrong_uid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg_dir = root / "config"
            vault_dir = root / "vault"
            init_vault(vault_dir, host_id="host-alpha")

            # 1. Executable stored inside synced vault MUST be rejected even if chmod 0700
            evil_exec = vault_dir / "hosts" / "host-alpha" / "evil.py"
            evil_exec.write_text("#!/usr/bin/env python3\nprint('pwn')\n", encoding="utf-8")
            os.chmod(evil_exec, 0o700)
            apps_dir = cfg_dir / "apps.d"
            apps_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(apps_dir, 0o700)
            vault_manifest = apps_dir / "evil-vault.json"
            vault_manifest.write_text(
                json.dumps({"subcommand": "evil-vault", "executable": str(evil_exec)}),
                encoding="utf-8",
            )
            os.chmod(vault_manifest, 0o600)

            with self.assertRaises(MipmemError) as ctx_v:
                run_app_delegate(
                    "evil-vault", [], config_dir=cfg_dir, vault_root=vault_dir
                )
            self.assertEqual(ctx_v.exception.code, SUBAPP_UNTRUSTED_PERMS)
            self.assertNotIn("evil-vault", discover_app_delegates(cfg_dir, vault_root=vault_dir))

            # 2. Config dir itself inside vault -> never discovered / always rejected
            inside_cfg = vault_dir / "apps_inside"
            self._create_trusted_app(inside_cfg, "inside-cmd", "print('x')\n")
            self.assertEqual(discover_app_delegates(inside_cfg, vault_root=vault_dir), {})
            with self.assertRaises(MipmemError) as ctx_ic:
                run_app_delegate("inside-cmd", [], config_dir=inside_cfg, vault_root=vault_dir)
            self.assertEqual(ctx_ic.exception.code, SUBAPP_UNTRUSTED_PERMS)

            # 3. Group/world-writable manifest (0666) & executable (0777)
            bad_m, ok_s = self._create_trusted_app(cfg_dir, "perm-bad-m", "print('m')\n")
            os.chmod(bad_m, 0o666)
            with self.assertRaises(MipmemError) as ctx_pm:
                run_app_delegate("perm-bad-m", [], config_dir=cfg_dir, vault_root=vault_dir)
            self.assertEqual(ctx_pm.exception.code, SUBAPP_UNTRUSTED_PERMS)
            os.chmod(bad_m, 0o600)
            os.chmod(ok_s, 0o777)
            with self.assertRaises(MipmemError) as ctx_ps:
                run_app_delegate("perm-bad-m", [], config_dir=cfg_dir, vault_root=vault_dir)
            self.assertEqual(ctx_ps.exception.code, SUBAPP_UNTRUSTED_PERMS)
            os.chmod(ok_s, 0o700)

            # 4. Symlinked manifest and symlinked executable rejected
            _, real_s = self._create_trusted_app(cfg_dir, "real-app", "print('ok')\n")
            sym_m = apps_dir / "sym-m.json"
            sym_m.symlink_to(apps_dir / "real-app.json")
            with self.assertRaises(MipmemError) as ctx_sm:
                run_app_delegate("sym-m", [], config_dir=cfg_dir, vault_root=vault_dir)
            self.assertEqual(ctx_sm.exception.code, SUBAPP_UNTRUSTED_PERMS)
            sym_m.unlink()

            sym_s = cfg_dir / "bin" / "sym_exec.py"
            sym_s.symlink_to(real_s)
            (apps_dir / "sym-exec.json").write_text(
                json.dumps({"subcommand": "sym-exec", "executable": str(sym_s)}),
                encoding="utf-8",
            )
            os.chmod(apps_dir / "sym-exec.json", 0o600)
            with self.assertRaises(MipmemError) as ctx_ss:
                run_app_delegate("sym-exec", [], config_dir=cfg_dir, vault_root=vault_dir)
            self.assertEqual(ctx_ss.exception.code, SUBAPP_UNTRUSTED_PERMS)

            # 5. UID mismatch rejected
            with mock.patch("mipmem.core.subapps.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaises(MipmemError) as ctx_uid:
                    run_app_delegate("real-app", [], config_dir=cfg_dir, vault_root=vault_dir)
                self.assertEqual(ctx_uid.exception.code, SUBAPP_UNTRUSTED_PERMS)

    def test_enforces_timeout_cap_blocks_core_collisions_and_handles_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg_dir = root / "config"
            vault_dir = root / "vault"
            init_vault(vault_dir, host_id="host-alpha")

            self._create_trusted_app(
                cfg_dir,
                "clamped-app",
                "print('ok')\n",
                extra_manifest={"timeout_sec": 999},
            )
            delegates = discover_app_delegates(cfg_dir, vault_root=vault_dir)
            self.assertEqual(delegates["clamped-app"].timeout_sec, MAX_DELEGATE_TIMEOUT_SEC)

            self._create_trusted_app(
                cfg_dir,
                "slow-app",
                "import time\ntime.sleep(1.5)\n",
                extra_manifest={"timeout_sec": 0.15},
            )
            with self.assertRaises(MipmemError) as ctx_to:
                run_app_delegate("slow-app", [], config_dir=cfg_dir, vault_root=vault_dir)
            self.assertEqual(ctx_to.exception.code, SUBAPP_EXEC_TIMEOUT)

            self._create_trusted_app(cfg_dir, "recall", "print('hijack')\n")
            self.assertNotIn("recall", discover_app_delegates(cfg_dir, vault_root=vault_dir))
            with self.assertRaises(MipmemError) as ctx_core:
                run_app_delegate("recall", [], config_dir=cfg_dir, vault_root=vault_dir)
            self.assertEqual(ctx_core.exception.code, SUBAPP_EXEC_FAILED)

            secret = "ghp_" + "E" * 24
            self._create_trusted_app(
                cfg_dir,
                "fail-app",
                f"import sys\nsys.stderr.write('boom {secret}\\n')\nsys.exit(2)\n",
            )
            with self.assertRaises(MipmemError) as ctx_fail:
                run_app_delegate("fail-app", [], config_dir=cfg_dir, vault_root=vault_dir)
            self.assertEqual(ctx_fail.exception.code, SUBAPP_EXEC_FAILED)
            self.assertNotIn(secret, ctx_fail.exception.message)
            self.assertIn("[REDACTED:GITHUB_PAT]", ctx_fail.exception.message)


if __name__ == "__main__":
    unittest.main()
