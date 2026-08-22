import copy
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

try:
    from clash_sub.checks import CheckError, MihomoValidator, validate_clash
except ImportError:
    CheckError = None
    MihomoValidator = None
    validate_clash = None


def valid_document():
    return {
        "dns": {"enable": True},
        "proxies": [
            {
                "name": "Owner 3x-ui",
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
            },
            {
                "name": "Airport",
                "type": "trojan",
                "server": "airport.example.com",
                "port": 443,
                "password": "synthetic-password",
            },
        ],
        "proxy-groups": [
            {"name": "Selector", "type": "select", "proxies": ["DIRECT", "Owner 3x-ui", "Airport"]},
        ],
        "rule-providers": {"Direct": {"type": "http", "behavior": "classical"}},
        "rules": ["RULE-SET,Direct,DIRECT", "MATCH,Selector"],
    }


def dump(document):
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


class LightweightChecksTests(unittest.TestCase):
    def test_valid_document_is_accepted(self):
        self.assertIsNotNone(validate_clash)

        parsed = validate_clash(dump(valid_document()), ())

        self.assertEqual(parsed["proxies"][0]["name"], "Owner 3x-ui")

    def test_duplicate_proxy_name_is_rejected(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        document["proxies"][1]["name"] = "Owner 3x-ui"

        with self.assertRaisesRegex(CheckError, "duplicate proxy name"):
            validate_clash(dump(document), ())

    def test_duplicate_group_name_is_rejected(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        document["proxy-groups"].append({"name": "Selector", "type": "select", "proxies": ["DIRECT"]})

        with self.assertRaisesRegex(CheckError, "duplicate proxy group name"):
            validate_clash(dump(document), ())

    def test_unknown_group_and_rule_targets_are_rejected(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        document["proxy-groups"][0]["proxies"].append("Unknown Group")

        with self.assertRaisesRegex(CheckError, "unknown target"):
            validate_clash(dump(document), ())

        document = valid_document()
        document["rules"].append("DOMAIN,example.com,Unknown Group")
        with self.assertRaisesRegex(CheckError, "unknown target"):
            validate_clash(dump(document), ())

    def test_proxy_providers_and_incomplete_reality_are_rejected(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        document["proxy-providers"] = {"remote": {"type": "http"}}

        with self.assertRaisesRegex(CheckError, "proxy-providers"):
            validate_clash(dump(document), ())

        document = valid_document()
        del document["proxies"][0]["reality-opts"]["short-id"]
        with self.assertRaisesRegex(CheckError, "REALITY"):
            validate_clash(dump(document), ())

        document = valid_document()
        del document["proxies"][0]["reality-opts"]
        with self.assertRaisesRegex(CheckError, "REALITY"):
            validate_clash(dump(document), ())

    def test_forbidden_source_values_are_rejected_without_echoing_them(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        forbidden = "http://127.0.0.1:2096/clash/private-sub-id"
        document["notes"] = forbidden

        with self.assertRaisesRegex(CheckError, "forbidden value") as context:
            validate_clash(dump(document), (forbidden, "public-token"))

        self.assertNotIn(forbidden, str(context.exception))

    def test_loopback_urls_are_rejected_even_without_a_specific_forbidden_value(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        document["notes"] = "http://127.0.0.1:2096/clash/private-sub-id"

        with self.assertRaisesRegex(CheckError, "forbidden value"):
            validate_clash(dump(document), ())

    def test_mihomo_uses_the_pinned_safe_invocation(self):
        self.assertIsNotNone(MihomoValidator)
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

    def test_mihomo_timeout_and_failure_are_sanitized(self):
        self.assertIsNotNone(MihomoValidator)

        def timeout_runner(arguments, **kwargs):
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"], output="private output")

        def failed_runner(arguments, **kwargs):
            return subprocess.CompletedProcess(arguments, 1, stdout="private output", stderr="private error")

        candidate = Path("/private/candidate.yaml")
        with self.assertRaisesRegex(CheckError, "timed out") as timeout:
            MihomoValidator(Path("/opt/mihomo/mihomo"), runner=timeout_runner).validate(candidate)
        self.assertNotIn("private output", str(timeout.exception))
        self.assertIsNone(timeout.exception.__cause__)
        self.assertIsNone(timeout.exception.__context__)

        with self.assertRaisesRegex(CheckError, "failed") as failed:
            MihomoValidator(Path("/opt/mihomo/mihomo"), runner=failed_runner).validate(candidate)
        self.assertNotIn("private error", str(failed.exception))

    def test_reality_values_require_their_expected_types_and_ranges(self):
        self.assertIsNotNone(validate_clash)
        invalid_values = (
            (("tls",), False),
            (("port",), True),
            (("port",), 0),
            (("port",), 65536),
            (("port",), "443"),
            (("server",), 123),
            (("uuid",), 123),
            (("network",), 123),
            (("servername",), 123),
            (("client-fingerprint",), 123),
            (("reality-opts", "public-key"), 123),
            (("reality-opts", "short-id"), 123),
        )
        for path, value in invalid_values:
            with self.subTest(path=path, value=value):
                document = copy.deepcopy(valid_document())
                target = document["proxies"][0]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value

                with self.assertRaisesRegex(CheckError, "REALITY"):
                    validate_clash(dump(document), ())

    def test_non_reality_proxy_fields_are_not_overvalidated(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        document["proxies"][1]["tls"] = False
        document["proxies"][1]["port"] = "not-a-reality-port"

        validate_clash(dump(document), ())

    def test_proxy_group_cycle_is_rejected_without_group_names_in_the_error(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        document["proxy-groups"] = [
            {"name": "First Secret Group", "type": "select", "proxies": ["Second Secret Group"]},
            {"name": "Second Secret Group", "type": "select", "proxies": ["First Secret Group"]},
        ]
        document["rules"] = ["MATCH,First Secret Group"]

        with self.assertRaisesRegex(CheckError, "recursive proxy group reference") as context:
            validate_clash(dump(document), ())
        self.assertNotIn("First Secret Group", str(context.exception))
        self.assertNotIn("Second Secret Group", str(context.exception))
