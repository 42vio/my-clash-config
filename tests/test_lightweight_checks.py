import copy
import unittest

import yaml

from clash_sub.checks import CheckError, validate_clash
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
        }],
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


if __name__ == "__main__":
    unittest.main()
