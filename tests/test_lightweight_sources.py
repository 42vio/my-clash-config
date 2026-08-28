import contextlib
import copy
import io
import os
import stat
import tempfile
import traceback
import unittest
from unittest.mock import patch
from pathlib import Path
import urllib.request
from urllib.request import Request

from clash_sub.domain import Traffic
from clash_sub.sources import (
    SourceError,
    XUI_INBOUND_PORT,
    _HttpsRedirectHandler,
    download_airport_proxies,
    fetch_xui_proxies,
    load_proxy_snapshot,
    merge_proxy_sources,
    normalize_xui_endpoints,
    parse_subscription_userinfo,
    write_proxy_snapshot,
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
            download_airport_proxies(
                "https://airport.example/private-token", 1024, opener=self.opener_for(response)
            )
        with self.assertRaises(SourceError):
            download_airport_proxies(
                "http://airport.example/private-token", 1024, opener=self.opener_for(response)
            )

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
                download_airport_proxies(secret, 1024, opener=self.failing_opener)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(caught.exception))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_airport_error_never_echoes_url_in_its_traceback(self):
        secret = "https://airport.example/private-five-minute-token"

        def failing_opener(request, timeout):
            raise OSError(secret)

        with self.assertRaises(SourceError) as caught:
            download_airport_proxies(secret, 1024, opener=failing_opener)

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
            proxies = download_airport_proxies("https://airport.example/private-token", 1024)

        self.assertEqual(proxies[0]["name"], "Example")
        builder.assert_called_once()
        self.assertEqual(len(captured["handlers"]), 2)
        self.assertIsInstance(captured["handlers"][0], urllib.request.ProxyHandler)
        self.assertEqual(captured["handlers"][0].proxies, {})
        self.assertIsInstance(captured["handlers"][1], _HttpsRedirectHandler)
        self.assertEqual(captured["timeout"], 15)


class SnapshotAndMergeTests(unittest.TestCase):
    def test_snapshot_is_root_only_atomic_yaml_without_a_source_url(self):
        secret = "https://airport.example/private-five-minute-token"
        proxies = [{"name": "Airport", "type": "ss", "server": "example.invalid"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "airport.yaml"
            write_proxy_snapshot(path, proxies)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_proxy_snapshot(path), proxies)
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_snapshot_loads_a_regular_single_link_file_with_the_expected_owner(self):
        proxies = [{"name": "Home", "type": "ss", "server": "example.invalid"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "home.yaml"
            write_proxy_snapshot(path, proxies)

            self.assertEqual(load_proxy_snapshot(path), proxies)

    def test_snapshot_rejects_a_file_owned_by_someone_other_than_the_effective_owner(self):
        proxies = [{"name": "Home", "type": "ss", "server": "example.invalid"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "home.yaml"
            write_proxy_snapshot(path, proxies)
            if os.geteuid() == 0:
                os.chown(path, 1, -1)
                with self.assertRaises(SourceError):
                    load_proxy_snapshot(path)
            else:
                with patch("clash_sub.sources.os.geteuid", return_value=os.geteuid() + 1):
                    with self.assertRaises(SourceError):
                        load_proxy_snapshot(path)

    def test_snapshot_rejects_a_hard_linked_file(self):
        proxies = [{"name": "Home", "type": "ss", "server": "example.invalid"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "home.yaml"
            write_proxy_snapshot(path, proxies)
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
