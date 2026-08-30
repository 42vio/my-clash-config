import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from ruamel.yaml.comments import CommentedMap

from clash_sub.domain import AirportProvider
from clash_sub.generator import _compose_variant, render_user_bundle
from clash_sub.sources import HomeSourceError, parse_home_overlay
from clash_sub.yaml_rt import load_round_trip


PROVIDER_URL = "https://sub.example.test:443/s/owner-token/AmyTelecom.yaml"
HOME_CIDR_RULE = "IP-CIDR,192.168.2.0/24,HomeOnly,no-resolve"


BASE_COMPAT = """# compat shared comment
mixed-port: 7890
allow-lan: true
mode: rule
dns:
  enable: true
  nameserver:  # compat dns comment
  - https://compat-only-dns.example/dns-query  # compat nameserver item
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
  - https://balance-only-dns.example/dns-query  # balance nameserver item
"""

PROFILES = """profiles:
  compat-office:
    dns: compat
    home: true
  compat-universal:
    dns: compat
    home: false
  balance-office:
    dns: balance-office
    home: true
inject-node-groups:
- Public
inject-provider-groups: []
"""

HOME_YAML = """# home header
proxies: # home proxies comment
- name: Home  # home proxy comment
  type: vless
  server: 192.0.2.20
  port: 443
  uuid: 22222222-2222-4222-8222-222222222222
  network: tcp
  tls: true
  flow: xtls-rprx-vision
  servername: home.example.test
  client-fingerprint: chrome
  reality-opts:
    public-key: BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
    short-id: fedcba9876543210
proxy-groups: # home groups comment
- name: HomeServer  # home group comment
  type: select
  proxies: [DIRECT]
- name: HomeOnly
  type: select
  proxies: [DIRECT]
extend-proxy-groups:
  Public: [HomeServer]
inject-node-groups: [HomeServer]
inject-home-node-groups: [HomeOnly]
rules: # home rules comment
- IP-CIDR,192.168.2.0/24,HomeOnly,no-resolve
"""


def provider(digest="1" * 64):
    return AirportProvider(PROVIDER_URL, digest)


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


def home_overlay():
    return parse_home_overlay(HOME_YAML.encode("utf-8"), 1024 * 1024)


def proxy_names(text):
    return [proxy["name"] for proxy in yaml.safe_load(text)["proxies"]]


def proxy_groups(text):
    return {group["name"]: group for group in yaml.safe_load(text)["proxy-groups"]}


def provider_mapping(text):
    return yaml.safe_load(text).get("proxy-providers", {})


class LightweightGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self._write("templates/base/compat-office.yaml", BASE_COMPAT)
        self._write("templates/dns/balance-office.yaml", BALANCE_DNS)
        self._write("templates/profiles.yaml", PROFILES)

    def _write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_fixed_owner_and_member_profile_sets(self):
        owner = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            home_overlay(),
            self.root / "templates",
        )
        member = render_user_bundle(
            False,
            [reality_proxy("Member 3x-ui")],
            None,
            None,
            self.root / "templates",
        )

        self.assertEqual(
            tuple(owner), ("compat-office", "compat-universal", "balance-office")
        )
        self.assertEqual(tuple(member), ("compat-universal",))

    def test_authorization_matrix_has_exact_inline_sources_and_home_scope(self):
        owner = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            home_overlay(),
            self.root / "templates",
        )
        member = render_user_bundle(
            False,
            [reality_proxy("Member 3x-ui")],
            None,
            None,
            self.root / "templates",
        )

        self.assertEqual(
            proxy_names(owner["compat-office"]), ["Owner 3x-ui", "Home"]
        )
        self.assertEqual(proxy_names(owner["compat-universal"]), ["Owner 3x-ui"])
        self.assertEqual(
            proxy_names(owner["balance-office"]), ["Owner 3x-ui", "Home"]
        )
        self.assertEqual(proxy_names(member["compat-universal"]), ["Member 3x-ui"])

        for variant in ("compat-office", "balance-office"):
            document = yaml.safe_load(owner[variant])
            groups = proxy_groups(owner[variant])
            self.assertIn("HomeServer", groups)
            self.assertIn("HomeOnly", groups)
            self.assertIn(HOME_CIDR_RULE, document["rules"])
            self.assertEqual(tuple(provider_mapping(owner[variant])), ("AmyTelecom",))
        universal = yaml.safe_load(owner["compat-universal"])
        self.assertNotIn("Home", proxy_names(owner["compat-universal"]))
        self.assertNotIn(
            "HomeServer", {group["name"] for group in universal["proxy-groups"]}
        )
        self.assertNotIn(HOME_CIDR_RULE, universal["rules"])
        self.assertEqual(
            tuple(provider_mapping(owner["compat-universal"])), ("AmyTelecom",)
        )

        member_document = yaml.safe_load(member["compat-universal"])
        self.assertEqual(provider_mapping(member["compat-universal"]), {})
        self.assertNotIn("Home", proxy_names(member["compat-universal"]))
        self.assertNotIn(
            "HomeServer", {group["name"] for group in member_document["proxy-groups"]}
        )
        self.assertNotIn(HOME_CIDR_RULE, member_document["rules"])

    def test_balance_replaces_complete_dns_and_comments_remain_profile_specific(self):
        owner = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            home_overlay(),
            self.root / "templates",
        )

        self.assertIn("# compat shared comment", owner["compat-office"])
        self.assertIn("# compat shared comment", owner["compat-universal"])
        self.assertIn("# balance dns comment", owner["balance-office"])
        self.assertNotIn("compat-only-dns.example", owner["balance-office"])
        self.assertEqual(
            yaml.safe_load(owner["balance-office"])["dns"],
            yaml.safe_load(
                (self.root / "templates/dns/balance-office.yaml").read_text(
                    encoding="utf-8"
                )
            )["dns"],
        )

    def test_home_object_comments_and_key_comments_only_reach_office_outputs(self):
        owner = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            home_overlay(),
            self.root / "templates",
        )

        for variant in ("compat-office", "balance-office"):
            self.assertIn("# home group comment", owner[variant])
            self.assertIn("# home groups comment", owner[variant])
            self.assertIn("# home rules comment", owner[variant])
            self.assertIn("# home proxy comment", owner[variant])
        self.assertNotIn("# home group comment", owner["compat-universal"])
        self.assertNotIn("# home groups comment", owner["compat-universal"])
        self.assertNotIn("# home rules comment", owner["compat-universal"])
        self.assertNotIn("# home proxy comment", owner["compat-universal"])

    def test_compose_variant_returns_round_trip_document_and_manifest_injections(self):
        document, injections = _compose_variant(
            self.root / "templates", "compat-universal"
        )

        self.assertIsInstance(document, CommentedMap)
        self.assertEqual(injections, {"Public": "all"})
        self.assertEqual(document["proxies"], [])

    def test_manifest_is_strict_and_cannot_add_extra_keys(self):
        self._write(
            "templates/profiles.yaml",
            PROFILES.replace(
                "inject-node-groups:\n- Public\n",
                "inject-node-groups:\n- Public\nlegacy: true\n",
            ),
        )
        with self.assertRaises(ValueError):
            _compose_variant(self.root / "templates", "compat-universal")

    def test_owner_provider_only_group_is_composed_without_member_provider_access(self):
        """A provider-only target must not depend on node injection membership."""
        self._write(
            "templates/base/compat-office.yaml",
            BASE_COMPAT.replace(
                "rule-providers: {}\n",
                "- name: Automatic\n"
                "  type: url-test\n"
                "  proxies: [DIRECT]\n"
                "rule-providers: {}\n",
            ),
        )
        self._write(
            "templates/profiles.yaml",
            PROFILES.replace(
                "inject-provider-groups: []",
                "inject-provider-groups: [Automatic]",
            ),
        )

        owner = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            home_overlay(),
            self.root / "templates",
        )
        member = render_user_bundle(
            False,
            [reality_proxy("Member 3x-ui")],
            None,
            None,
            self.root / "templates",
        )

        owner_groups = proxy_groups(owner["compat-universal"])
        member_groups = proxy_groups(member["compat-universal"])
        self.assertEqual(owner_groups["Automatic"]["use"], ["AmyTelecom"])
        self.assertNotIn("use", member_groups["Automatic"])
        self.assertNotIn("proxy-providers", yaml.safe_load(member["compat-universal"]))

    def test_render_keeps_shared_yaml_aliases_as_shared_objects(self):
        """Rebuilding a group separately must not expand a shared YAML alias."""
        self._write(
            "templates/base/compat-office.yaml",
            BASE_COMPAT.replace(
                "rule-providers: {}\n",
                "routing-default: &routing-default {interval: 300}\n"
                "routing-default-copy: *routing-default\n"
                "rule-providers: {}\n",
            ),
        )

        rendered = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            home_overlay(),
            self.root / "templates",
        )["compat-universal"]
        document = load_round_trip(rendered.encode("utf-8"))

        self.assertIs(document["routing-default"], document["routing-default-copy"])

    def test_member_rejects_unauthorized_provider_and_home(self):
        with self.assertRaises(ValueError):
            render_user_bundle(
                False,
                [reality_proxy("Member 3x-ui")],
                provider(),
                None,
                self.root / "templates",
            )
        with self.assertRaises(ValueError):
            render_user_bundle(
                False,
                [reality_proxy("Member 3x-ui")],
                None,
                home_overlay(),
                self.root / "templates",
            )

    def test_owner_requires_home_overlay_for_office_profiles(self):
        with self.assertRaises(HomeSourceError) as caught:
            render_user_bundle(
                True,
                [reality_proxy("Owner 3x-ui")],
                provider(),
                None,
                self.root / "templates",
            )

        self.assertEqual(caught.exception.code, "home_source_invalid")
        self.assertEqual(
            str(caught.exception),
            "home overlay rejected: home_source_invalid",
        )

    def test_identical_inputs_are_byte_stable(self):
        first = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            home_overlay(),
            self.root / "templates",
        )
        second = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            home_overlay(),
            self.root / "templates",
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
