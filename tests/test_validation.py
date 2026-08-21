import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from clash_sub.models import RealitySettings
from clash_sub.validation import ValidationError, sha256_bytes, sha256_file, validate_config


REALITY = RealitySettings("198.51.100.25", 443, "xtls-rprx-vision")


def dump(document) -> str:
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def valid_document():
    return {
        "dns": {"enable": True},
        "proxies": [
            {
                "name": "Owner XUI",
                "type": "vless",
                "server": REALITY.public_address,
                "port": REALITY.public_port,
                "uuid": "11111111-1111-4111-8111-111111111111",
                "network": "tcp",
                "tls": True,
                "flow": REALITY.required_flow,
                "servername": "www.example.com",
                "client-fingerprint": "chrome",
                "reality-opts": {
                    "public-key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "short-id": "0123456789abcdef",
                },
            },
            {
                "name": "Airport Node",
                "type": "trojan",
                "server": "airport.example.com",
                "port": 443,
                "password": "synthetic-password",
            },
        ],
        "proxy-groups": [
            {"name": "Selector", "type": "select", "proxies": ["DIRECT", "Owner XUI", "Airport Node"]},
            {"name": "Fallback", "type": "select", "proxies": ["Selector", "REJECT-DROP"]},
        ],
        "rule-providers": {
            "Apple": {
                "type": "http",
                "behavior": "classical",
                "url": "https://rules.example/apple.yaml",
                "path": "./rules/apple.yaml",
            }
        },
        "rules": [
            "DOMAIN-SUFFIX,example.com,Selector",
            "MATCH,DIRECT",
        ],
    }


class ValidationTests(unittest.TestCase):
    def test_sha256_bytes_matches_hashlib(self):
        self.assertEqual(
            sha256_bytes(b"hello"),
            hashlib.sha256(b"hello").hexdigest(),
        )

    def test_sha256_file_hashes_file_contents(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.txt"
            path.write_bytes(b"fixture-data")

            self.assertEqual(
                sha256_file(path),
                hashlib.sha256(b"fixture-data").hexdigest(),
            )

    def test_malformed_yaml_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "invalid YAML"):
            validate_config("proxies: [\n", [], REALITY)

    def test_non_mapping_root_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "mapping"):
            validate_config("- just\n- a\n- list\n", [], REALITY)

    def test_missing_required_top_level_section_is_rejected(self):
        document = valid_document()
        del document["dns"]

        with self.assertRaisesRegex(ValidationError, "dns"):
            validate_config(dump(document), [], REALITY)

    def test_proxy_providers_presence_is_rejected(self):
        document = valid_document()
        document["proxy-providers"] = {"Subscribe": {"type": "http"}}

        with self.assertRaisesRegex(ValidationError, "proxy-providers"):
            validate_config(dump(document), [], REALITY)

    def test_empty_proxies_list_is_rejected(self):
        document = valid_document()
        document["proxies"] = []

        with self.assertRaisesRegex(ValidationError, "proxies"):
            validate_config(dump(document), [], REALITY)

    def test_duplicate_proxy_names_are_rejected(self):
        document = valid_document()
        document["proxies"][1]["name"] = "Owner XUI"

        with self.assertRaisesRegex(ValidationError, "Owner XUI"):
            validate_config(dump(document), [], REALITY)

    def test_duplicate_group_names_are_rejected(self):
        document = valid_document()
        document["proxy-groups"][1]["name"] = "Selector"

        with self.assertRaisesRegex(ValidationError, "Selector"):
            validate_config(dump(document), [], REALITY)

    def test_unknown_proxy_group_target_is_rejected(self):
        document = valid_document()
        document["proxy-groups"][0]["proxies"].append("missing-target")

        with self.assertRaisesRegex(ValidationError, "missing-target"):
            validate_config(dump(document), [], REALITY)

    def test_unknown_nested_group_target_is_rejected(self):
        document = valid_document()
        document["proxy-groups"][1]["proxies"] = ["Selector", "Missing Group"]

        with self.assertRaisesRegex(ValidationError, "Missing Group"):
            validate_config(dump(document), [], REALITY)

    def test_unknown_rule_target_is_rejected(self):
        document = valid_document()
        document["rules"].append("DOMAIN,missing.example,Unknown Target")

        with self.assertRaisesRegex(ValidationError, "Unknown Target"):
            validate_config(dump(document), [], REALITY)

    def test_ip_asn_rule_uses_final_target_field_when_no_resolve_precedes_it(self):
        document = valid_document()
        document["rules"].append("IP-ASN,13335,no-resolve,Selector")

        parsed = validate_config(dump(document), [], REALITY)

        self.assertEqual(parsed["rules"][-1], "IP-ASN,13335,no-resolve,Selector")

    def test_trailing_no_resolve_does_not_hide_unknown_rule_target(self):
        document = valid_document()
        document["rules"].append("RULE-SET,Apple,Unknown Target,no-resolve")

        with self.assertRaisesRegex(ValidationError, "Unknown Target"):
            validate_config(dump(document), [], REALITY)

    def test_rule_provider_mapping_must_be_valid(self):
        document = valid_document()
        document["rule-providers"]["Apple"] = []

        with self.assertRaisesRegex(ValidationError, "rule-providers"):
            validate_config(dump(document), [], REALITY)

    def test_leftover_jinja_marker_is_rejected(self):
        document = valid_document()
        document["notes"] = "{{ PRIVATE_VALUE }}"

        with self.assertRaisesRegex(ValidationError, "template marker"):
            validate_config(dump(document), [], REALITY)

    def test_exact_upstream_url_leak_is_rejected_without_echoing_it(self):
        source_url = "http://127.0.0.1:2096/sub/private-value"
        document = valid_document()
        document["notes"] = source_url

        with self.assertRaisesRegex(ValidationError, "upstream source URL") as context:
            validate_config(dump(document), [source_url], REALITY)

        self.assertNotIn(source_url, str(context.exception))

    def test_proxy_value_leak_error_does_not_echo_private_fields(self):
        document = valid_document()
        document["proxies"][0]["server"] = "127.0.0.1"
        document["proxies"][0]["uuid"] = "00000000-0000-4000-8000-000000000999"

        with self.assertRaisesRegex(ValidationError, "public endpoint") as context:
            validate_config(dump(document), [], REALITY)

        message = str(context.exception)
        self.assertNotIn("00000000-0000-4000-8000-000000000999", message)
        self.assertNotIn("127.0.0.1", message)

    def test_self_hosted_reality_node_requires_expected_public_endpoint(self):
        document = valid_document()
        document["proxies"][0]["server"] = "127.0.0.1"

        with self.assertRaisesRegex(ValidationError, "public endpoint"):
            validate_config(dump(document), [], REALITY)

    def test_self_hosted_reality_node_requires_expected_port(self):
        document = valid_document()
        document["proxies"][0]["port"] = 8443

        with self.assertRaisesRegex(ValidationError, "public endpoint"):
            validate_config(dump(document), [], REALITY)

    def test_self_hosted_reality_node_requires_tcp_network(self):
        document = valid_document()
        document["proxies"][0]["network"] = "ws"

        with self.assertRaisesRegex(ValidationError, "REALITY"):
            validate_config(dump(document), [], REALITY)

    def test_vless_node_without_required_flow_is_not_treated_as_reality(self):
        document = valid_document()
        document["proxies"][0]["flow"] = "xtls-rprx-origin"

        parsed = validate_config(dump(document), [], REALITY)

        self.assertEqual(parsed["proxies"][0]["flow"], "xtls-rprx-origin")

    def test_self_hosted_reality_node_requires_tls(self):
        document = valid_document()
        document["proxies"][0]["tls"] = False

        with self.assertRaisesRegex(ValidationError, "REALITY"):
            validate_config(dump(document), [], REALITY)

    def test_self_hosted_reality_node_requires_sni(self):
        document = valid_document()
        del document["proxies"][0]["servername"]

        with self.assertRaisesRegex(ValidationError, "REALITY"):
            validate_config(dump(document), [], REALITY)

    def test_self_hosted_reality_node_requires_client_fingerprint(self):
        document = valid_document()
        del document["proxies"][0]["client-fingerprint"]

        with self.assertRaisesRegex(ValidationError, "REALITY"):
            validate_config(dump(document), [], REALITY)

    def test_self_hosted_reality_node_requires_public_key(self):
        document = valid_document()
        del document["proxies"][0]["reality-opts"]["public-key"]

        with self.assertRaisesRegex(ValidationError, "REALITY"):
            validate_config(dump(document), [], REALITY)

    def test_self_hosted_reality_node_requires_short_id(self):
        document = valid_document()
        document["proxies"][0]["reality-opts"]["short-id"] = ""

        with self.assertRaisesRegex(ValidationError, "REALITY"):
            validate_config(dump(document), [], REALITY)

    def test_partial_reality_fields_are_rejected_for_vless_nodes(self):
        document = valid_document()
        document["proxies"][0]["flow"] = REALITY.required_flow
        del document["proxies"][0]["reality-opts"]

        with self.assertRaisesRegex(ValidationError, "REALITY"):
            validate_config(dump(document), [], REALITY)

    def test_vless_ws_tls_node_with_normal_servername_is_not_treated_as_reality(self):
        document = valid_document()
        document["proxies"][0] = {
            "name": "Home Node",
            "type": "vless",
            "server": "home.example.com",
            "port": 443,
            "uuid": "22222222-2222-4222-8222-222222222222",
            "network": "ws",
            "tls": True,
            "servername": "home.example.com",
        }
        document["proxy-groups"][0]["proxies"] = ["DIRECT", "Home Node", "Airport Node"]

        parsed = validate_config(dump(document), [], REALITY)

        self.assertEqual(parsed["proxies"][0]["servername"], "home.example.com")

    def test_valid_airport_home_non_reality_node_is_allowed(self):
        document = valid_document()
        document["proxies"][0] = {
            "name": "Home Node",
            "type": "vless",
            "server": "home.example.com",
            "port": 443,
            "uuid": "22222222-2222-4222-8222-222222222222",
            "network": "ws",
            "tls": True,
        }
        document["proxy-groups"][0]["proxies"] = ["DIRECT", "Home Node", "Airport Node"]

        parsed = validate_config(dump(document), [], REALITY)

        self.assertEqual(parsed["proxies"][0]["name"], "Home Node")

    def test_recursive_proxy_group_cycle_is_rejected(self):
        document = valid_document()
        document["proxy-groups"] = [
            {"name": "A", "type": "select", "proxies": ["B"]},
            {"name": "B", "type": "select", "proxies": ["A"]},
        ]
        document["rules"] = ["MATCH,A"]

        with self.assertRaisesRegex(ValidationError, r"proxy-groups\[1\]\.proxies\[0\]") as context:
            validate_config(dump(document), [], REALITY)

        self.assertNotIn("A", str(context.exception))
        self.assertNotIn("B", str(context.exception))

    def test_whitespace_only_proxy_name_is_rejected(self):
        document = valid_document()
        document["proxies"][0]["name"] = "   "

        with self.assertRaisesRegex(ValidationError, r"proxies\[0\]\.name"):
            validate_config(dump(document), [], REALITY)

    def test_whitespace_only_group_name_is_rejected(self):
        document = valid_document()
        document["proxy-groups"][0]["name"] = " \t "

        with self.assertRaisesRegex(ValidationError, r"proxy-groups\[0\]\.name"):
            validate_config(dump(document), [], REALITY)

    def test_non_boolean_include_all_is_rejected(self):
        document = valid_document()
        document["proxy-groups"][0]["include-all"] = "banana"

        with self.assertRaisesRegex(
            ValidationError, r"proxy-groups\[0\]\.include-all must be a boolean"
        ):
            validate_config(dump(document), [], REALITY)

    def test_boolean_include_all_with_proxies_is_accepted(self):
        document = valid_document()
        document["proxy-groups"][0]["include-all"] = True

        self.assertIsInstance(
            validate_config(dump(document), [], REALITY), dict
        )


if __name__ == "__main__":
    unittest.main()
