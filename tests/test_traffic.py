import unittest
from urllib.error import URLError

from clash_sub.models import SubscriptionUserinfo
from clash_sub.traffic import TrafficError, TrafficClient, parse_subscription_userinfo


class HeaderOnlyResponse:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.closed = False
        self.read_calls = 0

    def read(self, size=-1):
        self.read_calls += 1
        return b"body should not be read"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False


class HeaderOnlyOpener:
    def __init__(self, *, response=None, error=None):
        self.response = response or HeaderOnlyResponse()
        self.error = error
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class TrafficTests(unittest.TestCase):
    def test_valid_header_is_canonicalized(self):
        value = "download=20; upload=10; expire=1893456000; total=100"

        info = parse_subscription_userinfo(value)

        self.assertEqual(info.remaining, 70)
        self.assertEqual(
            info.header_value,
            "upload=10; download=20; total=100; expire=1893456000",
        )

    def test_unlimited_total_has_no_numeric_remaining(self):
        info = parse_subscription_userinfo("upload=10; download=20; total=0; expire=0")

        self.assertIsNone(info.remaining)

    def test_parse_requires_all_fields(self):
        with self.assertRaisesRegex(TrafficError, "incomplete"):
            parse_subscription_userinfo("upload=10; download=20; total=100")

    def test_parse_rejects_unknown_fields(self):
        with self.assertRaisesRegex(TrafficError, "unknown"):
            parse_subscription_userinfo("upload=10; download=20; total=100; expire=1; extra=9")

    def test_parse_rejects_duplicate_fields(self):
        with self.assertRaisesRegex(TrafficError, "invalid"):
            parse_subscription_userinfo("upload=10; download=20; total=100; expire=1; upload=11")

    def test_parse_rejects_negative_values(self):
        with self.assertRaisesRegex(TrafficError, "non-negative"):
            parse_subscription_userinfo("upload=-1; download=20; total=100; expire=1")

    def test_parse_rejects_non_integer_values(self):
        with self.assertRaisesRegex(TrafficError, "non-negative"):
            parse_subscription_userinfo("upload=1.5; download=20; total=100; expire=1")

    def test_fetch_returns_none_when_header_absent(self):
        response = HeaderOnlyResponse(headers={})
        client = TrafficClient(opener=HeaderOnlyOpener(response=response))

        info = client.fetch("http://127.0.0.1:2096/sub/example")

        self.assertIsNone(info)
        self.assertEqual(response.read_calls, 0)
        self.assertTrue(response.closed)

    def test_fetch_reads_only_headers_and_closes_response(self):
        response = HeaderOnlyResponse(
            headers={"Subscription-Userinfo": "upload=1; download=2; total=10; expire=3"}
        )
        opener = HeaderOnlyOpener(response=response)
        client = TrafficClient(opener=opener)

        info = client.fetch("http://127.0.0.1:2096/sub/example")

        self.assertIsInstance(info, SubscriptionUserinfo)
        self.assertEqual(opener.calls[0][1], 10)
        self.assertEqual(response.read_calls, 0)
        self.assertTrue(response.closed)

    def test_fetch_propagates_timeout_value(self):
        response = HeaderOnlyResponse(
            headers={"Subscription-Userinfo": "upload=1; download=2; total=10; expire=3"}
        )
        opener = HeaderOnlyOpener(response=response)
        client = TrafficClient(opener=opener, timeout=7)

        client.fetch("http://127.0.0.1:2096/sub/example")

        self.assertEqual(opener.calls[0][1], 7)

    def test_fetch_rejects_over_limit_header_without_echoing_source_url(self):
        source_url = "http://127.0.0.1:2096/sub/private-value"
        oversized_number = "9" * 600
        oversized_header = (
            "upload=%s; download=2; total=%s; expire=4"
            % (oversized_number, oversized_number)
        )
        response = HeaderOnlyResponse(headers={"Subscription-Userinfo": oversized_header})
        client = TrafficClient(opener=HeaderOnlyOpener(response=response))

        with self.assertRaisesRegex(TrafficError, "size limit") as context:
            client.fetch(source_url)

        self.assertTrue(response.closed)
        self.assertEqual(response.read_calls, 0)
        self.assertNotIn(source_url, str(context.exception))
        self.assertNotIn(oversized_number, str(context.exception))

    def test_fetch_errors_do_not_echo_source_url(self):
        source_url = "http://127.0.0.1:2096/sub/private-value"
        client = TrafficClient(opener=HeaderOnlyOpener(error=URLError("boom")))

        with self.assertRaisesRegex(TrafficError, "fetch failed") as context:
            client.fetch(source_url)

        self.assertNotIn(source_url, str(context.exception))
