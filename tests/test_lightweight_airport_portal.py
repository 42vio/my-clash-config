"""Pure-HTTP airport portal adapter against the real portal protocol.

The page and wire fixtures mirror the redacted structural capture in
``tests/fixtures/portal-page.html`` (real page fetched 2026-09-02 with plain
HTTP; every dynamic value is a placeholder).  All hosts are placeholders.
"""

import email.message
import json
import unittest
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from clash_sub.airport_portal import (
    _PortalRedirectHandler,
    AirportPortalClient,
    AirportPortalError,
)

FIXTURE_PAGE = Path(__file__).resolve().parent / "fixtures" / "portal-page.html"

ACTIVATION_URL = "https://portal.example/Subscription/index?sid=placeholder-sid&token=placeholder-token"
GENERATED_URL = "https://portal.example/subscription-placeholder"
SID = "placeholder-sid"
TOKEN = "placeholder-token"
PID = "placeholder-pid-0000-4000-8000-000000000000"
DELAY = 8
# Task ids are real lowercase UUIDs on the wire; the value is generated at
# runtime so no well-formed UUID literal ever sits in the tracked sources.
TASK_ID = str(uuid.uuid4())
# The real button URL travels verbatim inside the form's "info" field,
# mm/ktmm/random-marker parameters included.
BUTTON_URL = (
    "/Subscription/Clash?t=anytls_clash&sid=placeholder-sid"
    "&token=placeholder-token&mm=97045&ktmm=placeholder-base64%3d%3d"
    "&placeholder-random-marker"
)

DEFAULT_SCRIPT = (
    "var IsCheckIpProxy = '0';\n"
    "var delaytime = 8;\n"
    "var pid = '%s';\n"
    "var sid = '%s';\n"
    "var token='%s';\n" % (PID, SID, TOKEN)
)
DEFAULT_BUTTON = (
    "<input type=\"button\" class=\"forconfigbtn\" value=\"Clash Subscription Anyttls\" "
    "onclick=\"GetSubscription('%s','Clash1_Anyttls')\" id=\"Clash1_Anyttls\">" % BUTTON_URL
)


class FakeResponse:
    def __init__(self, body, url):
        self._body = body
        self._url = url

    def read(self, limit=-1):
        return self._body[:limit]

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class FakeOpener:
    """Records (method, url, form body) and replays canned responses."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request.get_method(), request.full_url, request.data))
        return self._responses.pop(0)


def portal_body(button=None, script=None):
    if button is None:
        button = DEFAULT_BUTTON
    if script is None:
        script = DEFAULT_SCRIPT
    return (
        "<!DOCTYPE html><html><head><title>Subscription</title></head><body>"
        "<input type=\"button\" class=\"forconfigbtn\" value=\"ignored sibling\" "
        "onclick=\"GetSubscription('/Subscription/QuantumultX?sid=x&t=anytls_qx&token=y',"
        "'QuantumultX_Anytls')\" id=\"QuantumultX_Anytls\">"
        + button +
        "<script>" + script + "</script>"
        "</body></html>"
    ).encode("utf-8")


def page_response(button=None, script=None, url=ACTIVATION_URL):
    return FakeResponse(portal_body(button, script), url)


def generation_payload(result, msg):
    return json.dumps({"result": result, "msg": msg}).encode("utf-8")


def generation_response(result, msg):
    return FakeResponse(generation_payload(result, msg), entry_url())


def entry_url():
    return "https://portal.example/Subscription/GetSubscription?0.424242"


def make_client(*responses, sleeper=None):
    opener = FakeOpener(*responses)
    if sleeper is None:
        sleeper = lambda _seconds: None
    client = AirportPortalClient(opener_factory=lambda: opener, sleeper=sleeper)
    return client, opener


def post_forms(opener):
    return [parse_qs(request[2].decode("utf-8")) for request in opener.requests[1:]]


def post_paths(opener):
    return [urlsplit(request[1]).path for request in opener.requests[1:]]


class HappyProtocolTests(unittest.TestCase):
    def test_uses_the_redacted_structural_fixture_page(self):
        # The fixture is the redacted capture of the real page: activating it
        # must work with zero code-level tailoring.
        client, opener = FakeOpener(
            FakeResponse(FIXTURE_PAGE.read_bytes(), ACTIVATION_URL),
            generation_response(True, "url:%s" % GENERATED_URL),
        ), None
        portal = AirportPortalClient(opener_factory=lambda: client, sleeper=lambda _s: None)
        page = portal.activate(ACTIVATION_URL)
        self.assertEqual(portal.generate_source_url(page), GENERATED_URL)

    def test_first_answer_url_resolves_without_waiting(self):
        sleeps = []
        client, opener = make_client(
            page_response(),
            generation_response(True, "url:%s" % GENERATED_URL),
            sleeper=sleeps.append,
        )

        page = client.activate(ACTIVATION_URL)
        source_url = client.generate_source_url(page)

        self.assertEqual(source_url, GENERATED_URL)
        self.assertEqual(sleeps, [])
        self.assertEqual([request[0] for request in opener.requests], ["GET", "POST"])

    def test_task_flow_waits_the_page_delay_then_posts_the_task_id(self):
        sleeps = []
        client, opener = make_client(
            page_response(),
            generation_response(True, "subid:%s" % TASK_ID),
            generation_response(True, "url:%s" % GENERATED_URL),
            sleeper=sleeps.append,
        )

        page = client.activate(ACTIVATION_URL)
        source_url = client.generate_source_url(page)

        self.assertEqual(source_url, GENERATED_URL)
        self.assertEqual(sleeps, [DELAY])
        first, second = post_forms(opener)
        self.assertNotIn("subid", first)
        self.assertEqual(second["subid"], [TASK_ID])
        for form in (first, second):
            self.assertEqual(form["sid"], [SID])
            self.assertEqual(form["token"], [TOKEN])
            self.assertEqual(form["pid"], [PID])
            # The whole button URL rides along verbatim, marker parameters
            # included: nothing from the page is dropped or re-encoded.
            self.assertEqual(form["info"], [BUTTON_URL])
        self.assertEqual(post_paths(opener), ["/Subscription/GetSubscription"] * 2)
        for request in opener.requests[1:]:
            self.assertTrue(urlsplit(request[1]).query, "cache-buster query required")

    def test_boundary_delays_are_accepted(self):
        for delay in (0, 30):
            with self.subTest(delay=delay):
                client, _ = make_client(
                    page_response(
                        script=DEFAULT_SCRIPT.replace("var delaytime = 8;", "var delaytime = %d;" % delay)
                    ),
                    generation_response(True, "url:%s" % GENERATED_URL),
                )
                page = client.activate(ACTIVATION_URL)
                self.assertEqual(client.generate_source_url(page), GENERATED_URL)

    def test_each_activation_uses_a_fresh_cookie_session(self):
        created = []

        def factory():
            opener = FakeOpener(
                page_response(),
                generation_response(True, "url:%s" % GENERATED_URL),
            )
            created.append(opener)
            return opener

        client = AirportPortalClient(opener_factory=factory, sleeper=lambda _s: None)
        first_page = client.activate(ACTIVATION_URL)
        client.generate_source_url(first_page)
        second_page = client.activate(ACTIVATION_URL)
        client.generate_source_url(second_page)

        self.assertIsNot(created[0], created[1])
        self.assertEqual(len(created[0].requests), 2)
        self.assertEqual(len(created[1].requests), 2)


class ActivationUrlTests(unittest.TestCase):
    def test_non_https_userinfo_or_fragment_urls_are_rejected(self):
        for url in (
            "http://portal.example/Subscription/index",
            "ftp://portal.example/Subscription/index",
            "https://user@portal.example/Subscription/index",
            "https://user:pass@portal.example/Subscription/index",
            "https://portal.example/Subscription/index#fragment",
        ):
            with self.subTest(url=url.split("://", 1)[1][:24]):
                client, opener = make_client(page_response())
                with self.assertRaises(AirportPortalError) as caught:
                    client.activate(url)
                self.assertEqual(caught.exception.code, "airport_activation_url_invalid")
                self.assertEqual(str(caught.exception), "airport_activation_url_invalid")
                self.assertEqual(opener.requests, [])


class PageBoundaryTests(unittest.TestCase):
    def test_final_page_on_another_origin_or_scheme_is_unavailable(self):
        for final_url in (
            "http://portal.example/Subscription/index",
            "https://other.example/Subscription/index",
        ):
            with self.subTest(final_url=final_url):
                client, _ = make_client(page_response(url=final_url))
                with self.assertRaises(AirportPortalError) as caught:
                    client.activate(ACTIVATION_URL)
                self.assertEqual(caught.exception.code, "airport_portal_unavailable")

    def test_oversized_page_is_unavailable(self):
        oversized = FakeResponse(b"<html>" + b"x" * (1024 * 1024), ACTIVATION_URL)
        client, _ = make_client(oversized)
        with self.assertRaises(AirportPortalError) as caught:
            client.activate(ACTIVATION_URL)
        self.assertEqual(caught.exception.code, "airport_portal_unavailable")

    def test_get_network_failure_is_unavailable(self):
        class ExplodingOpener:
            def open(self, request, timeout=None):
                raise OSError("private transport detail")

        client = AirportPortalClient(
            opener_factory=ExplodingOpener, sleeper=lambda _s: None
        )
        with self.assertRaises(AirportPortalError) as caught:
            client.activate(ACTIVATION_URL)
        self.assertEqual(caught.exception.code, "airport_portal_unavailable")
        self.assertNotIn("private transport detail", str(caught.exception))

    def test_unparseable_final_page_address_is_a_stable_unavailable(self):
        # A final address urlsplit() cannot even parse must map onto the
        # stable code instead of leaking a raw ValueError.
        client, _ = make_client(FakeResponse(portal_body(), "https://[unparseable"))
        with self.assertRaises(AirportPortalError) as caught:
            client.activate(ACTIVATION_URL)
        self.assertEqual(caught.exception.code, "airport_portal_unavailable")
        self.assertEqual(str(caught.exception), "airport_portal_unavailable")


class PageStructureTests(unittest.TestCase):
    def assert_unsupported(self, body):
        client, _ = make_client(FakeResponse(body, ACTIVATION_URL))
        with self.assertRaises(AirportPortalError) as caught:
            client.activate(ACTIVATION_URL)
        self.assertEqual(caught.exception.code, "airport_portal_unsupported")
        self.assertEqual(str(caught.exception), "airport_portal_unsupported")

    def test_missing_target_button_is_unsupported(self):
        self.assert_unsupported(b"<html><body>no target button</body></html>")

    def test_duplicate_target_button_is_unsupported(self):
        self.assert_unsupported(
            portal_body(button=DEFAULT_BUTTON + DEFAULT_BUTTON)
        )

    def test_malformed_onclick_calls_are_unsupported(self):
        for bad in (
            "alert('not the generator')",
            "GetSubscription('%s')" % BUTTON_URL,
            "GetSubscription('%s','OtherId')" % BUTTON_URL,
            "GetSubscription('%s','Clash1_Anyttls','extra')" % BUTTON_URL,
            "GetSubscription('%s','Clash1_Anyttls')" % (BUTTON_URL + "'"),
        ):
            with self.subTest(bad=bad[:40]):
                self.assert_unsupported(
                    portal_body(
                        button="<input type=\"button\" id=\"Clash1_Anyttls\" onclick=\"%s\">" % bad
                    )
                )

    def test_wrong_clash_entry_or_subscription_type_is_unsupported(self):
        for url in (
            "/Subscription/Other?t=anytls_clash&sid=s&token=t",
            "/Subscription/Clash?t=anytls_qx&sid=s&token=t",
            "/Subscription/Clash?sid=s&token=t",
            "/Subscription/Clash?t=anytls_clash&t=anytls_clash&sid=s&token=t",
            "https://other.example/Subscription/Clash?t=anytls_clash&sid=s&token=t",
            "/Subscription/Clash?t=anytls_clash&sid=s&token=t#fragment",
            "https://[unparseable/Subscription/Clash?t=anytls_clash&sid=s&token=t",
        ):
            with self.subTest(url=url[:44]):
                self.assert_unsupported(
                    portal_body(
                        button="<input id=\"Clash1_Anyttls\" "
                               "onclick=\"GetSubscription('%s','Clash1_Anyttls')\">" % url
                    )
                )

    def test_missing_empty_or_duplicated_page_variables_are_unsupported(self):
        for script in (
            DEFAULT_SCRIPT.replace("var pid = '%s';\n" % PID, ""),
            DEFAULT_SCRIPT.replace("var sid = '%s';\n" % SID, ""),
            DEFAULT_SCRIPT.replace("var token='%s';\n" % TOKEN, ""),
            DEFAULT_SCRIPT.replace("var delaytime = 8;\n", ""),
            DEFAULT_SCRIPT.replace("var pid = '%s';" % PID, "var pid = '';"),
            DEFAULT_SCRIPT.replace("var sid = '%s';" % SID, "var sid = '';"),
            DEFAULT_SCRIPT.replace("var token='%s';" % TOKEN, "var token='';"),
            DEFAULT_SCRIPT + "var sid = 'duplicate';\n",
            DEFAULT_SCRIPT.replace("var delaytime = 8;", "var delaytime = 'eight';"),
            DEFAULT_SCRIPT.replace("var delaytime = 8;", "var delaytime = -1;"),
            DEFAULT_SCRIPT.replace("var delaytime = 8;", "var delaytime = 31;"),
            DEFAULT_SCRIPT.replace("var delaytime = 8;", "var delaytime = 8.5;"),
        ):
            with self.subTest(script=script[:40]):
                self.assert_unsupported(portal_body(script=script))


class GenerationBoundaryTests(unittest.TestCase):
    def generate(self, *responses):
        client, opener = make_client(page_response(), *responses)
        page = client.activate(ACTIVATION_URL)
        return client, opener, page

    def test_generation_response_must_stay_on_the_same_https_origin(self):
        # A cross-origin or downgraded final response address is rejected
        # even when the JSON body itself names a same-origin link.
        for final_url in (
            "https://evil.example/Subscription/GetSubscription",
            "http://portal.example/Subscription/GetSubscription",
            "https://portal.example.evil.example/Subscription/GetSubscription",
        ):
            with self.subTest(final_url=final_url):
                client, _, page = self.generate(
                    FakeResponse(generation_payload(True, "url:%s" % GENERATED_URL), final_url)
                )
                with self.assertRaises(AirportPortalError) as caught:
                    client.generate_source_url(page)
                self.assertEqual(caught.exception.code, "airport_link_generation_failed")

    def test_task_resolution_response_must_stay_on_the_same_https_origin(self):
        sleeps = []
        opener = FakeOpener(
            page_response(),
            generation_response(True, "subid:%s" % TASK_ID),
            FakeResponse(
                generation_payload(True, "url:%s" % GENERATED_URL),
                "https://evil.example/Subscription/GetSubscription",
            ),
        )
        client = AirportPortalClient(opener_factory=lambda: opener, sleeper=sleeps.append)
        page = client.activate(ACTIVATION_URL)

        with self.assertRaises(AirportPortalError) as caught:
            client.generate_source_url(page)
        self.assertEqual(caught.exception.code, "airport_link_generation_failed")
        self.assertEqual(sleeps, [DELAY])

    def test_oversized_json_is_a_generation_failure(self):
        client, _, page = self.generate(
            generation_response(True, "url:https://portal.example/" + "a" * 5000)
        )
        with self.assertRaises(AirportPortalError) as caught:
            client.generate_source_url(page)
        self.assertEqual(caught.exception.code, "airport_link_generation_failed")

    def test_unparseable_generation_address_is_a_stable_failure(self):
        client, _, page = self.generate(
            FakeResponse(generation_payload(True, "url:%s" % GENERATED_URL), "https://[unparseable")
        )
        with self.assertRaises(AirportPortalError) as caught:
            client.generate_source_url(page)
        self.assertEqual(caught.exception.code, "airport_link_generation_failed")
        self.assertEqual(str(caught.exception), "airport_link_generation_failed")

    def test_malformed_envelopes_are_generation_failures(self):
        payloads = (
            b"not json",
            b"[]",
            b"3",
            json.dumps({}).encode("utf-8"),
            json.dumps({"result": True}).encode("utf-8"),
            json.dumps({"result": True, "msg": "url:x", "extra": 1}).encode("utf-8"),
            json.dumps({"result": "true", "msg": "url:%s" % GENERATED_URL}).encode("utf-8"),
            json.dumps({"result": 1, "msg": "url:%s" % GENERATED_URL}).encode("utf-8"),
            json.dumps({"result": False, "msg": "参数不全"}).encode("utf-8"),
            json.dumps({"result": True, "msg": 1}).encode("utf-8"),
            json.dumps({"result": True, "msg": ""}).encode("utf-8"),
            json.dumps({"result": True, "msg": "参数不全"}).encode("utf-8"),
        )
        for payload in payloads:
            with self.subTest(payload=payload[:24]):
                client, _, page = self.generate(FakeResponse(payload, entry_url()))
                with self.assertRaises(AirportPortalError) as caught:
                    client.generate_source_url(page)
                self.assertEqual(caught.exception.code, "airport_link_generation_failed")
                self.assertNotIn("参数", str(caught.exception))

    def test_generated_link_must_be_https_same_origin_without_credentials(self):
        for link in (
            "http://portal.example/subscription",
            "https://other.example/subscription",
            "https://user@portal.example/subscription",
            "https://user:pass@portal.example/subscription",
            "https://portal.example/subscription#fragment",
        ):
            with self.subTest(link=link):
                client, _, page = self.generate(generation_response(True, "url:%s" % link))
                with self.assertRaises(AirportPortalError) as caught:
                    client.generate_source_url(page)
                self.assertEqual(caught.exception.code, "airport_link_generation_failed")

    def test_task_ids_must_be_lowercase_uuids_before_the_second_post(self):
        # The real portal answers with a lowercase UUID task id; anything
        # else is a malformed answer and must fail before the wait or the
        # second POST happen.
        for task in (
            " ",
            TASK_ID.upper(),
            "not-a-uuid",
            "a" * 129,
            "https://portal.example/task",
            "task/../../escape",
            "ta sk",
            "ta\x00sk",
            "ta\nsk",
            "id=1&x=2",
            TASK_ID + "0",
            "任务编号",
        ):
            with self.subTest(task=task[:24]):
                sleeps = []
                opener = FakeOpener(
                    page_response(), generation_response(True, "subid:%s" % task)
                )
                client = AirportPortalClient(
                    opener_factory=lambda: opener, sleeper=sleeps.append
                )
                page = client.activate(ACTIVATION_URL)

                with self.assertRaises(AirportPortalError) as caught:
                    client.generate_source_url(page)

                self.assertEqual(
                    caught.exception.code, "airport_link_generation_failed"
                )
                self.assertEqual(sleeps, [])
                self.assertEqual(
                    [request[0] for request in opener.requests], ["GET", "POST"]
                )
                if task.strip():
                    self.assertNotIn(task.strip()[:8], str(caught.exception))

    def test_second_task_answer_instead_of_a_link_is_a_generation_failure(self):
        sleeps = []
        opener = FakeOpener(
            page_response(),
            generation_response(True, "subid:%s" % TASK_ID),
            generation_response(True, "subid:%s" % TASK_ID),
        )
        client = AirportPortalClient(opener_factory=lambda: opener, sleeper=sleeps.append)
        page = client.activate(ACTIVATION_URL)

        with self.assertRaises(AirportPortalError) as caught:
            client.generate_source_url(page)
        self.assertEqual(caught.exception.code, "airport_link_generation_failed")
        self.assertEqual(sleeps, [DELAY])

    def test_post_network_failure_is_a_generation_failure(self):
        class ExplodingOpener:
            def open(self, request, timeout=None):
                if request.get_method() == "GET":
                    return page_response()
                raise OSError("private transport detail")

        client = AirportPortalClient(
            opener_factory=ExplodingOpener, sleeper=lambda _s: None
        )
        page = client.activate(ACTIVATION_URL)
        with self.assertRaises(AirportPortalError) as caught:
            client.generate_source_url(page)
        self.assertEqual(caught.exception.code, "airport_link_generation_failed")
        self.assertNotIn("private transport detail", str(caught.exception))


class RedirectHandlerTests(unittest.TestCase):
    def test_more_than_three_redirects_are_unavailable(self):
        handler = _PortalRedirectHandler()
        headers = email.message.Message()
        request = urllib.request.Request(ACTIVATION_URL)
        with self.assertRaises(AirportPortalError) as caught:
            for _ in range(4):
                handler.redirect_request(
                    request, None, 302, "Found", headers,
                    "https://portal.example/Subscription/next",
                )
        self.assertEqual(caught.exception.code, "airport_portal_unavailable")

    def test_non_https_redirect_target_is_unavailable(self):
        handler = _PortalRedirectHandler()
        headers = email.message.Message()
        request = urllib.request.Request(ACTIVATION_URL)
        with self.assertRaises(AirportPortalError) as caught:
            handler.redirect_request(
                request, None, 302, "Found", headers,
                "http://portal.example/Subscription/next",
            )
        self.assertEqual(caught.exception.code, "airport_portal_unavailable")


class RedactionTests(unittest.TestCase):
    def test_errors_and_page_context_never_expose_private_values(self):
        client, _ = make_client(page_response())
        with self.assertRaises(AirportPortalError) as unsupported:
            client.activate("http://portal.example/Subscription/index")
        client2, _ = make_client(FakeResponse(portal_body(), ACTIVATION_URL))
        page = client2.activate(ACTIVATION_URL)
        client3, _ = make_client(
            page_response(), generation_response(True, "url:http://portal.example/leak")
        )
        page3 = client3.activate(ACTIVATION_URL)
        with self.assertRaises(AirportPortalError) as invalid_link:
            client3.generate_source_url(page3)

        texts = [
            str(unsupported.exception),
            str(invalid_link.exception),
            repr(page),
            repr(page3),
        ]
        for text in texts:
            for secret in (SID, TOKEN, PID, TASK_ID, GENERATED_URL,
                           "/Subscription/index", "/Subscription/Clash",
                           "placeholder-random-marker"):
                self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
