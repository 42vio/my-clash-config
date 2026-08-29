import importlib
import py_compile
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


# Synthetic canary values: if any of these ever appear in a tracked
# template, example, or fixture, real private data has leaked into the
# repository.
FORBIDDEN_SUBSTRINGS = (
    "198.51.100.77",
    "203.0.113.88",
    "canary-panel.example.com",
    "canary-subscription.example.com",
    "relay-placeholder.example.com",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "fedcba9876543210",
    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "synthetic-password-fixture",
    "synthetic-owner-subscription-id",
    "synthetic-friend-subscription-id",
    "safe-panel-base-path-fixture",
    "operator-encrypted-storage-fixture",
)

TRACKED_DOCUMENT_PATHS = (
    "templates/clash.yaml",
    "templates/variants/manifest.yaml",
    "templates/variants/privacy-dns.yaml",
)

LEGACY_RUNTIME_PATHS = (
    "Dockerfile",
    "compose.yaml",
    ".dockerignore",
    ".env.example",
    "config/subconverter/pref.ini",
    "config/users.example.yaml",
    "clash_sub/converter.py",
    "clash_sub/host_cli.py",
    "clash_sub/manager.py",
    "clash_sub/models.py",
    "clash_sub/publisher.py",
    "clash_sub/reference_rules.py",
    "clash_sub/releases.py",
    "clash_sub/rendering.py",
    "clash_sub/settings.py",
    "clash_sub/traffic.py",
    "clash_sub/validation.py",
    "templates/variants/balanced-win.yaml",
    "scripts/check_certificate.py",
    "scripts/install-server.sh",
    "scripts/install_server.py",
    "scripts/migrate_reference_templates.py",
    "scripts/compare_reference_configs.py",
    "scripts/server_preflight.py",
    "deploy/nginx/00-acme-http.conf.tmpl",
    "deploy/nginx/10-clash-domain.conf.tmpl",
    "deploy/nginx/10-clash-ip.conf.tmpl",
    "deploy/systemd/clash-sub-cert-check.service",
    "deploy/systemd/clash-sub-cert-check.timer",
    "deploy/systemd/clash-sub-cert-renew-failed.service",
    "deploy/systemd/clash-sub-cert-renew.service",
    "deploy/systemd/clash-sub-cert-renew.timer",
)

SUPPORTED_EXPORTS = (
    "ServiceConfig",
    "ConfigError",
    "ClashSubService",
    "ServiceError",
)

ACTIVE_RUNTIME_PATHS = (
    "bin/clash-sub",
    "clash_sub",
    "config/service.example.yaml",
    "deploy",
    "scripts/check_reality_target.py",
    "scripts/scan_tracked_secrets.py",
    "templates/clash.yaml",
    "templates/variants",
    "requirements.txt",
)

FORBIDDEN_RUNTIME_REFERENCES = (
    "publisher",
    "subconverter",
    "balanced-win",
    "refresh",
    "Certbot",
    "Docker",
)

RETAINED_TEST_NAMES = {
    "test_repository_safety.py",
    "test_secret_scan.py",
    "test_reality_target.py",
}

RETAINED_SCRIPT_ENTRY_POINTS = {
    "check_reality_target.py",
    "scan_tracked_secrets.py",
}

# The home overlay reaches the server only through a manual SFTP overwrite
# of private/home.yaml followed by `clash-sub sync`; no upload workflow may
# exist anywhere in the runtime.
HOME_UPLOAD_SURFACE_REFERENCES = (
    "home-import",
    "home_import",
    "home-upload",
    "home_upload",
    "upload-home",
    "upload_home",
)

DOCUMENTED_CLI_COMMANDS = frozenset(
    (
        "sync",
        "traffic-update",
        "status",
        "links",
        "history",
        "rollback",
        "rotate-link",
        "reinitialize-owner",
        "recover",
        "install",
        "backup",
        "template-sync",
        "mihomo-update",
        "update",
        "cert",
    )
)


class RepositorySafetyTests(unittest.TestCase):
    def test_requirements_pin_exactly_one_ruamel_yaml_version(self):
        lines = [
            line.strip()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip().lower().startswith("ruamel.yaml")
        ]

        self.assertEqual(lines, ["ruamel.yaml==0.19.1"])

    def test_only_retained_script_entry_points_compile_without_execution(self):
        scripts = ROOT / "scripts"
        self.assertEqual(
            {path.name for path in scripts.iterdir() if path.is_file()},
            RETAINED_SCRIPT_ENTRY_POINTS,
        )
        with TemporaryDirectory() as directory:
            for name in RETAINED_SCRIPT_ENTRY_POINTS:
                py_compile.compile(
                    str(scripts / name),
                    cfile=str(Path(directory) / (name + "c")),
                    doraise=True,
                )

    def test_superseded_runtime_assets_and_tests_are_absent(self):
        for relative in LEGACY_RUNTIME_PATHS:
            self.assertFalse((ROOT / relative).exists(), relative)
        self.assertEqual(
            tuple((ROOT / "deploy/systemd").glob("clash-sub-cert-*")),
            (),
        )
        legacy_tests = tuple(
            path.name
            for path in (ROOT / "tests").glob("test_*.py")
            if not path.name.startswith("test_lightweight_")
            and path.name not in RETAINED_TEST_NAMES
        )
        self.assertEqual(legacy_tests, ())

    def test_supported_package_exports_are_exact(self):
        package = importlib.import_module("clash_sub")

        self.assertEqual(tuple(package.__all__), SUPPORTED_EXPORTS)
        for name in SUPPORTED_EXPORTS:
            self.assertTrue(hasattr(package, name), name)

    def test_active_runtime_has_no_superseded_stack_references(self):
        paths = [
            str(ROOT / relative)
            for relative in ACTIVE_RUNTIME_PATHS
            if (ROOT / relative).exists()
        ]
        for reference in FORBIDDEN_RUNTIME_REFERENCES:
            result = subprocess.run(
                ["rg", "--fixed-strings", "--line-number", reference, "--", *paths],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_no_home_upload_workflow_exists_in_the_command_surface(self):
        paths = [str(ROOT / "bin" / "clash-sub"), str(ROOT / "clash_sub")]
        for reference in HOME_UPLOAD_SURFACE_REFERENCES:
            result = subprocess.run(
                ["rg", "--fixed-strings", "--line-number", reference, "--", *paths],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_cli_commands_stay_exactly_the_documented_argument_free_sync_surface(self):
        import argparse

        from clash_sub import cli

        parser = cli._parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertEqual(frozenset(subparsers.choices), DOCUMENTED_CLI_COMMANDS)
        # `sync` never grows an upload target: it accepts no arguments.
        self.assertEqual(subparsers.choices["sync"]._actions, [])

    def test_every_runtime_private_path_is_ignored(self):
        paths = (
            "private/home.yaml",
            "private/config/service.yaml",
            "private/config/users.yaml",
            "private/reference-configs/2026-08-21/My-Clash_Balanced.yaml",
            "private/reference-configs/2026-08-21/My-Clash_Balanced_Win.yaml",
            "private/reference-configs/2026-08-21/My-Clash_Privacy.yaml",
            "private/sources/owner/airport.yaml",
            "private/sources/owner/home.yaml",
            "private/staging/op/user/config.yaml",
            "private/releases/user/release/config.yaml",
            "private/current/user",
            "private/state/certificate.json",
            "private/logs/operations.jsonl",
        )
        for path in paths:
            result = subprocess.run(
                ["git", "check-ignore", "-q", path],
                cwd=ROOT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, path)

    def test_secret_scanner_pins_the_root_home_overlay(self):
        # The private-value comparison must keep covering the ignored
        # root home overlay; dropping the file from the scanner would
        # silently stop catching home credential leaks.
        source = (ROOT / "scripts" / "scan_tracked_secrets.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"home.yaml"', source)

    def test_reference_sources_are_not_tracked(self):
        for name in (
            "My-Clash_Balanced.yaml",
            "My-Clash_Balanced_Win.yaml",
            "My-Clash_Privacy.yaml",
        ):
            result = subprocess.run(
                ["git", "ls-files", "--error-unmatch", f"1/{name}"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, name)

    def test_tracked_templates_examples_and_fixtures_contain_no_private_data(self):
        for relative in TRACKED_DOCUMENT_PATHS:
            lowered = (ROOT / relative).read_text(encoding="utf-8").lower()
            for forbidden in FORBIDDEN_SUBSTRINGS:
                self.assertNotIn(
                    forbidden.lower(), lowered, f"{relative} leaks {forbidden!r}"
                )

    def test_legacy_trojan_topology_is_isolated_and_explicitly_historical(self):
        legacy = ROOT / "docs" / "legacy-trojan-topology.md"
        self.assertTrue(legacy.is_file())
        legacy_text = legacy.read_text(encoding="utf-8")
        for fact in ("1443", "8080", "trojan-web", "fallback"):
            self.assertIn(fact, legacy_text)
        for statement in ("不是新服务器的安装步骤", "不得在新服务器上执行"):
            self.assertIn(statement, legacy_text)
        active_docs = (
            "README.md",
            "DEPLOYMENT.md",
            "docs/3x-ui-setup.md",
            "docs/operations.md",
            "docs/private-data.md",
        )
        # The bare word "trojan" is no longer legacy-only: the unified-443
        # topology reserves `trojan.<域名>` → 127.0.0.1:20443 (README ADR and
        # docs/recovery.md). Only the legacy trojan-web/Jrohy stack stays
        # quarantined in the historical document.
        for relative in active_docs:
            text = (ROOT / relative).read_text(encoding="utf-8").lower()
            for term in ("jrohy", "trojan-web", "8080"):
                self.assertNotIn(term, text, f"{relative} mentions {term!r}")
