import contextlib
import copy
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request

from clash_sub.domain import Traffic
from clash_sub.sources import (
    SourceError,
    _HttpsRedirectHandler,
    download_airport_proxies,
    fetch_xui_proxies,
    load_proxy_snapshot,
    merge_proxy_sources,
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
