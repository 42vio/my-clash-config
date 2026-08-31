import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from clash_sub import template_sync
from clash_sub.template_sync import OUTPUT_MODES, PUBLIC_TEMPLATE_FILES, TemplateSyncError, TemplateSyncReport, default_source_paths, run_template_sync

COMPAT = """# compat comment
mixed-port: 7890
allow-lan: true
mode: rule
dns:
  enable: true
proxies:
- name: Dynamic
  type: vless
  server: 192.0.2.1
  port: 443
  uuid: 11111111-1111-4111-8111-111111111111
  network: tcp
  tls: true
  flow: xtls-rprx-vision
  servername: test.example
  client-fingerprint: chrome
  reality-opts: {public-key: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA, short-id: 1111111111111111}
proxy-providers:
  AmyTelecom: {type: http, url: https://airport.example.invalid/secret, interval: 0, path: ./private.yaml}
proxy-groups:
- name: Select
  type: select
  proxies: [DIRECT, Dynamic]
  use: [AmyTelecom]
rule-providers: {}
rules: [MATCH,Select]
"""
BALANCE = COMPAT.replace("  enable: true\n", "  enable: true  # balance dns comment\n").replace("rules: [MATCH,Select]", "rules: [MATCH,DIRECT]").replace("rule-providers: {}", "- name: Extra\n  type: select\n  proxies: [DIRECT]\nrule-providers: {}")

def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)

class TemplateSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.compat_source = self.root / "source/Clash-Compat.yaml"; self.balance_source = self.root / "source/Clash-Balance.yaml"
        write(self.compat_source, COMPAT, 0o600); write(self.balance_source, BALANCE, 0o600)
        write(self.root / "templates/base/Clash-Compat.yaml", COMPAT)
        write(self.root / "templates/dns/Clash-Balance.yaml", "dns: {}\n")
        write(self.root / "templates/profiles.yaml", "profiles: {}\n")
        write(self.root / "scripts/scan_tracked_secrets.py", "def find_content_findings(text, path): return ()\n")

    def test_default_sources_use_new_case_sensitive_names(self):
        compat, balance = default_source_paths(Path("/Users/test"))
        self.assertEqual(compat.name, "Clash-Compat.yaml"); self.assertEqual(balance.name, "Clash-Balance.yaml")

    def test_balance_sync_writes_dns_with_comments_and_reports_other_paths(self):
        report = run_template_sync(self.root, balance=self.balance_source)
        self.assertIn("# balance dns comment", (self.root / "templates/dns/Clash-Balance.yaml").read_text())
        self.assertIn("proxy-groups", report.ignored_balance_paths)
        self.assertNotIn("private.example", "\n".join(report.lines))

    def test_single_compat_input_does_not_touch_balance(self):
        target = self.root / "templates/dns/Clash-Balance.yaml"; before = target.read_bytes()
        run_template_sync(self.root, compat=self.compat_source)
        self.assertEqual(target.read_bytes(), before)

    def test_compat_removes_dynamic_proxies_and_provider_and_records_groups(self):
        run_template_sync(self.root, compat=self.compat_source)
        text = (self.root / "templates/base/Clash-Compat.yaml").read_text(); manifest = (self.root / "templates/profiles.yaml").read_text()
        self.assertIn("proxies: []", text); self.assertNotIn("AmyTelecom", text); self.assertIn("Select", manifest)
        self.assertNotIn("airport.example", text + manifest)

    def test_second_replacement_failure_restores_selected_outputs_and_modes(self):
        before = {r: ((self.root / r).read_bytes(), stat.S_IMODE((self.root / r).stat().st_mode)) for r in PUBLIC_TEMPLATE_FILES}
        real_replace = template_sync._os_replace; calls = []
        def fail_second(source, target):
            calls.append(target); real_replace(source, target)
            if len(calls) == 2: raise OSError("injected")
        with patch.object(template_sync, "_os_replace", side_effect=fail_second):
            with self.assertRaisesRegex(TemplateSyncError, "template_write_failed"):
                run_template_sync(self.root, compat=self.compat_source, balance=self.balance_source)
        after = {r: ((self.root / r).read_bytes(), stat.S_IMODE((self.root / r).stat().st_mode)) for r in PUBLIC_TEMPLATE_FILES}
        self.assertEqual(after, before)

    def test_prewrite_failures_leave_selected_outputs_unchanged(self):
        before = {r: ((self.root / r).read_bytes(), stat.S_IMODE((self.root / r).stat().st_mode)) for r in PUBLIC_TEMPLATE_FILES}
        cases = (
            ("serialization", patch.object(template_sync, "_dump", side_effect=TemplateSyncError("template_candidate_invalid"))),
            ("validation", patch.object(template_sync, "_validate_candidates", side_effect=TemplateSyncError("template_candidate_invalid"))),
            ("secret scan", patch.object(template_sync, "_load_scanner", return_value=type("Scanner", (), {"find_content_findings": staticmethod(lambda text, path: ("finding",))})())),
        )
        for name, replacement in cases:
            with self.subTest(name=name), replacement:
                with self.assertRaises(TemplateSyncError):
                    run_template_sync(self.root, compat=self.compat_source, balance=self.balance_source)
                after = {r: ((self.root / r).read_bytes(), stat.S_IMODE((self.root / r).stat().st_mode)) for r in PUBLIC_TEMPLATE_FILES}
                self.assertEqual(after, before)

    def test_public_paths_and_modes_are_fixed(self):
        self.assertEqual(PUBLIC_TEMPLATE_FILES, ("templates/base/Clash-Compat.yaml", "templates/dns/Clash-Balance.yaml", "templates/profiles.yaml"))
        self.assertEqual(OUTPUT_MODES, {r: 0o644 for r in PUBLIC_TEMPLATE_FILES})
        self.assertEqual(TemplateSyncReport((), (), ()).changed, ())
