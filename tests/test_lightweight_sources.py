import contextlib
import copy
import io
import os
import stat
import tempfile
import traceback
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch
from pathlib import Path
import urllib.request
from urllib.request import Request

import yaml

from clash_sub.domain import HomeOverlay, Traffic
from clash_sub.sources import (
    HomeSourceError,
    SourceError,
    XUI_INBOUND_PORT,
    _HttpsRedirectHandler,
    download_airport_document,
    dump_home_overlay,
    fetch_xui_proxies,
    home_overlay_digest,
    load_home_overlay,
    load_proxy_snapshot,
    merge_proxy_sources,
    merge_proxy_sources_with_aliases,
    normalize_xui_endpoints,
    parse_home_overlay,
    parse_subscription_userinfo,
)


class FakeResponse:
    def __init__(self, body, url, headers=None):
        self.body = body
        self.url = url
        self.headers = headers or {}
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body[:size]

    def geturl(self):
        return self.url


def proxy_yaml(name="Example"):
    return ("proxies:\n- name: %s\n  type: ss\n" % name).encode("utf-8")


def airport_document():
    """An upstream airport body whose exact bytes must survive the pipeline."""
    return (
        "# airport header comment\n"
        "proxies:\n"
        "- {name: 'Amy 01', type: ss, server: a.example, port: 443}\n"
        "- name: Amy 02\n"
        "  type: trojan\n"
        "  server: b.example\n"
        "  port: 443\n"
        "# trailing comment kept verbatim\n"
    ).encode("utf-8")


def home_document():
    """A synthetic six-field home overlay holding only fake values."""
    return {
        "proxies": [
            {
                "name": "HomeExit",
                "type": "ss",
                "server": "home.example.invalid",
                "port": 8388,
                "cipher": "aes-128-gcm",
                "password": "synthetic-home-password",
            },
            {
                "name": "HomeRelay",
                "type": "ss",
                "server": "relay.example.invalid",
                "port": 8389,
                "cipher": "aes-128-gcm",
                "password": "synthetic-home-password",
            },
        ],
        "proxy-groups": [
            {"name": "ProxyServer", "type": "select", "proxies": ["HomeExit"]},
            {"name": "HomeServer", "type": "select", "proxies": ["HomeRelay"]},
        ],
        "extend-proxy-groups": {
            "BiliBili": ["ProxyServer"],
            "国内流媒体": ["ProxyServer"],
        },
        "inject-node-groups": ["ProxyServer"],
        "inject-home-node-groups": ["HomeServer"],
        "rules": ["IP-CIDR,192.168.0.0/16,HomeServer,no-resolve"],
    }


def home_document_bytes():
    return yaml.safe_dump(
        home_document(), allow_unicode=True, sort_keys=False
    ).encode("utf-8")


class SourceFetchingTests(unittest.TestCase):
    def opener_for(self, response):
        def opener(request, timeout):
            self.request = request
            self.timeout = timeout
            return response

        return opener

    def failing_opener(self, request, timeout):
        raise OSError("private source unavailable")

    def test_xui_allows_only_a_loopback_http_path_without_extra_url_parts(self):
        response = FakeResponse(proxy_yaml(), "http://127.0.0.1:2096/clash/member")
        result = fetch_xui_proxies(
            "http://127.0.0.1:2096/clash/member", 1024, opener=self.opener_for(response)
        )
        self.assertEqual(result[0]["name"], "Example")
        self.assertEqual(self.request.full_url, "http://127.0.0.1:2096/clash/member")
        self.assertEqual(self.timeout, 15)
        for invalid in (
            "https://127.0.0.1:2096/clash/member",
            "http://localhost:2096/clash/member",
            "http://127.0.0.1/clash/member",
            "http://127.0.0.1:0/clash/member",
            "http://user@127.0.0.1:2096/clash/member",
            "http://127.0.0.1:2096/clash/member?token=1",
            "http://127.0.0.1:2096/clash/member#fragment",
            "http://127.0.0.1:2096",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SourceError):
                    fetch_xui_proxies(invalid, 1024, opener=self.opener_for(response))

    def test_airport_requires_https_and_rejects_non_https_redirects(self):
        response = FakeResponse(proxy_yaml(), "http://redirected.example/subscription")
        with self.assertRaises(SourceError):
            download_airport_document(
                "https://airport.example/private-token", 1024, opener=self.opener_for(response)
            )
        with self.assertRaises(SourceError):
            download_airport_document(
                "http://airport.example/private-token", 1024, opener=self.opener_for(response)
            )

    def test_airport_download_preserves_the_exact_response_bytes(self):
        body = airport_document()
        response = FakeResponse(body, "https://airport.example/final")

        result = download_airport_document(
            "https://airport.example/private-token", 1024, opener=self.opener_for(response)
        )

        self.assertEqual(result, body)
        self.assertIsInstance(result, bytes)

    def test_airport_download_validates_only_a_non_empty_proxy_document(self):
        for body in (
            b"",
            b"[]\n",
            b"proxies: []\n",
            b"rules:\n- MATCH,DIRECT\n",
            b"proxies: not-a-list\n",
            b"!!python/object/apply:os.system ['echo unsafe']\n",
            b"proxies: [\n",
        ):
            response = FakeResponse(body, "https://airport.example/final")
            with self.subTest(body=body):
                with self.assertRaises(SourceError):
                    download_airport_document(
                        "https://airport.example/private-token",
                        1024,
                        opener=self.opener_for(response),
                    )

    def test_airport_download_reads_only_the_bounded_extra_byte(self):
        oversized = FakeResponse(
            airport_document() + b"x" * 1024, "https://airport.example/final"
        )
        with self.assertRaises(SourceError):
            download_airport_document(
                "https://airport.example/private-token", 32, opener=self.opener_for(oversized)
            )
        self.assertEqual(oversized.read_sizes, [33])

    def test_airport_redirect_policy_allows_only_three_https_hops(self):
        handler = _HttpsRedirectHandler()
        request = Request("https://airport.example/first")
        for target in (
            "https://airport.example/second",
            "https://airport.example/third",
            "https://airport.example/fourth",
        ):
            request = handler.redirect_request(request, None, 302, "Found", {}, target)
        with self.assertRaises(SourceError):
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://airport.example/fifth"
            )
        with self.assertRaises(SourceError):
            _HttpsRedirectHandler().redirect_request(
                Request("https://airport.example/first"),
                None,
                302,
                "Found",
                {},
                "http://airport.example/second",
            )

    def test_fetch_reads_only_the_bounded_extra_byte_and_rejects_bad_bodies(self):
        oversized = FakeResponse(proxy_yaml() + b"x" * 1024, "http://127.0.0.1:2096/clash/member")
        with self.assertRaises(SourceError):
            fetch_xui_proxies(
                "http://127.0.0.1:2096/clash/member", 32, opener=self.opener_for(oversized)
            )
        self.assertEqual(oversized.read_sizes, [33])

        for body in (
            b"",
            b"[]\n",
            b"proxies: []\n",
            b"proxies:\n- name: ''\n",
            b"proxies:\n- not-a-mapping\n",
            b"!!python/object/apply:os.system ['echo unsafe']\n",
        ):
            response = FakeResponse(body, "http://127.0.0.1:2096/clash/member")
            with self.subTest(body=body):
                with self.assertRaises(SourceError):
                    fetch_xui_proxies(
                        "http://127.0.0.1:2096/clash/member", 1024, opener=self.opener_for(response)
                    )

    def test_proxy_source_repairs_utf16_surrogate_pairs_before_rendering(self):
        response = FakeResponse(
            (
                'proxies:\n- name: "\\uD83C\\uDDED\\uD83C\\uDDF0 香港 01"\n'
                "  type: ss\n"
            ).encode("utf-8"),
            "http://127.0.0.1:2096/clash/member",
        )

        result = fetch_xui_proxies(
            "http://127.0.0.1:2096/clash/member", 1024, opener=self.opener_for(response)
        )

        self.assertEqual(result[0]["name"], chr(0x1F1ED) + chr(0x1F1F0) + " 香港 01")

    def test_airport_error_never_echoes_url_or_prints_it(self):
        secret = "https://airport.example/private-five-minute-token"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SourceError) as caught:
                download_airport_document(secret, 1024, opener=self.failing_opener)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(caught.exception))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_airport_error_never_echoes_url_in_its_traceback(self):
        secret = "https://airport.example/private-five-minute-token"

        def failing_opener(request, timeout):
            raise OSError(secret)

        with self.assertRaises(SourceError) as caught:
            download_airport_document(secret, 1024, opener=failing_opener)

        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(secret, rendered)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_default_xui_downloader_disables_ambient_proxies(self):
        response = FakeResponse(
            proxy_yaml(), "http://127.0.0.1:2096/clash/member"
        )
        captured = {}

        class CapturingOpener:
            def open(self, request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return response

        def build_opener(*handlers):
            captured["handlers"] = handlers
            return CapturingOpener()

        with patch("urllib.request.build_opener", side_effect=build_opener) as builder:
            proxies = fetch_xui_proxies("http://127.0.0.1:2096/clash/member", 1024)

        self.assertEqual(proxies[0]["name"], "Example")
        builder.assert_called_once()
        self.assertEqual(len(captured["handlers"]), 1)
        self.assertIsInstance(captured["handlers"][0], urllib.request.ProxyHandler)
        self.assertEqual(captured["handlers"][0].proxies, {})
        self.assertEqual(captured["timeout"], 15)

    def test_default_airport_downloader_disables_ambient_proxies_and_preserves_redirect_policy(self):
        response = FakeResponse(proxy_yaml(), "https://airport.example/final")
        captured = {}

        class CapturingOpener:
            def open(self, request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return response

        def build_opener(*handlers):
            captured["handlers"] = handlers
            return CapturingOpener()

        with patch("urllib.request.build_opener", side_effect=build_opener) as builder:
            document = download_airport_document("https://airport.example/private-token", 1024)

        self.assertEqual(document, proxy_yaml())
        builder.assert_called_once()
        self.assertEqual(len(captured["handlers"]), 2)
        self.assertIsInstance(captured["handlers"][0], urllib.request.ProxyHandler)
        self.assertEqual(captured["handlers"][0].proxies, {})
        self.assertIsInstance(captured["handlers"][1], _HttpsRedirectHandler)
        self.assertEqual(captured["timeout"], 15)


def proxy(name):
    return {"name": name, "type": "ss"}


class SnapshotAndMergeTests(unittest.TestCase):
    def _home_snapshot(self, directory):
        path = Path(directory) / "home.yaml"
        path.write_text(
            "proxies:\n- name: Home\n  type: ss\n  server: example.invalid\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        return path

    def test_snapshot_loads_a_regular_single_link_file_with_the_expected_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._home_snapshot(directory)

            self.assertEqual(
                load_proxy_snapshot(path),
                [{"name": "Home", "type": "ss", "server": "example.invalid"}],
            )

    def test_snapshot_rejects_a_file_owned_by_someone_other_than_the_effective_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._home_snapshot(directory)
            if os.geteuid() == 0:
                os.chown(path, 1, -1)
                with self.assertRaises(SourceError):
                    load_proxy_snapshot(path)
            else:
                with patch("clash_sub.sources.os.geteuid", return_value=os.geteuid() + 1):
                    with self.assertRaises(SourceError):
                        load_proxy_snapshot(path)

    def test_snapshot_rejects_a_hard_linked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._home_snapshot(directory)
            os.link(path, path.with_name("linked-home.yaml"))

            with self.assertRaises(SourceError):
                load_proxy_snapshot(path)

    def test_snapshot_rejects_insecure_mode_and_invalid_proxy_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "home.yaml"
            path.write_text("proxies: []\n", encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaises(SourceError):
                load_proxy_snapshot(path)
            path.write_text("proxies:\n- name: Home\n  type: ss\n", encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaises(SourceError):
                load_proxy_snapshot(path)

    def test_merge_uses_deterministic_source_suffixes_without_mutating_inputs(self):
        xui = [{"name": "Shared", "type": "ss"}, {"name": "Only xui", "type": "ss"}]
        airport = [{"name": "Shared", "type": "ss"}]
        home = [{"name": "Shared", "type": "ss"}]
        originals = copy.deepcopy((xui, airport, home))

        merged = merge_proxy_sources((("3x-ui", xui), ("机场", airport), ("家庭", home)))

        self.assertEqual(
            [proxy["name"] for proxy in merged],
            ["Shared [3x-ui]", "Only xui", "Shared [机场]", "Shared [家庭]"],
        )
        self.assertEqual((xui, airport, home), originals)

    def test_merge_disambiguates_presuffixed_and_duplicate_source_names(self):
        xui = [
            {"name": "Shared", "type": "ss"},
            {"name": "Shared [3x-ui]", "type": "ss"},
            {"name": "Repeated", "type": "ss"},
            {"name": "Repeated", "type": "ss"},
        ]
        airport = [{"name": "Shared", "type": "ss"}]
        originals = copy.deepcopy((xui, airport))

        merged = merge_proxy_sources((("3x-ui", xui), ("机场", airport)))
        names = [proxy["name"] for proxy in merged]

        self.assertEqual(
            names,
            [
                "Shared [3x-ui]",
                "Shared [3x-ui] [2]",
                "Repeated [3x-ui]",
                "Repeated [3x-ui] [2]",
                "Shared [机场]",
            ],
        )
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual((xui, airport), originals)

    def test_merge_returns_home_aliases_for_collision_rewrites(self):
        merged, aliases = merge_proxy_sources_with_aliases(
            (("3x-ui", [proxy("Duplicate")]), ("home", [proxy("Duplicate")]))
        )

        self.assertEqual(
            [item["name"] for item in merged], ["Duplicate [3x-ui]", "Duplicate [home]"]
        )
        self.assertEqual(aliases["home"], {"Duplicate": "Duplicate [home]"})
        self.assertEqual(aliases["3x-ui"], {"Duplicate": "Duplicate [3x-ui]"})

    def test_merge_rejects_duplicate_names_inside_one_home_source(self):
        with self.assertRaises(SourceError):
            merge_proxy_sources_with_aliases(
                (("3x-ui", [proxy("Shared")]), ("home", [proxy("Same"), proxy("Same")]))
            )


class TrafficHeaderTests(unittest.TestCase):
    def test_parses_only_bounded_nonnegative_subscription_traffic(self):
        self.assertEqual(
            parse_subscription_userinfo("upload=12; download=34; total=56; expire=78"),
            Traffic(upload=12, download=34, total=56, expiry_ms=78),
        )
        for value in (
            "upload=-1; download=34; total=56; expire=78",
            "upload=1; download=2; total=three; expire=4",
            "upload=1; download=2; total=3",
            "upload=1; download=2; total=3; expire=4; other=5",
            "upload=" + "9" * 1024,
        ):
            with self.subTest(value=value[:32]):
                with self.assertRaises(SourceError):
                    parse_subscription_userinfo(value)


class EndpointNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.proxies = [
            {
                "name": "reality-node",
                "type": "vless",
                "server": "panel.example.com",
                "port": 10443,
                "uuid": "uuid-value",
                "tls": True,
                "servername": "www.example.com",
                "reality-opts": {"public-key": "key", "short-id": "sid"},
            }
        ]

    def test_rewrites_server_and_port_only(self):
        normalized = normalize_xui_endpoints(self.proxies, "example.com:443")

        self.assertEqual(normalized[0]["server"], "example.com")
        self.assertEqual(normalized[0]["port"], 443)
        self.assertEqual(normalized[0]["servername"], "www.example.com")
        self.assertEqual(normalized[0]["uuid"], "uuid-value")
        self.assertEqual(normalized[0]["reality-opts"]["public-key"], "key")

    def test_rejects_node_with_unexpected_inbound_port(self):
        self.proxies[0]["port"] = 10544

        with self.assertRaisesRegex(SourceError, "proxy source rejected"):
            normalize_xui_endpoints(self.proxies, "example.com:443")

    def test_rejects_node_without_port(self):
        del self.proxies[0]["port"]

        with self.assertRaisesRegex(SourceError, "proxy source rejected"):
            normalize_xui_endpoints(self.proxies, "example.com:443")

    def test_rejects_invalid_endpoint(self):
        for endpoint in ("", "example.com", "https://example.com:443", "example.com:8443"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(SourceError, "proxy source rejected"):
                    normalize_xui_endpoints(self.proxies, endpoint)

    def test_does_not_mutate_input(self):
        original = copy.deepcopy(self.proxies)

        normalize_xui_endpoints(self.proxies, "example.com:443")

        self.assertEqual(self.proxies, original)

    def test_inbound_port_constant_matches_xui_gate(self):
        from clash_sub import xui as xui_module

        self.assertEqual(XUI_INBOUND_PORT, xui_module._REALITY_INBOUND_PORT)


class HomeOverlaySourceTests(unittest.TestCase):
    max_bytes = 5 * 1024 * 1024

    def assertHomeCode(self, payload, code):
        with self.assertRaises(HomeSourceError) as caught:
            parse_home_overlay(payload, self.max_bytes)
        self.assertEqual(caught.exception.code, code)

    def assertDocumentCode(self, mutation, code):
        document = home_document()
        mutation(document)
        self.assertHomeCode(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode(
                "utf-8"
            ),
            code,
        )

    def _write_home_file(self, directory):
        path = Path(directory) / "home.yaml"
        path.write_bytes(home_document_bytes())
        os.chmod(path, 0o600)
        return path

    def test_six_field_home_overlay_round_trips_without_mutable_aliases(self):
        payload = home_document_bytes()
        home = parse_home_overlay(payload, len(payload))

        self.assertEqual(home.inject_node_groups, ("ProxyServer",))
        self.assertEqual(home.inject_home_node_groups, ("HomeServer",))
        self.assertEqual(
            dict(home.extend_proxy_groups),
            {"BiliBili": ("ProxyServer",), "国内流媒体": ("ProxyServer",)},
        )
        self.assertEqual(
            home.proxies,
            (
                {
                    "name": "HomeExit",
                    "type": "ss",
                    "server": "home.example.invalid",
                    "port": 8388,
                    "cipher": "aes-128-gcm",
                    "password": "synthetic-home-password",
                },
                {
                    "name": "HomeRelay",
                    "type": "ss",
                    "server": "relay.example.invalid",
                    "port": 8389,
                    "cipher": "aes-128-gcm",
                    "password": "synthetic-home-password",
                },
            ),
        )
        self.assertEqual(
            home.proxy_groups,
            (
                {"name": "ProxyServer", "type": "select", "proxies": ["HomeExit"]},
                {"name": "HomeServer", "type": "select", "proxies": ["HomeRelay"]},
            ),
        )
        self.assertEqual(home.rules, ("IP-CIDR,192.168.0.0/16,HomeServer,no-resolve",))
        self.assertEqual(parse_home_overlay(dump_home_overlay(home), 5 * 1024 * 1024), home)
        self.assertEqual(home_overlay_digest(home), home_overlay_digest(home))

    def test_home_overlay_deep_copies_and_freezes_constructor_input(self):
        proxies = [{"name": "HomeExit", "type": "ss"}]
        groups = [{"name": "ProxyServer", "type": "select", "proxies": ["HomeExit"]}]
        extensions = {"BiliBili": ["ProxyServer"]}
        home = HomeOverlay(
            proxies=tuple(proxies),
            proxy_groups=tuple(groups),
            extend_proxy_groups=extensions,
            inject_node_groups=("ProxyServer",),
            inject_home_node_groups=(),
            rules=(),
        )

        proxies[0]["name"] = "Mutated"
        groups[0]["proxies"].append("Mutated")
        extensions["BiliBili"].append("Mutated")
        extensions["Extra"] = ["ProxyServer"]

        self.assertEqual(home.proxies, ({"name": "HomeExit", "type": "ss"},))
        self.assertEqual(
            home.proxy_groups,
            ({"name": "ProxyServer", "type": "select", "proxies": ["HomeExit"]},),
        )
        self.assertEqual(dict(home.extend_proxy_groups), {"BiliBili": ("ProxyServer",)})
        with self.assertRaises(TypeError):
            home.extend_proxy_groups["BiliBili"] = ("HomeServer",)
        with self.assertRaises(FrozenInstanceError):
            home.inject_node_groups = ()

    def test_dump_is_deterministic_and_digest_is_stable(self):
        home = parse_home_overlay(home_document_bytes(), self.max_bytes)

        first = dump_home_overlay(home)
        second = dump_home_overlay(home)

        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        self.assertFalse(first.endswith(b"\n\n"))
        self.assertRegex(home_overlay_digest(home), r"^[0-9a-f]{64}$")
        self.assertEqual(
            home_overlay_digest(home),
            home_overlay_digest(parse_home_overlay(first, self.max_bytes)),
        )

    def test_injection_lists_must_be_disjoint_and_reference_private_groups(self):
        document = home_document()
        document["inject-node-groups"] = ["HomeServer"]
        document["inject-home-node-groups"] = ["HomeServer"]

        with self.assertRaises(HomeSourceError) as caught:
            parse_home_overlay(yaml.safe_dump(document).encode(), 5 * 1024 * 1024)

        self.assertEqual(caught.exception.code, "home_group_reference_invalid")

    def test_rejects_missing_and_unknown_top_level_keys(self):
        for key in home_document():
            with self.subTest(missing=key):
                self.assertDocumentCode(
                    lambda document, key=key: document.pop(key), "home_schema_invalid"
                )
        with self.subTest(unknown="extra"):
            self.assertDocumentCode(
                lambda document: document.update({"extra": []}), "home_schema_invalid"
            )

    def test_rejects_wrong_top_level_shapes_and_non_documents(self):
        for key, value in (
            ("proxies", "not-a-list"),
            ("proxy-groups", "not-a-list"),
            ("extend-proxy-groups", ["not-a-mapping"]),
            ("inject-node-groups", "not-a-list"),
            ("inject-home-node-groups", {"ProxyServer": 1}),
            ("rules", "not-a-list"),
        ):
            with self.subTest(key=key):
                self.assertDocumentCode(
                    lambda document, key=key, value=value: document.update({key: value}),
                    "home_schema_invalid",
                )
        self.assertHomeCode(b"[]\n", "home_schema_invalid")
        self.assertHomeCode(b"- one\n- two\n", "home_schema_invalid")
        self.assertHomeCode(b"null\n", "home_schema_invalid")

    def test_rejects_bad_injection_list_entries_as_schema_invalid(self):
        for key in ("inject-node-groups", "inject-home-node-groups"):
            with self.subTest(key=key):
                self.assertDocumentCode(
                    lambda document, key=key: document.update({key: [17]}),
                    "home_schema_invalid",
                )

    def test_rejects_empty_or_duplicated_proxy_and_group_sections(self):
        for key, code in (
            ("proxies", "home_proxy_invalid"),
            ("proxy-groups", "home_group_invalid"),
        ):
            with self.subTest(empty=key):
                self.assertDocumentCode(
                    lambda document, key=key: document.update({key: []}), code
                )
            with self.subTest(duplicated=key):
                self.assertDocumentCode(
                    lambda document, key=key: document[key].append(
                        dict(document[key][0])
                    ),
                    code,
                )
        self.assertDocumentCode(
            lambda document: document["proxies"].__setitem__(0, "not-a-mapping"),
            "home_proxy_invalid",
        )
        self.assertDocumentCode(
            lambda document: document["proxy-groups"].__setitem__(0, ["not-a-mapping"]),
            "home_group_invalid",
        )

    def test_rejects_extension_mappings_with_bad_values_or_missing_targets(self):
        for name, mutation, code in (
            (
                "string-value",
                lambda document: document.update(
                    {"extend-proxy-groups": {"BiliBili": "ProxyServer"}}
                ),
                "home_extension_invalid",
            ),
            (
                "empty-value",
                lambda document: document.update({"extend-proxy-groups": {"BiliBili": []}}),
                "home_extension_invalid",
            ),
            (
                "empty-key",
                lambda document: document.update({"extend-proxy-groups": {"": ["ProxyServer"]}}),
                "home_extension_invalid",
            ),
            (
                "missing-target",
                lambda document: document.update(
                    {"extend-proxy-groups": {"BiliBili": ["MissingGroup"]}}
                ),
                "home_group_reference_invalid",
            ),
        ):
            with self.subTest(name=name):
                self.assertDocumentCode(mutation, code)

    def test_rejects_injection_referencing_groups_missing_from_proxy_groups(self):
        for key in ("inject-node-groups", "inject-home-node-groups"):
            with self.subTest(key=key):
                self.assertDocumentCode(
                    lambda document, key=key: document.update({key: ["MissingGroup"]}),
                    "home_group_reference_invalid",
                )

    def test_rejects_duplicate_names_within_one_injection_list(self):
        for key, group in (
            ("inject-node-groups", "ProxyServer"),
            ("inject-home-node-groups", "HomeServer"),
        ):
            with self.subTest(key=key):
                self.assertDocumentCode(
                    lambda document, key=key, group=group: document.update(
                        {key: [group, group]}
                    ),
                    "home_group_reference_invalid",
                )

    def test_rejects_invalid_and_terminal_rules(self):
        for rule in (
            "MATCH,ProxyServer",
            "FINAL,HomeServer",
            "match,ProxyServer",
            "MATCH",
            "MATCH ,HomeServer",
            "final ,HomeServer",
            "match\t,HomeServer",
            "MATCH , HomeServer,no-resolve",
            "IP-CIDR,192.168.0.0/16,MissingGroup",
            "IP-CIDR",
            "",
        ):
            with self.subTest(rule=rule):
                self.assertDocumentCode(
                    lambda document, rule=rule: document.update(rules=[rule]),
                    "home_rule_invalid",
                )
        self.assertDocumentCode(
            lambda document: document.update(rules=[42]), "home_rule_invalid"
        )
        self.assertDocumentCode(
            lambda document: document.update(rules=[None]), "home_rule_invalid"
        )

    def test_rejects_unsafe_payload_bytes(self):
        for payload in (
            b"",
            b"\xff\xfebroken",
            b"proxies: [\n",
            b"{{ home_proxies }}\n",
            b"{% extends layout %}\n",
        ):
            with self.subTest(payload=payload):
                self.assertHomeCode(payload, "home_yaml_invalid")
        with self.assertRaises(HomeSourceError) as caught:
            parse_home_overlay(b"\xff\xfebroken", self.max_bytes)
        self.assertIsNone(caught.exception.__context__)
        payload = home_document_bytes()
        for bad_max_bytes in (0, len(payload) - 1, "64"):
            with self.subTest(max_bytes=bad_max_bytes):
                with self.assertRaises(HomeSourceError) as caught:
                    parse_home_overlay(payload, bad_max_bytes)
                self.assertEqual(caught.exception.code, "home_source_invalid")
        with self.assertRaises(HomeSourceError) as caught:
            parse_home_overlay(payload.decode("utf-8"), self.max_bytes)
        self.assertEqual(caught.exception.code, "home_source_invalid")

    def test_home_errors_never_echo_document_values_in_message_or_traceback(self):
        document = home_document()
        document["proxy-groups"].append(dict(document["proxy-groups"][0]))
        payload = yaml.safe_dump(document, allow_unicode=True).encode("utf-8")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(HomeSourceError) as caught:
                parse_home_overlay(payload, self.max_bytes)

        self.assertEqual(caught.exception.code, "home_group_invalid")
        rendered = "".join(traceback.format_exception(caught.exception))
        for secret in ("ProxyServer", "HomeExit", "synthetic-home-password", "192.168.0.0/16"):
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn(secret, repr(caught.exception))
            self.assertNotIn(secret, rendered)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_loads_a_single_link_owner_only_home_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_home_file(directory)

            home = load_home_overlay(path, self.max_bytes)

            self.assertEqual(home.inject_node_groups, ("ProxyServer",))
            self.assertEqual(home.inject_home_node_groups, ("HomeServer",))

    def test_rejects_symlinked_home_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_home_file(directory)
            link = Path(directory) / "linked-home.yaml"
            os.symlink(path, link)

            with self.assertRaises(HomeSourceError) as caught:
                load_home_overlay(link, self.max_bytes)

            self.assertEqual(caught.exception.code, "home_source_invalid")

    def test_rejects_hard_linked_home_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_home_file(directory)
            os.link(path, path.with_name("hard-home.yaml"))

            with self.assertRaises(HomeSourceError) as caught:
                load_home_overlay(path, self.max_bytes)

            self.assertEqual(caught.exception.code, "home_source_invalid")

    def test_rejects_home_overlay_owned_by_another_user(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_home_file(directory)
            if os.geteuid() == 0:
                os.chown(path, 1, -1)
                with self.assertRaises(HomeSourceError) as caught:
                    load_home_overlay(path, self.max_bytes)
                self.assertEqual(caught.exception.code, "home_source_invalid")
            else:
                with patch(
                    "clash_sub.sources.os.geteuid", return_value=os.geteuid() + 1
                ):
                    with self.assertRaises(HomeSourceError) as caught:
                        load_home_overlay(path, self.max_bytes)
                    self.assertEqual(caught.exception.code, "home_source_invalid")

    def test_rejects_home_overlay_with_shared_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_home_file(directory)
            os.chmod(path, 0o644)

            with self.assertRaises(HomeSourceError) as caught:
                load_home_overlay(path, self.max_bytes)

            self.assertEqual(caught.exception.code, "home_source_invalid")

    def test_rejects_home_overlay_larger_than_max_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_home_file(directory)

            with self.assertRaises(HomeSourceError) as caught:
                load_home_overlay(path, 16)

            self.assertEqual(caught.exception.code, "home_source_invalid")

    def test_rejects_missing_home_overlay_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HomeSourceError) as caught:
                load_home_overlay(Path(directory) / "absent.yaml", self.max_bytes)

            self.assertEqual(caught.exception.code, "home_source_invalid")
            self.assertIsNone(caught.exception.__context__)
            self.assertIsNone(caught.exception.__cause__)
