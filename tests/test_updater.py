"""Tests for opt-in self-update install-mode detection and atomic zipapp updates."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from mipmem.core.diagnostics import CP_UPD_01, UPDATE_VERIFICATION_FAILED, MipmemError
from mipmem.core.diagnostics import RotatingJsonLogger
from mipmem.core.updater import (
    compare_versions,
    detect_install_mode,
    parse_sha256sums,
    recommended_upgrade_command,
    run_self_update,
    validate_release_url,
)


class TestUpdaterInstallModeAndManifest(unittest.TestCase):
    """Verify install-mode classification, HTTPS allowlists, and SHA256SUMS parsing."""

    def test_detect_install_mode_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)

            # 1. zipapp (.pyz archive member path)
            pyz_member = root / "opt" / "mipmem.pyz" / "mipmem" / "core" / "updater.py"
            mode, target = detect_install_mode(module_file=pyz_member, env={})
            self.assertEqual(mode, "zipapp")
            self.assertEqual(target, root / "opt" / "mipmem.pyz")

            # 2. plugin (PLUGIN_ROOT env or ancestor plugin.json without .git)
            plugin_dir = root / "plugin_install"
            plugin_mod = plugin_dir / "src" / "mipmem" / "core" / "updater.py"
            plugin_mod.parent.mkdir(parents=True)
            plugin_mod.write_text("# marker\n", encoding="utf-8")
            (plugin_dir / "plugin.json").write_text('{"name":"mipmem"}\n', encoding="utf-8")

            mode_p_env, tgt_p_env = detect_install_mode(
                module_file=plugin_mod,
                env={"PLUGIN_ROOT": str(plugin_dir)},
            )
            self.assertEqual(mode_p_env, "plugin")
            self.assertEqual(tgt_p_env, plugin_dir.resolve())

            mode_p_walk, tgt_p_walk = detect_install_mode(module_file=plugin_mod, env={})
            self.assertEqual(mode_p_walk, "plugin")
            self.assertEqual(tgt_p_walk, plugin_dir.resolve())

            # 3. uv tool environment
            uv_mod = (
                root
                / ".local"
                / "share"
                / "uv"
                / "tools"
                / "mipmem"
                / "lib"
                / "python3.12"
                / "site-packages"
                / "mipmem"
                / "core"
                / "updater.py"
            )
            mode_uv, _ = detect_install_mode(module_file=uv_mod, env={})
            self.assertEqual(mode_uv, "uv")

            # 4. pipx environment
            pipx_mod = (
                root
                / ".local"
                / "pipx"
                / "venvs"
                / "mipmem"
                / "lib"
                / "python3.12"
                / "site-packages"
                / "mipmem"
                / "core"
                / "updater.py"
            )
            mode_pipx, _ = detect_install_mode(module_file=pipx_mod, env={})
            self.assertEqual(mode_pipx, "pipx")

            # 5. standard pip site-packages
            pip_mod = (
                root
                / "venv"
                / "lib"
                / "python3.12"
                / "site-packages"
                / "mipmem"
                / "core"
                / "updater.py"
            )
            mode_pip, _ = detect_install_mode(module_file=pip_mod, env={})
            self.assertEqual(mode_pip, "pip")

            # 6. git-dev repo (takes precedence over plugin.json in working clone)
            git_dir = root / "dev_repo"
            git_mod = git_dir / "src" / "mipmem" / "core" / "updater.py"
            git_mod.parent.mkdir(parents=True)
            git_mod.write_text("# dev\n", encoding="utf-8")
            (git_dir / ".git").mkdir()
            (git_dir / "plugin.json").write_text('{"name":"mipmem"}\n', encoding="utf-8")
            mode_git, tgt_git = detect_install_mode(module_file=git_mod, env={})
            self.assertEqual(mode_git, "git-dev")
            self.assertEqual(tgt_git, git_dir.resolve())

    def test_recommended_upgrade_command_all_modes(self) -> None:
        self.assertIn("uv tool upgrade mipmem", recommended_upgrade_command("uv"))
        self.assertIn("pipx upgrade mipmem", recommended_upgrade_command("pipx"))
        self.assertIn("pip install --upgrade mipmem", recommended_upgrade_command("pip"))
        self.assertIn("Plugin", recommended_upgrade_command("plugin"))
        self.assertIn("git pull", recommended_upgrade_command("git-dev"))
        self.assertIn("self-update", recommended_upgrade_command("zipapp"))

    def test_validate_release_url_strict_allowlist(self) -> None:
        allowed_urls = (
            "https://api.github.com/repos/pageel/mipmem/releases/latest",
            "https://api.github.com/repos/pageel/mipmem/releases/tags/v1.0.0",
            "https://github.com/pageel/mipmem/releases/download/v1.0.0/mipmem.pyz",
            "https://objects.githubusercontent.com/github-production-release-asset/1/mipmem.pyz",
            "https://release-assets.githubusercontent.com/assets/SHA256SUMS",
        )
        for url in allowed_urls:
            self.assertEqual(validate_release_url(url), url)

        disallowed_urls = (
            "http://github.com/pageel/mipmem/releases/download/v1.0.0/mipmem.pyz",
            "https://github.com/other/mipmem/releases/download/v1.0.0/mipmem.pyz",
            "https://api.github.com/repos/other/mipmem/releases/latest",
            "https://example.com/mipmem.pyz",
            "https://" + "user:secret@" + "github.com/pageel/mipmem/releases/latest",
            "http://127.0.0.1:8080/releases/latest",
            "file:///tmp/mipmem.pyz",
        )
        for bad_url in disallowed_urls:
            with self.subTest(bad_url=bad_url):
                with self.assertRaises(MipmemError) as ctx:
                    validate_release_url(bad_url)
                self.assertEqual(ctx.exception.code, UPDATE_VERIFICATION_FAILED)

        # Internal localhost override works only when explicitly enabled
        local_ip = "http://127.0.0.1:9001/releases/latest"
        local_name = "http://localhost:9001/download/mipmem.pyz"
        self.assertEqual(validate_release_url(local_ip, allow_localhost_http=True), local_ip)
        self.assertEqual(validate_release_url(local_name, allow_localhost_http=True), local_name)
        with self.assertRaises(MipmemError):
            validate_release_url("http://example.com/releases/latest", allow_localhost_http=True)

    def test_parse_sha256sums_valid_and_malformed(self) -> None:
        hash_a = "A" * 64
        hash_b = "0123456789abcdef" * 4
        manifest = (
            "# Release checksums\n\n"
            f"{hash_a}  mipmem.pyz\n"
            f"{hash_b} *dist/mipmem-1.0.0.tar.gz\n"
        )
        parsed = parse_sha256sums(manifest)
        self.assertEqual(parsed["mipmem.pyz"], hash_a.lower())
        self.assertEqual(parsed["mipmem-1.0.0.tar.gz"], hash_b)
        self.assertEqual(parsed["dist/mipmem-1.0.0.tar.gz"], hash_b)

        for bad in ("", "   \n# only comments\n", "not-a-hash  mipmem.pyz\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(MipmemError) as ctx:
                    parse_sha256sums(bad)
                self.assertEqual(ctx.exception.code, UPDATE_VERIFICATION_FAILED)

    def test_compare_versions(self) -> None:
        self.assertEqual(compare_versions("0.3.0", "1.0.0"), -1)
        self.assertEqual(compare_versions("v1.0.0", "1.0.0"), 0)
        self.assertEqual(compare_versions("1.0.0-rc.1", "1.0.0"), -1)
        self.assertEqual(compare_versions("1.2.0", "1.0.10"), 1)
        self.assertEqual(compare_versions("v0.3.10", "v0.3.9"), 1)


def _make_synthetic_pyz_bytes(version: str, *, exit_code: int = 0) -> bytes:
    """Build a standalone runnable .pyz payload answering `health --json`."""
    main_code = (
        "import json, sys\n"
        "if len(sys.argv) >= 2 and sys.argv[1] == 'health':\n"
        f"    sys.stdout.write(json.dumps({{'success': {exit_code == 0}, "
        f"'version': '{version}'}}) + '\\n')\n"
        f"    raise SystemExit({exit_code})\n"
        "raise SystemExit(0)\n"
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_pyz = Path(tmp_dir) / "sample.pyz"
        with zipfile.ZipFile(out_pyz, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("__main__.py", main_code.encode("utf-8"))
        return out_pyz.read_bytes()


def _tamper_hex_first_char(hex_digest: str) -> str:
    first = "1" if hex_digest[0] != "1" else "2"
    return first + hex_digest[1:]


class TestAtomicZipappSelfUpdater(unittest.TestCase):
    """Integration tests against a local 127.0.0.1 HTTP release server."""

    def _start_release_server(
        self,
        routes: dict[str, tuple[int, bytes, dict[str, str]]],
    ) -> ThreadingHTTPServer:
        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                entry = routes.get(self.path)
                if entry is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                status, body, headers = entry
                self.send_response(status)
                for h_k, h_v in headers.items():
                    self.send_header(h_k, h_v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _base_routes(
        self,
        *,
        port_box: list[int],
        pyz_bytes: bytes,
        tag_name: str = "v1.0.0",
        checksums_override: bytes | None = None,
        redirect_url: str | None = None,
    ) -> dict[str, tuple[int, bytes, dict[str, str]]]:
        digest = hashlib.sha256(pyz_bytes).hexdigest()
        sums_bytes = (
            checksums_override
            if checksums_override is not None
            else f"{digest}  mipmem.pyz\n".encode("utf-8")
        )

        class _RouteMap(dict):  # type: ignore[type-arg]
            pass

        routes: dict[str, tuple[int, bytes, dict[str, str]]] = _RouteMap()

        def _release_body() -> bytes:
            port = port_box[0]
            return json.dumps(
                {
                    "tag_name": tag_name,
                    "assets": [
                        {
                            "name": "mipmem.pyz",
                            "browser_download_url": f"http://127.0.0.1:{port}/download/mipmem.pyz",
                        },
                        {
                            "name": "SHA256SUMS",
                            "browser_download_url": f"http://127.0.0.1:{port}/download/SHA256SUMS",
                        },
                    ],
                }
            ).encode("utf-8")

        routes["/releases/latest"] = (
            200,
            _release_body(),
            {"Content-Type": "application/json"},
        )
        routes["/releases/tags/v1.0.0"] = (
            200,
            _release_body(),
            {"Content-Type": "application/json"},
        )
        routes["/download/SHA256SUMS"] = (
            200,
            sums_bytes,
            {"Content-Type": "text/plain"},
        )
        if redirect_url is not None:
            routes["/download/mipmem.pyz"] = (302, b"", {"Location": redirect_url})
        else:
            routes["/download/mipmem.pyz"] = (
                200,
                pyz_bytes,
                {"Content-Type": "application/octet-stream"},
            )
        return routes

    def _rebuild_release_json(
        self,
        routes: dict[str, tuple[int, bytes, dict[str, str]]],
        port: int,
        tag_name: str = "v1.0.0",
    ) -> None:
        release_bytes = json.dumps(
            {
                "tag_name": tag_name,
                "assets": [
                    {
                        "name": "mipmem.pyz",
                        "browser_download_url": f"http://127.0.0.1:{port}/download/mipmem.pyz",
                    },
                    {
                        "name": "SHA256SUMS",
                        "browser_download_url": f"http://127.0.0.1:{port}/download/SHA256SUMS",
                    },
                ],
            }
        ).encode("utf-8")
        routes["/releases/latest"] = (200, release_bytes, {"Content-Type": "application/json"})
        routes[f"/releases/tags/{tag_name}"] = (
            200,
            release_bytes,
            {"Content-Type": "application/json"},
        )

    def test_check_and_dry_run_do_not_modify_target(self) -> None:
        old_bytes = _make_synthetic_pyz_bytes("0.3.0")
        new_bytes = _make_synthetic_pyz_bytes("1.0.0")
        routes = self._base_routes(port_box=[0], pyz_bytes=new_bytes)
        server = self._start_release_server(routes)
        port = server.server_address[1]
        self._rebuild_release_json(routes, port)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pyz_path = root / "bin" / "mipmem.pyz"
            pyz_path.parent.mkdir(parents=True)
            pyz_path.write_bytes(old_bytes)
            mod_inside = pyz_path / "mipmem" / "core" / "updater.py"
            log_file = root / "cache" / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file)

            api_url = f"http://127.0.0.1:{port}/releases/latest"
            res_check = run_self_update(
                check_only=True,
                current_version="0.3.0",
                releases_api_url=api_url,
                module_file=mod_inside,
                env={},
                allow_localhost_http=True,
                logger=logger,
            )
            self.assertTrue(res_check.update_available)
            self.assertEqual(res_check.target_version, "1.0.0")
            self.assertFalse(res_check.applied)
            self.assertTrue(res_check.check_only)
            self.assertEqual(pyz_path.read_bytes(), old_bytes)

            res_dry = run_self_update(
                dry_run=True,
                current_version="0.3.0",
                releases_api_url=api_url,
                module_file=mod_inside,
                env={},
                allow_localhost_http=True,
                logger=logger,
            )
            self.assertTrue(res_dry.update_available)
            self.assertTrue(res_dry.dry_run)
            self.assertFalse(res_dry.applied)
            self.assertEqual(pyz_path.read_bytes(), old_bytes)
            entries = [
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertGreaterEqual(len(entries), 2)
            self.assertEqual(entries[-1]["event"], CP_UPD_01)

    def test_zipapp_self_update_atomic_success_and_cp_upd_01(self) -> None:
        old_bytes = _make_synthetic_pyz_bytes("0.3.0")
        new_bytes = _make_synthetic_pyz_bytes("1.0.0")
        routes = self._base_routes(port_box=[0], pyz_bytes=new_bytes)
        server = self._start_release_server(routes)
        port = server.server_address[1]
        self._rebuild_release_json(routes, port)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pyz_path = root / "bin" / "mipmem.pyz"
            pyz_path.parent.mkdir(parents=True)
            pyz_path.write_bytes(old_bytes)
            mod_inside = pyz_path / "mipmem" / "core" / "updater.py"
            log_file = root / "cache" / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file)

            res = run_self_update(
                target_version="v1.0.0",
                current_version="0.3.0",
                releases_api_url=f"http://127.0.0.1:{port}/releases/tags/v1.0.0",
                module_file=mod_inside,
                env={},
                allow_localhost_http=True,
                logger=logger,
            )
            self.assertTrue(res.applied)
            self.assertTrue(res.sha256_verified)
            self.assertTrue(res.health_verified)
            self.assertEqual(res.target_version, "1.0.0")
            self.assertEqual(pyz_path.read_bytes(), new_bytes)
            leftover_tmps = list(pyz_path.parent.glob(".tmp_mipmem_update_*"))
            self.assertEqual(leftover_tmps, [])

            entries = [
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertEqual(len(entries), 1)
            details = entries[0]["details"]
            self.assertEqual(entries[0]["event"], CP_UPD_01)
            for key in (
                "install_mode",
                "current_version",
                "target_version",
                "dry_run",
                "sha256_verified",
                "health_verified",
                "applied",
                "error_code",
            ):
                self.assertIn(key, details)
            self.assertTrue(details["applied"])
            self.assertTrue(details["sha256_verified"])
            self.assertTrue(details["health_verified"])

    def test_rejects_tampered_sha256_corrupt_zip_failed_health_and_redirect(self) -> None:
        old_bytes = _make_synthetic_pyz_bytes("0.3.0")
        good_new = _make_synthetic_pyz_bytes("1.0.0")
        good_digest = hashlib.sha256(good_new).hexdigest()

        # 1. 1-byte tampered SHA256SUMS
        bad_sums = f"{_tamper_hex_first_char(good_digest)}  mipmem.pyz\n".encode("utf-8")
        # 2. Corrupt ZIP archive (with matching hash so testzip catches it)
        corrupt_zip = b"corrupt-zip-data-not-valid"
        # 3. Valid ZIP whose `health --json` exits 1
        unhealthy_zip = _make_synthetic_pyz_bytes("1.0.0", exit_code=1)

        scenarios: list[tuple[str, bytes, bytes | None, str | None]] = [
            ("sha256_mismatch", good_new, bad_sums, None),
            ("corrupt_zip", corrupt_zip, None, None),
            ("unhealthy_pyz", unhealthy_zip, None, None),
            ("unsafe_redirect", good_new, None, "https://example.com/evil.pyz"),
        ]

        for label, payload, sums_override, redirect_to in scenarios:
            with self.subTest(scenario=label):
                routes = self._base_routes(
                    port_box=[0],
                    pyz_bytes=payload,
                    checksums_override=sums_override,
                    redirect_url=redirect_to,
                )
                server = self._start_release_server(routes)
                port = server.server_address[1]
                self._rebuild_release_json(routes, port)

                with tempfile.TemporaryDirectory() as tmp_dir:
                    root = Path(tmp_dir)
                    pyz_path = root / "bin" / "mipmem.pyz"
                    pyz_path.parent.mkdir(parents=True)
                    pyz_path.write_bytes(old_bytes)
                    mod_inside = pyz_path / "mipmem" / "core" / "updater.py"

                    with self.assertRaises(MipmemError) as ctx:
                        run_self_update(
                            current_version="0.3.0",
                            releases_api_url=f"http://127.0.0.1:{port}/releases/latest",
                            module_file=mod_inside,
                            env={},
                            allow_localhost_http=True,
                        )
                    self.assertEqual(ctx.exception.code, UPDATE_VERIFICATION_FAILED)
                    self.assertEqual(pyz_path.read_bytes(), old_bytes)
                    self.assertEqual(list(pyz_path.parent.glob(".tmp_mipmem_update_*")), [])

    def test_refuses_non_zipapp_install_modes_with_upgrade_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            uv_mod = root / ".local" / "share" / "uv" / "tools" / "mipmem" / "updater.py"
            uv_mod.parent.mkdir(parents=True)
            uv_mod.write_text("# marker\n", encoding="utf-8")
            log_file = root / "cache" / "mipmem.log"
            logger = RotatingJsonLogger(log_path=log_file)

            with self.assertRaises(MipmemError) as ctx:
                run_self_update(module_file=uv_mod, env={}, logger=logger)
            self.assertEqual(ctx.exception.code, UPDATE_VERIFICATION_FAILED)
            self.assertIn("uv tool upgrade mipmem", ctx.exception.message)

            # check_only works offline/without overwriting non-zipapp installations
            good_new = _make_synthetic_pyz_bytes("1.0.0")
            routes = self._base_routes(port_box=[0], pyz_bytes=good_new)
            server = self._start_release_server(routes)
            port = server.server_address[1]
            self._rebuild_release_json(routes, port)

            res = run_self_update(
                check_only=True,
                current_version="0.3.0",
                releases_api_url=f"http://127.0.0.1:{port}/releases/latest",
                module_file=uv_mod,
                env={},
                allow_localhost_http=True,
            )
            self.assertEqual(res.install_mode, "uv")
            self.assertFalse(res.applied)
            self.assertIn("uv tool upgrade mipmem", res.recommended_command)


if __name__ == "__main__":
    unittest.main()
