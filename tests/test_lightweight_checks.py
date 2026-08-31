import copy
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from clash_sub.checks import CheckError, MihomoValidator, validate_clash
from clash_sub.domain import MEMBER_VARIANTS, OWNER_VARIANTS


PROVIDER_URL = "https://sub.example.test:443/s/owner-token/AmyTelecom.yaml"


def valid_document():
    return {
        "dns": {"enable": True},
        "proxies": [{
            "name": "Owner 3x-ui", "type": "vless", "server": "203.0.113.10",
            "port": 443, "uuid": "11111111-1111-4111-8111-111111111111",
            "network": "tcp", "tls": True, "flow": "xtls-rprx-vision",
            "servername": "www.example.com", "client-fingerprint": "chrome",
            "reality-opts": {"public-key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "short-id": "0123456789abcdef"},
        }, {"name": "Airport", "type": "trojan", "server": "airport.example.com", "port": 443, "password": "synthetic-password"}],
        "proxy-groups": [{"name": "Selector", "type": "select", "proxies": ["DIRECT", "Owner 3x-ui"]}],
        "rule-providers": {"Direct": {"type": "http", "behavior": "classical"}},
        "rules": ["RULE-SET,Direct,DIRECT", "MATCH,Selector"],
    }


def dump(document):
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def owner_document():
    document = valid_document()
    document["proxy-providers"] = {
        "AmyTelecom": {
            "type": "http", "url": PROVIDER_URL,
            "path": "./proxy_providers/AmyTelecom-Provider.yaml", "interval": 604800,
        }
    }
    return document


class ProviderMappingTests(unittest.TestCase):
    def test_owner_provider_requires_stable_filename_and_weekly_interval(self):
        validate_clash(dump(owner_document()), (), PROVIDER_URL)

    def test_old_digest_path_is_rejected(self):
        document = owner_document()
        document["proxy-providers"]["AmyTelecom"]["path"] = "./proxy_providers/AmyTelecom-" + "1" * 64 + ".yaml"
        with self.assertRaisesRegex(CheckError, "provider"):
            validate_clash(dump(document), (), PROVIDER_URL)

    def test_zero_interval_is_rejected(self):
        document = owner_document()
        document["proxy-providers"]["AmyTelecom"]["interval"] = 0
        with self.assertRaisesRegex(CheckError, "provider"):
            validate_clash(dump(document), (), PROVIDER_URL)

    def test_extra_provider_is_rejected(self):
        document = owner_document()
        document["proxy-providers"]["Extra"] = {"type": "http", "url": PROVIDER_URL}
        with self.assertRaisesRegex(CheckError, "proxy-providers"):
            validate_clash(dump(document), (), PROVIDER_URL)

    def test_member_document_containing_amytelecom_is_rejected(self):
        with self.assertRaisesRegex(CheckError, "proxy-providers"):
            validate_clash(dump(owner_document()), ())

    def test_owner_authorization_requires_provider_and_isolated_url(self):
        with self.assertRaisesRegex(CheckError, "airport proxy-provider"):
            validate_clash(dump(valid_document()), (), PROVIDER_URL)
        validate_clash(dump(owner_document()), ("owner-token",), PROVIDER_URL)
        document = owner_document()
        document["notes"] = "leaked owner-token inline"
        with self.assertRaisesRegex(CheckError, "forbidden value"):
            validate_clash(dump(document), ("owner-token",), PROVIDER_URL)
        document = owner_document()
        document["dns"]["notes"] = PROVIDER_URL
        with self.assertRaisesRegex(CheckError, "forbidden value"):
            validate_clash(dump(document), ("owner-token",), PROVIDER_URL)

    def test_group_uses_require_declared_provider(self):
        document = owner_document()
        document["proxy-groups"][0]["use"] = ["Unknown"]
        with self.assertRaisesRegex(CheckError, "unknown provider"):
            validate_clash(dump(document), (), PROVIDER_URL)
        document = owner_document()
        document["proxy-groups"][0]["use"] = []
        with self.assertRaisesRegex(CheckError, "use"):
            validate_clash(dump(document), (), PROVIDER_URL)
        document = valid_document()
        document["proxy-groups"][0]["use"] = ["AmyTelecom"]
        with self.assertRaisesRegex(CheckError, "unknown provider"):
            validate_clash(dump(document), ())

    def test_yaml_alias_cycle_fails_closed(self):
        text = "dns: &cycle {enable: true}\nproxies: []\nproxy-groups: []\nrule-providers: {Direct: {type: http}}\nrules: [MATCH,DIRECT]\nnotes: {next: *cycle, self: &inner {peer: *inner}}\n"
        with self.assertRaisesRegex(CheckError, "invalid YAML"):
            validate_clash(text, ())


class LightweightChecksTests(unittest.TestCase):
    def test_fixed_profile_authorization_sets_have_no_legacy_aliases(self):
        self.assertEqual(OWNER_VARIANTS, ("compat", "balance"))
        self.assertEqual(MEMBER_VARIANTS, ("compat",))

    def test_valid_document_is_accepted(self):
        self.assertEqual(validate_clash(dump(valid_document()), ())["proxies"][0]["name"], "Owner 3x-ui")

    def test_duplicate_proxy_name_is_rejected(self):
        document = valid_document()
        document["proxies"].append(copy.deepcopy(document["proxies"][0]))
        with self.assertRaisesRegex(CheckError, "duplicate proxy name"):
            validate_clash(dump(document), ())

    def test_duplicate_group_name_is_rejected(self):
        document = valid_document()
        document["proxy-groups"].append({"name": "Selector", "type": "select", "proxies": ["DIRECT"]})
        with self.assertRaisesRegex(CheckError, "duplicate proxy group name"):
            validate_clash(dump(document), ())

    def test_unknown_group_and_rule_targets_are_rejected(self):
        document = valid_document()
        document["proxy-groups"][0]["proxies"].append("Unknown Group")
        with self.assertRaisesRegex(CheckError, "unknown target"):
            validate_clash(dump(document), ())
        document = valid_document()
        document["rules"].append("DOMAIN,example.com,Unknown Group")
        with self.assertRaisesRegex(CheckError, "unknown target"):
            validate_clash(dump(document), ())

    def test_rule_set_unknown_provider_is_sanitized(self):
        document = valid_document()
        document["rules"] = ["RULE-SET,Missing,DIRECT", "MATCH,Selector"]
        with self.assertRaises(CheckError) as caught:
            validate_clash(dump(document), ())
        self.assertRegex(str(caught.exception), "unknown provider")
        self.assertNotIn("Missing", str(caught.exception))

    def test_known_rule_set_provider_passes(self):
        document = valid_document()
        document["rules"] = ["RULE-SET,Direct,DIRECT,no-resolve", "MATCH,Selector"]
        validate_clash(dump(document), ())

    def test_incomplete_reality_is_rejected(self):
        document = valid_document()
        del document["proxies"][0]["reality-opts"]["short-id"]
        with self.assertRaisesRegex(CheckError, "REALITY"):
            validate_clash(dump(document), ())
        document = valid_document()
        del document["proxies"][0]["reality-opts"]
        with self.assertRaisesRegex(CheckError, "REALITY"):
            validate_clash(dump(document), ())

    def test_forbidden_values_and_loopback_urls_are_sanitized(self):
        document = valid_document()
        forbidden = "http://127.0.0.1:2096/clash/private-sub-id"
        document["notes"] = forbidden
        with self.assertRaisesRegex(CheckError, "forbidden value") as caught:
            validate_clash(dump(document), (forbidden, "public-token"))
        self.assertNotIn(forbidden, str(caught.exception))
        with self.assertRaisesRegex(CheckError, "forbidden value"):
            validate_clash(dump(document), ())

    def test_mihomo_uses_safe_invocation_and_sanitizes_failures(self):
        calls = []
        def runner(arguments, **kwargs):
            calls.append((tuple(arguments), kwargs))
            return subprocess.CompletedProcess(arguments, 0)
        with TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.yaml"
            candidate.write_text("mixed-port: 7890\n", encoding="utf-8")
            MihomoValidator(Path("/opt/mihomo/mihomo"), runner=runner).validate(candidate)
        self.assertEqual(calls[0][0], ("/opt/mihomo/mihomo", "-t", "-f", str(candidate)))
        self.assertIs(calls[0][1]["stdin"], subprocess.DEVNULL)
        self.assertIs(calls[0][1]["stdout"], subprocess.DEVNULL)
        self.assertIs(calls[0][1]["stderr"], subprocess.DEVNULL)
        self.assertEqual(calls[0][1]["timeout"], 30)
        self.assertFalse(calls[0][1]["check"])
        def timeout(arguments, **kwargs):
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"], output="private output")
        with self.assertRaisesRegex(CheckError, "timed out") as caught:
            MihomoValidator(Path("/opt/mihomo/mihomo"), runner=timeout).validate(candidate)
        self.assertNotIn("private output", str(caught.exception))

    def test_reality_fields_and_semantics_are_validated(self):
        invalid_values = (("tls", False), ("port", True), ("port", 0), ("port", 65536), ("port", "443"), ("server", 123), ("uuid", 123), ("network", "ws"), ("flow", "xtls-rprx-origin"), ("servername", 123), ("client-fingerprint", 123))
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                document = valid_document()
                document["proxies"][0][field] = value
                with self.assertRaisesRegex(CheckError, "REALITY"):
                    validate_clash(dump(document), ())
        document = valid_document()
        document["proxies"][1]["tls"] = False
        document["proxies"][1]["port"] = "not-a-reality-port"
        validate_clash(dump(document), ())

    def test_proxy_group_cycle_and_name_collision_are_sanitized(self):
        document = valid_document()
        document["proxy-groups"] = [{"name": "First Secret Group", "type": "select", "proxies": ["Second Secret Group"]}, {"name": "Second Secret Group", "type": "select", "proxies": ["First Secret Group"]}]
        document["rules"] = ["MATCH,First Secret Group"]
        with self.assertRaisesRegex(CheckError, "recursive proxy group reference") as caught:
            validate_clash(dump(document), ())
        self.assertNotIn("First Secret Group", str(caught.exception))
        document = valid_document()
        document["proxy-groups"] = [{"name": "Owner 3x-ui", "type": "select", "proxies": ["DIRECT"]}]
        document["rules"] = ["MATCH,DIRECT"]
        with self.assertRaisesRegex(CheckError, "proxy name conflicts"):
            validate_clash(dump(document), ())


if __name__ == "__main__":
    unittest.main()
