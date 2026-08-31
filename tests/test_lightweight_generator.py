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
        self._write("templates/base/compat-office.yaml", BASE_COMPAT)
        self._write("templates/dns/balance-office.yaml", BALANCE_DNS)
        self._write("templates/profiles.yaml", PROFILES)

    def _write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_fixed_owner_and_member_profile_sets(self):
        owner = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
        member = render_user_bundle(False, [reality_proxy("Member")], None, self.templates)
        self.assertEqual(tuple(owner), ("compat", "balance"))
        self.assertEqual(tuple(member), ("compat",))

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
        document, injections = _compose_variant(self.templates, "compat")
        self.assertIsInstance(document, CommentedMap)
        self.assertEqual(injections, {"Public": "all"})
        self._write("templates/profiles.yaml", PROFILES + "legacy: true\n")
        with self.assertRaises(ValueError):
            _compose_variant(self.templates, "compat")

    def test_provider_injection_is_owner_only(self):
        self._write("templates/base/compat-office.yaml", BASE_COMPAT.replace("rule-providers: {}\n", "- name: Automatic\n  type: url-test\n  proxies: [DIRECT]\nrule-providers: {}\n"))
        self._write("templates/profiles.yaml", PROFILES.replace("inject-provider-groups: []", "inject-provider-groups: [Automatic]"))
        owner = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
        member = render_user_bundle(False, [reality_proxy("Member")], None, self.templates)
        self.assertEqual(yaml.safe_load(owner["compat"])["proxy-groups"][1]["use"], ["AmyTelecom"])
        self.assertNotIn("use", yaml.safe_load(member["compat"])["proxy-groups"][1])

    def test_render_keeps_shared_yaml_aliases(self):
        self._write("templates/base/compat-office.yaml", BASE_COMPAT.replace("rule-providers: {}\n", "routing-default: &routing-default {interval: 300}\nrouting-default-copy: *routing-default\nrule-providers: {}\n"))
        rendered = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)["compat"]
        document = load_round_trip(rendered.encode("utf-8"))
        self.assertIs(document["routing-default"], document["routing-default-copy"])

    def test_identical_inputs_are_byte_stable(self):
        first = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
        second = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
