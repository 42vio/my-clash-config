import importlib
import py_compile
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml


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
    "templates/base/Clash-Compat.yaml",
    "templates/dns/Clash-Balance.yaml",
    "templates/profiles.yaml",
)

SUPERSEDED_TEMPLATE_PATHS = (
    "templates/clash.yaml",
    "templates/variants/manifest.yaml",
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
    "templates/base",
    "templates/dns",
    "templates/profiles.yaml",
    "requirements.txt",
)

FORBIDDEN_RUNTIME_REFERENCES = (
    "publisher",
    "subconverter",
    # The removed Docker stack's per-user "refresh [user-id]" command; the
    # sanctioned airport refresh flow (refresh_airport) shares no surface
    # with it, and the legacy refresh/refresh-all argv stay rejected by the
    # CLI command-surface tests.
    "_command_refresh",
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

# The retired scheduled-traffic mechanism (timer-driven traffic_update plus
# its traffic-update CLI command) is fully replaced by the socket-activated
# metadata service.  Its names may survive only in the development-history
# records under docs/superpowers/, never in active runtime code, deployment
# assets, the four project-use manuals, or the runtime tests.
LEGACY_SCHEDULED_TRAFFIC_REFERENCES = (
    "clash-sub-traffic",
    "traffic-update",
    "traffic_update",
)

DOCUMENTED_CLI_COMMANDS = frozenset(
    (
        "sync",
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
        # Internal systemd entry: surfaced only to clash-sub-metadata.service,
        # never in the interactive menus or user documentation.
        "metadata-serve",
    )
)


def _runtime_source_files():
    files = []
    for relative in ACTIVE_RUNTIME_PATHS:
        path = ROOT / relative
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                child for child in sorted(path.rglob("*")) if child.is_file()
            )
    return files


PROJECT_USE_DOCUMENTS = (
    "README.md",
    "DEPLOYMENT.md",
    "docs/template-design.md",
    "docs/operations.md",
)

# Removed business names may survive only in the development-history
# documents under docs/superpowers/, never in active runtime code,
# templates, tests, configuration, deployment assets, or the four
# project-use manuals.  The published airport file AmyTelecom.yaml is a
# current, sanctioned name and is deliberately absent here; the public
# templates stay free of every AmyTelecom reference through the dedicated
# template assertions below.
LEGACY_BUSINESS_REFERENCES = (
    "compat-office",
    "compat-universal",
    "balance-office",
    "Clash-Compat-Office.yaml",
    "Clash-Compat-Universal.yaml",
    "Clash-Balance-Office.yaml",
    "private/home.yaml",
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

    def test_scheduled_traffic_mechanism_is_absent_from_the_active_product_tree(self):
        # Active runtime code, deployment assets, the four project-use
        # manuals, and the runtime tests must carry none of the retired
        # scheduled-traffic names.  Historical design records under
        # docs/superpowers/ are deliberately not scanned.
        paths = [str(ROOT / "clash_sub"), str(ROOT / "deploy")]
        paths.extend(str(ROOT / relative) for relative in PROJECT_USE_DOCUMENTS)
        paths.extend(
            str(path)
            for path in sorted((ROOT / "tests").glob("test_lightweight_*.py"))
        )
        for reference in LEGACY_SCHEDULED_TRAFFIC_REFERENCES:
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
            "private/reference-configs/2026-08-21/private-source.yaml",
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

    def test_home_script_targets_only_new_titles(self):
        script = ROOT / "private" / "clash-verge-home.js"
        if not script.is_file():
            self.skipTest("local-only home script is absent from this checkout")
        source = script.read_text(encoding="utf-8")
        self.assertIn('"Clash-Compat"', source)
        self.assertIn('"Clash-Balance"', source)
        self.assertNotIn("Clash Compat Universal", source)
        self.assertNotIn("Clash Balance Universal", source)

    def test_server_home_yaml_is_not_a_runtime_contract(self):
        tracked = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in _runtime_source_files()
        )
        self.assertNotIn("HomeOverlay", tracked)
        self.assertNotIn("private/home.yaml", tracked)

    def test_private_tree_stays_ignored_and_the_home_script_stays_untracked(self):
        for relative in ("private/clash-verge-home.js", "private/home.yaml"):
            result = subprocess.run(
                ["git", "check-ignore", "-q", relative],
                cwd=ROOT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, relative)
        completed = subprocess.run(
            ["git", "ls-files", "--", "private"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.stdout.split(), [])

    def test_secret_scanner_drops_the_home_overlay_boundary(self):
        # Home data lives only in the local machine's Clash Verge script;
        # the scanner must not expect or parse a server home overlay.
        source = (ROOT / "scripts" / "scan_tracked_secrets.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("home.yaml", source)
        self.assertNotIn("HomeOverlay", source)
        self.assertNotIn("work" + "bench", source)

    def test_reference_sources_are_not_tracked(self):
        for name in ("private-source.yaml",):
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

    def test_shipped_template_tree_replaces_superseded_templates_safely(self):
        for relative in TRACKED_DOCUMENT_PATHS:
            self.assertTrue((ROOT / relative).is_file(), relative)
        for relative in SUPERSEDED_TEMPLATE_PATHS:
            self.assertFalse((ROOT / relative).exists(), relative)

        compat = yaml.safe_load(
            (ROOT / "templates/base/Clash-Compat.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(compat.get("proxies"), [])
        self.assertNotIn("proxy-providers", compat)

        tracked_text = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in TRACKED_DOCUMENT_PATHS
        )
        self.assertNotIn("AmyTelecom", tracked_text)

    def test_project_use_documentation_is_exactly_four_chinese_files(self):
        actual = {
            path.relative_to(ROOT).as_posix()
            for path in (*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md"))
            if not path.relative_to(ROOT).as_posix().startswith("docs/superpowers/")
        }
        self.assertEqual(sorted(actual), sorted(PROJECT_USE_DOCUMENTS))
        for relative in PROJECT_USE_DOCUMENTS:
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertTrue(text.strip(), relative)

    def test_active_sources_contain_no_legacy_business_references(self):
        search_roots = ("clash_sub", "templates", "config", "deploy", "scripts", "tests")
        for root_name in search_roots:
            for path in sorted((ROOT / root_name).rglob("*")):
                if (
                    not path.is_file()
                    or path == Path(__file__).resolve()
                    or "__pycache__" in path.parts
                    or path.suffix in (".pyc", ".pyo")
                ):
                    # This file defines the reference list it enforces, and
                    # compiled bytecode caches merely mirror source text.
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for reference in LEGACY_BUSINESS_REFERENCES:
                    self.assertNotIn(
                        reference,
                        text,
                        "%s references %r" % (path.relative_to(ROOT).as_posix(), reference),
                    )
        for relative in PROJECT_USE_DOCUMENTS:
            text = (ROOT / relative).read_text(encoding="utf-8")
            for reference in LEGACY_BUSINESS_REFERENCES:
                self.assertNotIn(reference, text, relative)

    def test_retired_user_documentation_is_absent(self):
        retired = (
            "docs/3x-ui-setup.md",
            "docs/dns-design.md",
            "docs/legacy-trojan-topology.md",
            "docs/private-data.md",
            "docs/recovery.md",
            "docs/superpowers/plans/2026-08-21-clash-subscription-publication.md",
            "docs/superpowers/plans/2026-08-23-clash-sub-lightweight.md",
            "docs/superpowers/plans/2026-08-25-clash-sub-integration.md",
            "docs/superpowers/plans/2026-08-28-private-home-overlay-upload.md",
            "docs/superpowers/specs/2026-08-21-clash-subscription-publication-design.md",
            "docs/superpowers/specs/2026-08-23-clash-sub-lightweight-redesign.md",
            "docs/superpowers/specs/2026-08-25-clash-sub-integration-design.md",
            "docs/superpowers/specs/2026-08-27-local-template-work" + "bench-design.md",
            "docs/superpowers/specs/2026-08-28-private-home-overlay-upload-design.md",
            "docs/superpowers/specs/2026-08-28-stable-amytelecom-provider-design.md",
            "docs/superpowers/plans/2026-08-29-clash-template-redesign.md",
            "docs/superpowers/specs/2026-08-29-clash-template-redesign.md",
        )
        for relative in retired:
            self.assertFalse((ROOT / relative).exists(), relative)
