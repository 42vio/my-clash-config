import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from clash_sub import template_sync
from clash_sub.template_sync import OUTPUT_MODES, PUBLIC_TEMPLATE_FILES, TemplateSyncError, TemplateSyncReport, default_source_paths, run_template_sync
from clash_sub.yaml_rt import load_round_trip

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

_OLD_AIRPORT_CACHE = "AmyTelecom" + ".yaml"
AIRPORT_ALIAS = """# compat comment
mixed-port: 7890
allow-lan: true
mode: rule
dns:
  enable: true
anchors:
  auto: {type: url-test, use: &id001 [Subscribe]}
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
  # Subscribe: {<<: p, url: https://amy.example.invalid/?L1N1YnNjcmlwdGlvbi9DbGFzaD90PWFueXRsc19jbGFzaCZzaWQ9, path: ./@OLD@}
  Subscribe: {type: file, path: ./@OLD@}
  Other: {type: file, path: ./providers/other.yaml}
proxy-groups:
- name: Airport Only
  <<: {type: url-test, use: [Subscribe]}
  use: *id001
- name: Select
  type: select
  proxies: [DIRECT, Dynamic]
  use: [AmyTelecom, Subscribe]
rule-providers: {}
rules: [MATCH,Select]
""".replace("@OLD@", _OLD_AIRPORT_CACHE)

MERGE_BASE_NODES = """# compat comment
mixed-port: 7890
mode: rule
dns:
  enable: true
anchors:
  a3: {type: select, proxies: [Private Node, DIRECT]}
proxies:
- name: Private Node
  type: ss
  server: 192.0.2.9
  port: 8388
  cipher: aes-128-gcm
  password: placeholder-password
proxy-providers: {}
proxy-groups:
- name: Select
  <<: {type: select, proxies: [Private Node, DIRECT]}
  proxies: [DIRECT]
rule-providers: {}
rules: [MATCH,Select]
"""

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
        current_compat, injections = template_sync._sanitize_compat(load_round_trip(COMPAT))
        write(self.root / "templates/base/Clash-Compat.yaml", template_sync._dump(current_compat))
        write(self.root / "templates/dns/Clash-Balance.yaml", "dns: {}\n")
        write(self.root / "templates/profiles.yaml", template_sync._dump(template_sync._profiles(injections)))
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

    def test_compat_keeps_non_amy_provider_mapping_and_group_reference(self):
        source = load_round_trip(COMPAT.replace(
            "proxy-providers:\n",
            "proxy-providers:\n  Other: {type: file, path: ./providers/other.yaml}\n",
        ))
        sanitized, _injections = template_sync._sanitize_compat(source)
        self.assertIn("Other", sanitized["proxy-providers"])
        self.assertNotIn("AmyTelecom", sanitized["proxy-providers"])
        self.assertNotIn("AmyTelecom", sanitized["proxy-groups"][0].get("use", []))

    def test_compat_keeps_non_amy_http_provider(self):
        source = load_round_trip(COMPAT.replace(
            "proxy-providers:\n",
            "proxy-providers:\n  Other: {type: http, url: http://provider.example/other.yaml, interval: 3600, path: ./providers/other.yaml}\n",
        ))
        sanitized, _injections = template_sync._sanitize_compat(source)
        self.assertEqual(sanitized["proxy-providers"]["Other"]["url"], "http://provider.example/other.yaml")

    def test_compat_sync_publishes_template_with_non_amy_http_provider(self):
        source = self.root / "source/Clash-Compat-Other.yaml"
        write(source, COMPAT.replace(
            "proxy-providers:\n",
            "proxy-providers:\n  Other: {type: http, url: http://provider.example/other.yaml, interval: 3600, path: ./providers/other.yaml}\n",
        ), 0o600)
        run_template_sync(self.root, compat=source)
        self.assertIn("http://provider.example/other.yaml", (self.root / "templates/base/Clash-Compat.yaml").read_text())

    def test_compat_removes_local_airport_alias_providers_and_comments(self):
        sanitized, injections = template_sync._sanitize_compat(load_round_trip(AIRPORT_ALIAS))

        text = template_sync._dump(sanitized)
        self.assertNotIn(_OLD_AIRPORT_CACHE, text)
        self.assertIn("Other", sanitized["proxy-providers"])
        self.assertNotIn("Subscribe", sanitized["proxy-providers"])
        self.assertEqual(injections[1], ("Airport Only", "Select"))
        airport_group = next(group for group in sanitized["proxy-groups"] if group["name"] == "Airport Only")
        self.assertEqual(airport_group.get("use", []), [])
        self.assertEqual(airport_group.get("proxies", []), [])

    def test_compat_sync_publishes_template_without_airport_aliases(self):
        source = self.root / "source/Clash-Compat-Alias.yaml"
        write(source, AIRPORT_ALIAS, 0o600)

        run_template_sync(self.root, compat=source)

        text = (self.root / "templates/base/Clash-Compat.yaml").read_text()
        manifest = (self.root / "templates/profiles.yaml").read_text()
        self.assertNotIn(_OLD_AIRPORT_CACHE, text)
        self.assertIn("Airport Only", manifest)
        self.assertIn("Select", manifest)

    def test_compat_strips_private_nodes_from_merge_bases(self):
        sanitized, _injections = template_sync._sanitize_compat(load_round_trip(MERGE_BASE_NODES))

        text = template_sync._dump(sanitized)
        self.assertNotIn("Private Node", text)
        select = sanitized["proxy-groups"][0]
        self.assertEqual(select["proxies"], ["DIRECT"])
        anchor = sanitized["anchors"]["a3"]
        self.assertEqual(anchor["proxies"], ["DIRECT"])

    def test_sync_validates_the_member_render_before_publishing(self):
        source = self.root / "source/Clash-Compat-Alias.yaml"
        write(source, AIRPORT_ALIAS, 0o600)

        run_template_sync(self.root, compat=source)

        published = (self.root / "templates/base/Clash-Compat.yaml").read_text()
        manifest = (self.root / "templates/profiles.yaml").read_text()
        self.assertIn("Airport Only", published + manifest)

    def test_sync_rejects_a_synthetic_renderer_output_before_publishing(self):
        with patch("clash_sub.template_sync.render_user_bundle", return_value={"compat": "not yaml"}):
            with self.assertRaisesRegex(TemplateSyncError, "template_candidate_invalid"):
                run_template_sync(self.root, compat=self.compat_source)

    def test_second_replacement_failure_restores_selected_outputs_and_modes(self):
        base = self.root / "templates/base/Clash-Compat.yaml"
        write(base, base.read_text().replace("# compat comment", "# prior comment"))
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
