import copy
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from clash_sub.domain import MEMBER_VARIANTS, OWNER_VARIANTS

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


PROVIDER_URL = "https://sub.example.test:443/s/owner-token/AmyTelecom.yaml"
OWNER_TOKEN = "owner-token"


def owner_document():
    document = valid_document()
    document["proxy-providers"] = {
        "AmyTelecom": {
            "type": "http",
            "url": PROVIDER_URL,
            "path": "./proxy_providers/AmyTelecom-%s.yaml" % ("1" * 64),
            "interval": 0,
        }
    }
    document["proxy-groups"][0]["use"] = ["AmyTelecom"]
    return document


class ProviderMappingTests(unittest.TestCase):
    def test_owner_document_with_the_exact_provider_mapping_is_accepted(self):
        parsed = validate_clash(
            dump(owner_document()), (OWNER_TOKEN,), allowed_provider_url=PROVIDER_URL
        )

        self.assertEqual(tuple(parsed["proxy-providers"]), ("AmyTelecom",))

    def test_provider_mapping_is_rejected_without_the_owner_authorization(self):
        with self.assertRaisesRegex(CheckError, "proxy-providers"):
            validate_clash(dump(owner_document()), ())

    def test_only_the_exact_amytelecom_mapping_is_accepted(self):
        cases = (
            ("foreign name", {"AmyTelecom2": owner_document()["proxy-providers"]["AmyTelecom"]}),
            ("extra provider", dict(owner_document()["proxy-providers"], Extra={"type": "http", "url": PROVIDER_URL})),
            ("wrong type", {"AmyTelecom": dict(owner_document()["proxy-providers"]["AmyTelecom"], type="file")}),
            ("wrong url", {"AmyTelecom": dict(owner_document()["proxy-providers"]["AmyTelecom"], url=PROVIDER_URL + "x")}),
            ("positive interval", {"AmyTelecom": dict(owner_document()["proxy-providers"]["AmyTelecom"], interval=3600)}),
            ("boolean interval", {"AmyTelecom": dict(owner_document()["proxy-providers"]["AmyTelecom"], interval=False)}),
            ("missing interval", {"AmyTelecom": {"type": "http", "url": PROVIDER_URL}}),
            ("wrong path", {"AmyTelecom": dict(owner_document()["proxy-providers"]["AmyTelecom"], path="./proxy_providers/AmyTelecom.yaml")}),
            ("short digest path", {"AmyTelecom": dict(owner_document()["proxy-providers"]["AmyTelecom"], path="./proxy_providers/AmyTelecom-abc.yaml")}),
            ("non-mapping provider", {"AmyTelecom": "https://airport.example"}),
        )
        for name, providers in cases:
            with self.subTest(name=name):
                document = owner_document()
                document["proxy-providers"] = providers
                with self.assertRaisesRegex(CheckError, "provider"):
                    validate_clash(dump(document), (), allowed_provider_url=PROVIDER_URL)

    def test_owner_authorization_requires_a_declared_provider(self):
        document = valid_document()

        with self.assertRaisesRegex(CheckError, "airport proxy-provider"):
            validate_clash(dump(document), (), allowed_provider_url=PROVIDER_URL)

    def test_owner_token_may_only_appear_inside_the_expected_provider_url(self):
        validate_clash(dump(owner_document()), (OWNER_TOKEN,), allowed_provider_url=PROVIDER_URL)

        document = owner_document()
        document["notes"] = "leaked %s inline" % OWNER_TOKEN
        with self.assertRaisesRegex(CheckError, "forbidden value"):
            validate_clash(dump(document), (OWNER_TOKEN,), allowed_provider_url=PROVIDER_URL)

    def test_the_full_provider_url_is_rejected_outside_the_provider_url_field(self):
        for field in ("notes", ("dns", "notes"), "comment"):
            document = owner_document()
            if isinstance(field, tuple):
                document["dns"]["notes"] = PROVIDER_URL
            else:
                document[field] = PROVIDER_URL
            with self.subTest(field=field):
                with self.assertRaisesRegex(CheckError, "forbidden value"):
                    validate_clash(dump(document), (OWNER_TOKEN,), allowed_provider_url=PROVIDER_URL)

        document = owner_document()
        document["proxy-groups"][0]["notes"] = [PROVIDER_URL]
        with self.assertRaisesRegex(CheckError, "forbidden value"):
            validate_clash(dump(document), (OWNER_TOKEN,), allowed_provider_url=PROVIDER_URL)

    def test_member_documents_reject_the_provider_url_in_any_field(self):
        document = valid_document()
        document["notes"] = PROVIDER_URL

        with self.assertRaisesRegex(CheckError, "forbidden value"):
            validate_clash(dump(document), (PROVIDER_URL, OWNER_TOKEN))

        document = valid_document()
        document["notes"] = "inline %s leak" % OWNER_TOKEN
        with self.assertRaisesRegex(CheckError, "forbidden value"):
            validate_clash(dump(document), (PROVIDER_URL, OWNER_TOKEN))

    def test_group_use_must_reference_a_declared_provider(self):
        document = owner_document()
        document["proxy-groups"][0]["use"] = ["Unknown"]

        with self.assertRaisesRegex(CheckError, "unknown provider"):
            validate_clash(dump(document), (), allowed_provider_url=PROVIDER_URL)

        document = owner_document()
        document["proxy-groups"][0]["use"] = []
        with self.assertRaisesRegex(CheckError, "use"):
            validate_clash(dump(document), (), allowed_provider_url=PROVIDER_URL)

    def test_member_documents_reject_any_use_reference(self):
        document = valid_document()
        document["proxy-groups"][0]["use"] = ["AmyTelecom"]

        with self.assertRaisesRegex(CheckError, "unknown provider"):
            validate_clash(dump(document), ())

    def test_a_yaml_alias_cycle_fails_closed_instead_of_recursing(self):
        text = (
            "dns: &cycle {enable: true}\n"
            "proxies: []\n"
            "proxy-groups: []\n"
            "rule-providers:\n"
            "  Direct: {type: http}\n"
            "rules:\n"
            "- MATCH,DIRECT\n"
            "notes:\n"
            "  next: *cycle\n"
            "  self: &inner\n"
            "    peer: *inner\n"
        )

        with self.assertRaisesRegex(CheckError, "invalid YAML"):
            validate_clash(text, ())


class LightweightChecksTests(unittest.TestCase):
    def test_fixed_profile_authorization_sets_have_no_legacy_aliases(self):
        self.assertEqual(
            OWNER_VARIANTS,
            ("compat-office", "compat-universal", "balance-office"),
        )
        self.assertEqual(MEMBER_VARIANTS, ("compat-universal",))

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

    def test_rule_set_referencing_an_unknown_provider_is_rejected_without_naming_it(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        document["rules"] = ["RULE-SET,Missing,DIRECT", "MATCH,Selector"]

        with self.assertRaises(CheckError) as caught:
            validate_clash(dump(document), ())

        self.assertRegex(str(caught.exception), "unknown provider")
        self.assertNotIn("Missing", str(caught.exception))

    def test_known_rule_set_providers_keep_passing_structural_checks(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        document["rules"] = [
            "RULE-SET,Direct,DIRECT,no-resolve",
            "MATCH,Selector",
        ]

        self.assertIsNotNone(validate_clash(dump(document), ()))

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

    def test_reality_requires_raw_tcp_vision_semantics(self):
        self.assertIsNotNone(validate_clash)
        invalid_values = (
            (("network",), "ws"),
            (("network",), ""),
            (("network",), 123),
            (("flow",), "xtls-rprx-origin"),
            (("flow",), ""),
            (("flow",), 123),
        )
        for path, value in invalid_values:
            with self.subTest(path=path, value=value):
                document = copy.deepcopy(valid_document())
                document["proxies"][0][path[0]] = value

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

    def test_proxy_group_name_collision_is_rejected_before_cycle_analysis(self):
        self.assertIsNotNone(validate_clash)
        document = valid_document()
        document["proxy-groups"] = [
            {"name": "Owner 3x-ui", "type": "select", "proxies": ["DIRECT"]},
        ]
        document["rules"] = ["MATCH,DIRECT"]

        with self.assertRaisesRegex(CheckError, "proxy name conflicts with proxy group name") as context:
            validate_clash(dump(document), ())
        self.assertNotIn("Owner 3x-ui", str(context.exception))
