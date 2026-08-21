import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from clash_sub.models import RealitySettings
from clash_sub.converter import (
    SourceError,
    SubconverterClient,
    load_local_proxies,
    merge_proxy_sources,
    normalize_reality_proxy,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"
REALITY_YAML = (FIXTURES / "reality-converted.yaml").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, payload=b"", headers=None):
        self.payload = payload
        self.headers = headers or {}
        self.closed = False
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size < 0:
            return self.payload
        return self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False


class FakeOpener:
    def __init__(self, payload=None, *, response=None, error=None):
        self.payload = payload
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        return FakeResponse(self.payload or b"")


class ConverterTests(unittest.TestCase):
    def test_build_url_encodes_source_and_requests_clash_node_list(self):
        client = SubconverterClient("http://127.0.0.1:25500", opener=FakeOpener())

        url = client.build_url("http://127.0.0.1:2096/sub/example?a=1&b=2")

        self.assertIn("target=clash", url)
        self.assertIn("list=true", url)
        self.assertIn("a%3D1%26b%3D2", url)

    def test_convert_reads_yaml_proxy_list(self):
        opener = FakeOpener(payload=REALITY_YAML.encode("utf-8"))
        client = SubconverterClient("http://127.0.0.1:25500", opener=opener)

        proxies = client.convert("http://127.0.0.1:2096/sub/example")

        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0]["name"], "Example REALITY")
        self.assertEqual(opener.calls[0][1], 20)

    def test_response_over_limit_does_not_echo_source_url(self):
        source_url = "http://127.0.0.1:2096/sub/private-value"

        with self.assertRaisesRegex(SourceError, "response exceeds") as context:
            SubconverterClient(
                "http://127.0.0.1:25500",
                opener=FakeOpener(payload=b"x" * 1025),
                max_bytes=1024,
            ).convert(source_url)

        self.assertNotIn(source_url, str(context.exception))

    def test_convert_requires_mapping_with_proxies_list(self):
        client = SubconverterClient(
            "http://127.0.0.1:25500",
            opener=FakeOpener(payload=b"items: []\n"),
        )

        with self.assertRaisesRegex(SourceError, "no proxy list"):
            client.convert("http://127.0.0.1:2096/sub/example")

    def test_convert_rejects_non_list_root(self):
        client = SubconverterClient(
            "http://127.0.0.1:25500",
            opener=FakeOpener(payload=b"- name: bad\n"),
        )

        with self.assertRaisesRegex(SourceError, "no proxy list"):
            client.convert("http://127.0.0.1:2096/sub/example")

    def test_convert_rejects_non_mapping_proxy_entries(self):
        client = SubconverterClient(
            "http://127.0.0.1:25500",
            opener=FakeOpener(payload=b"proxies:\n  - bad\n"),
        )

        with self.assertRaisesRegex(SourceError, "invalid proxies"):
            client.convert("http://127.0.0.1:2096/sub/example")

    def test_convert_rejects_empty_proxy_outputs(self):
        client = SubconverterClient(
            "http://127.0.0.1:25500",
            opener=FakeOpener(payload=b"proxies: []\n"),
        )

        with self.assertRaisesRegex(SourceError, "invalid proxies"):
            client.convert("http://127.0.0.1:2096/sub/example")

    def test_convert_returns_deep_copies(self):
        client = SubconverterClient(
            "http://127.0.0.1:25500",
            opener=FakeOpener(payload=REALITY_YAML.encode("utf-8")),
        )

        proxies = client.convert("http://127.0.0.1:2096/sub/example")
        proxies[0]["name"] = "Changed"
        proxies_again = client.convert("http://127.0.0.1:2096/sub/example")

        self.assertEqual(proxies_again[0]["name"], "Example REALITY")

    def test_reality_normalization_forces_public_address_and_keeps_credentials(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]

        normalized = normalize_reality_proxy(
            proxy,
            RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
        )

        self.assertEqual(normalized["server"], "198.51.100.25")
        self.assertEqual(normalized["port"], 443)
        self.assertEqual(normalized["uuid"], proxy["uuid"])
        self.assertEqual(normalized["reality-opts"], proxy["reality-opts"])

    def test_reality_normalizer_does_not_mutate_input_mapping(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        original = copy.deepcopy(proxy)

        normalize_reality_proxy(
            proxy,
            RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
        )

        self.assertEqual(proxy, original)

    def test_reality_normalizer_rejects_non_vless_nodes(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        proxy["type"] = "trojan"

        with self.assertRaisesRegex(SourceError, "non-VLESS"):
            normalize_reality_proxy(
                proxy,
                RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
            )

    def test_reality_normalizer_rejects_missing_reality_transport(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        proxy["tls"] = False

        with self.assertRaisesRegex(SourceError, "REALITY"):
            normalize_reality_proxy(
                proxy,
                RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
            )

    def test_reality_normalizer_rejects_websocket_for_self_hosted_nodes(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        proxy["network"] = "ws"

        with self.assertRaisesRegex(SourceError, "REALITY"):
            normalize_reality_proxy(
                proxy,
                RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
            )

    def test_reality_normalizer_rejects_xhttp_for_self_hosted_nodes(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        proxy["network"] = "xhttp"

        with self.assertRaisesRegex(SourceError, "REALITY"):
            normalize_reality_proxy(
                proxy,
                RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
            )

    def test_reality_normalizer_rejects_missing_short_id(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        proxy["reality-opts"]["short-id"] = ""

        with self.assertRaisesRegex(SourceError, "REALITY"):
            normalize_reality_proxy(
                proxy,
                RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
            )

    def test_reality_normalizer_rejects_missing_public_key(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        del proxy["reality-opts"]["public-key"]

        with self.assertRaisesRegex(SourceError, "REALITY"):
            normalize_reality_proxy(
                proxy,
                RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
            )

    def test_reality_normalizer_rejects_missing_sni(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        del proxy["servername"]

        with self.assertRaisesRegex(SourceError, "REALITY"):
            normalize_reality_proxy(
                proxy,
                RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
            )

    def test_reality_normalizer_rejects_missing_client_fingerprint(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        del proxy["client-fingerprint"]

        with self.assertRaisesRegex(SourceError, "REALITY"):
            normalize_reality_proxy(
                proxy,
                RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
            )

    def test_reality_normalizer_rejects_wrong_flow(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        proxy["flow"] = "xtls-rprx-origin"

        with self.assertRaisesRegex(SourceError, "REALITY"):
            normalize_reality_proxy(
                proxy,
                RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
            )

    def test_load_local_proxies_reads_snapshot_without_normalizing(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "home.yaml"
            path.write_text(
                "proxies:\n"
                "  - name: Home Node\n"
                "    type: trojan\n"
                "    server: 198.51.100.77\n"
                "    port: 443\n",
                encoding="utf-8",
            )

            proxies = load_local_proxies(path)

        self.assertEqual(proxies[0]["type"], "trojan")

    def test_load_local_proxies_rejects_malformed_snapshots(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "home.yaml"
            path.write_text("proxies: [\n", encoding="utf-8")

            with self.assertRaisesRegex(SourceError, "unreadable or invalid"):
                load_local_proxies(path)

    def test_load_local_proxies_rejects_non_mapping_entries(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "home.yaml"
            path.write_text("proxies:\n  - nope\n", encoding="utf-8")

            with self.assertRaisesRegex(SourceError, "invalid proxies"):
                load_local_proxies(path)

    def test_merge_proxy_sources_keeps_unique_names(self):
        merged = merge_proxy_sources(
            (
                ("3x-ui", ({"name": "Alpha", "type": "vless"},)),
                ("家庭", ({"name": "Beta", "type": "ss"},)),
            )
        )

        self.assertEqual([item["name"] for item in merged], ["Alpha", "Beta"])

    def test_merge_proxy_sources_labels_only_colliding_names(self):
        merged = merge_proxy_sources(
            (
                ("3x-ui", ({"name": "Shared", "type": "vless"},)),
                ("机场", ({"name": "Shared", "type": "ss"},)),
                ("家庭", ({"name": "Shared", "type": "trojan"},)),
            )
        )

        self.assertEqual(
            [item["name"] for item in merged],
            ["Shared [3x-ui]", "Shared [机场]", "Shared [家庭]"],
        )

    def test_merge_proxy_sources_adds_numeric_suffix_when_labeled_name_exists(self):
        merged = merge_proxy_sources(
            (
                ("3x-ui", ({"name": "Shared", "type": "vless"},)),
                ("家庭", ({"name": "Shared", "type": "trojan"}, {"name": "Shared [家庭]", "type": "trojan"})),
            )
        )

        self.assertEqual(
            [item["name"] for item in merged],
            ["Shared [3x-ui]", "Shared [家庭]", "Shared [家庭]-2"],
        )

    def test_merge_proxy_sources_does_not_mutate_inputs(self):
        first = {"name": "Shared", "type": "vless"}
        second = {"name": "Shared", "type": "trojan"}
        first_original = copy.deepcopy(first)
        second_original = copy.deepcopy(second)

        merge_proxy_sources((("3x-ui", (first,)), ("家庭", (second,))))

        self.assertEqual(first, first_original)
        self.assertEqual(second, second_original)
