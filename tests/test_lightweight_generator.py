import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from ruamel.yaml.comments import CommentedMap

from clash_sub.domain import AirportProvider
from clash_sub.generator import _compose_variant, render_user_bundle
from clash_sub.yaml_rt import load_round_trip


PROVIDER_URL = "https://sub.example.test:443/s/owner-token/AmyTelecom.yaml"

BASE_COMPAT = """# compat shared comment
dns:
  enable: true
  nameserver:
  - https://compat-only-dns.example/dns-query
proxies: []
proxy-groups:
- name: Public
  type: select
  proxies: [DIRECT]
rule-providers: {}
rules:
- MATCH,Public
"""

BALANCE_DNS = """# balance dns document comment
dns:
  enable: true  # balance dns comment
  nameserver:
  - https://balance-only-dns.example/dns-query
"""

PROFILES = """profiles:
  compat:
    dns: compat
  balance:
    dns: balance
inject-node-groups:
- Public
inject-provider-groups: []
"""

AIRPORT_GROUPS_COMPAT = """# compat shared comment
dns:
  enable: true
proxies: []
proxy-groups:
- name: Region Auto
  type: url-test
  proxies: []
- name: Main Select
  type: select
  proxies: [Region Auto]
- name: Feature
  type: select
  proxies: [Region Auto]
- name: Public
  type: select
  proxies: [DIRECT]
rule-providers: {}
rules:
- MATCH,Main Select
"""

AIRPORT_GROUPS_PROFILES = """profiles:
  compat:
    dns: compat
  balance:
    dns: balance
inject-node-groups:
- Main Select
inject-provider-groups:
- Region Auto
"""


def provider():
    return AirportProvider(PROVIDER_URL)


def reality_proxy(name):
    return {
        "name": name,
        "type": "vless",
        "server": "203.0.113.10",
        "port": 443,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "network": "tcp",
        "tls": True,
        "flow": "xtls-rprx-vision",
        "servername": "www.example.com",
        "client-fingerprint": "chrome",
        "reality-opts": {
            "public-key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "short-id": "0123456789abcdef",
        },
    }


class LightweightGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.templates = self.root / "templates"
        self._write("templates/base/Clash-Compat.yaml", BASE_COMPAT)
        self._write("templates/dns/Clash-Balance.yaml", BALANCE_DNS)
        self._write("templates/profiles.yaml", PROFILES)

    def _write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_fixed_owner_and_member_profile_sets(self):
        owner = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
        member = render_user_bundle(False, [reality_proxy("Member")], None, self.templates)
        self.assertEqual(tuple(owner), ("compat", "balance", "airport"))
        self.assertEqual(tuple(member), ("compat",))
        airport = yaml.safe_load(owner["airport"])
        self.assertEqual(airport["proxies"], [])
        self.assertEqual(tuple(airport["proxy-providers"]), ("AmyTelecom",))

    def test_airport_profile_uses_its_separate_provider_capability(self):
        separate = "https://sub.example.test/s/airport-only/AmyTelecom.yaml"
        rendered = render_user_bundle(
            True, [reality_proxy("Owner")], AirportProvider(PROVIDER_URL, separate), self.templates
        )
        self.assertEqual(yaml.safe_load(rendered["airport"])["proxy-providers"]["AmyTelecom"]["url"], separate)
        self.assertEqual(yaml.safe_load(rendered["compat"])["proxy-providers"]["AmyTelecom"]["url"], PROVIDER_URL)

    def test_balance_replaces_only_dns_and_preserves_comments(self):
        rendered = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
        self.assertIn("# compat shared comment", rendered["balance"])
        self.assertIn("# balance dns comment", rendered["balance"])
        self.assertNotIn("compat-only-dns", rendered["balance"])

    def test_member_has_no_airport_provider(self):
        rendered = yaml.safe_load(
            render_user_bundle(False, [reality_proxy("Member")], None, self.templates)["compat"]
        )
        self.assertNotIn("proxy-providers", rendered)

    def test_member_rejects_airport_provider(self):
        with self.assertRaisesRegex(ValueError, "member profiles"):
            render_user_bundle(False, [reality_proxy("Member")], provider(), self.templates)

    def test_manifest_is_strict_and_compose_keeps_round_trip_document(self):
        document, injections, positions = _compose_variant(self.templates, "compat")
        self.assertIsInstance(document, CommentedMap)
        self.assertEqual(injections, {"Public": "all"})
        self.assertEqual(positions, {})
        self._write("templates/profiles.yaml", PROFILES + "legacy: true\n")
        with self.assertRaises(ValueError):
            _compose_variant(self.templates, "compat")

    def test_member_render_drops_provider_only_groups_and_cascades(self):
        # Region Auto is a sanitized provider-only group (empty proxies,
        # no use); members must not ship it, and references to it must
        # collapse. Main Select keeps the injected member node instead.
        self._write("templates/base/Clash-Compat.yaml", AIRPORT_GROUPS_COMPAT)
        self._write("templates/profiles.yaml", AIRPORT_GROUPS_PROFILES)
        self._write("templates/dns/Clash-Balance.yaml", BALANCE_DNS)

        member = yaml.safe_load(
            render_user_bundle(False, [reality_proxy("Member")], None, self.templates)["compat"]
        )

        names = [group["name"] for group in member["proxy-groups"]]
        self.assertNotIn("Region Auto", names)
        self.assertNotIn("Feature", names)
        self.assertIn("Main Select", names)
        self.assertIn("Public", names)
        for group in member["proxy-groups"]:
            self.assertTrue(
                group.get("proxies") or group.get("use") or group.get("include-all") is True,
                group["name"],
            )
        main = next(g for g in member["proxy-groups"] if g["name"] == "Main Select")
        self.assertEqual(main["proxies"], ["Member"])
        rules = member["rules"]
        self.assertTrue(all(rule.split(",")[-1] in {g["name"] for g in member["proxy-groups"]} for rule in rules))

    def test_owner_render_keeps_provider_groups_with_use(self):
        self._write("templates/base/Clash-Compat.yaml", AIRPORT_GROUPS_COMPAT)
        self._write("templates/profiles.yaml", AIRPORT_GROUPS_PROFILES)
        self._write("templates/dns/Clash-Balance.yaml", BALANCE_DNS)

        owner = yaml.safe_load(
            render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)["compat"]
        )

        names = [group["name"] for group in owner["proxy-groups"]]
        self.assertIn("Region Auto", names)
        region = next(g for g in owner["proxy-groups"] if g["name"] == "Region Auto")
        self.assertEqual(region.get("use"), ["AmyTelecom"])
        main = next(g for g in owner["proxy-groups"] if g["name"] == "Main Select")
        self.assertIn("Owner", main["proxies"])
        self.assertIn("Region Auto", main["proxies"])

    def test_provider_injection_is_owner_only(self):
        self._write("templates/base/Clash-Compat.yaml", BASE_COMPAT.replace("rule-providers: {}\n", "- name: Automatic\n  type: url-test\n  proxies: [DIRECT]\nrule-providers: {}\n"))
        self._write("templates/profiles.yaml", PROFILES.replace("inject-provider-groups: []", "inject-provider-groups: [Automatic]"))
        owner = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
        member = render_user_bundle(False, [reality_proxy("Member")], None, self.templates)
        self.assertEqual(yaml.safe_load(owner["compat"])["proxy-groups"][1]["use"], ["AmyTelecom"])
        self.assertNotIn("use", yaml.safe_load(member["compat"])["proxy-groups"][1])

    def test_render_keeps_shared_yaml_aliases(self):
        self._write("templates/base/Clash-Compat.yaml", BASE_COMPAT.replace("rule-providers: {}\n", "routing-default: &routing-default {interval: 300}\nrouting-default-copy: *routing-default\nrule-providers: {}\n"))
        rendered = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)["compat"]
        document = load_round_trip(rendered.encode("utf-8"))
        self.assertIs(document["routing-default"], document["routing-default-copy"])

    def test_positioned_nodes_preserve_order_for_owner_and_member(self):
        self._write("templates/base/Clash-Compat.yaml", BASE_COMPAT.replace(
            "proxies: [DIRECT]", "proxies: [DIRECT, REJECT]"))
        self._write("templates/profiles.yaml", PROFILES + "inject-node-positions: {Public: 1}\n")
        nodes = [reality_proxy("First"), reality_proxy("Second")]
        for owner in (True, False):
            for variant, text in render_user_bundle(owner, nodes, provider() if owner else None, self.templates).items():
                group = yaml.safe_load(text)["proxy-groups"][0]
                expected = ["DIRECT", "REJECT"] if variant == "airport" else ["DIRECT", "First", "Second", "REJECT"]
                self.assertEqual(group["proxies"], expected)

    def test_legacy_manifest_still_appends_nodes(self):
        text = render_user_bundle(False, [reality_proxy("Node")], None, self.templates)["compat"]
        self.assertEqual(yaml.safe_load(text)["proxy-groups"][0]["proxies"], ["DIRECT", "Node"])

    def test_positions_support_start_and_end(self):
        for position, expected in ((0, ["Node", "DIRECT"]), (1, ["DIRECT", "Node"])):
            self._write("templates/profiles.yaml", PROFILES + f"inject-node-positions: {{Public: {position}}}\n")
            text = render_user_bundle(False, [reality_proxy("Node")], None, self.templates)["compat"]
            self.assertEqual(yaml.safe_load(text)["proxy-groups"][0]["proxies"], expected)

    def test_invalid_node_positions_are_rejected(self):
        for positions in ("[]", "null", "{Missing: 0}", "{Public: true}", "{Public: -1}",
                          "{Public: 0.5}", "{Public: '0'}", "{Public: 2}"):
            with self.subTest(positions=positions):
                self._write("templates/profiles.yaml", PROFILES + f"inject-node-positions: {positions}\n")
                with self.assertRaisesRegex(ValueError, "position"):
                    render_user_bundle(False, [reality_proxy("Node")], None, self.templates)

    def test_positioned_insertion_does_not_mutate_another_aliased_group(self):
        self._write("templates/base/Clash-Compat.yaml", BASE_COMPAT.replace(
            "proxies: [DIRECT]", "proxies: &shared-options [DIRECT]").replace(
            "rule-providers: {}", "- name: Other\n  type: select\n  proxies: *shared-options\nrule-providers: {}"))
        self._write("templates/profiles.yaml", PROFILES + "inject-node-positions: {Public: 0}\n")
        text = render_user_bundle(False, [reality_proxy("Node")], None, self.templates)["compat"]
        groups = yaml.safe_load(text)["proxy-groups"]
        self.assertEqual(groups[0]["proxies"], ["Node", "DIRECT"])
        self.assertEqual(groups[1]["proxies"], ["DIRECT"])

    def test_group_can_receive_positioned_nodes_and_owner_provider(self):
        self._write("templates/profiles.yaml", PROFILES.replace(
            "inject-provider-groups: []", "inject-provider-groups: [Public]")
            + "inject-node-positions: {Public: 0}\n")
        for owner in (True, False):
            text = render_user_bundle(owner, [reality_proxy("Node")], provider() if owner else None, self.templates)["compat"]
            group = yaml.safe_load(text)["proxy-groups"][0]
            self.assertEqual(group["proxies"], ["Node", "DIRECT"])
            self.assertEqual(group.get("use", []), ["AmyTelecom"] if owner else [])

    def test_identical_inputs_are_byte_stable(self):
        first = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
        second = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
