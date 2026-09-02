"""Pure-HTTP airport portal adapter: page protocol, limits, and redaction.

All fixtures use placeholder hosts and placeholder dynamic values only.
"""

import email.message
import json
import unittest
import urllib.request
from urllib.parse import parse_qs, urlencode

from clash_sub.airport_portal import (
    _PortalRedirectHandler,
    AirportPortalClient,
    AirportPortalError,
)

ACTIVATION_URL = "https://portal.example/Subscription/index?sid=placeholder-sid&token=placeholder-token"
GET_SUBSCRIPTION_URL = "https://portal.example/Subscription/GetSubscription"
GENERATED_URL = "https://portal.example/generated-placeholder"

SID = "placeholder-sid"
TOKEN = "placeholder-token"
PID = "placeholder-pid"
DELAY = 8
TASK_ID = "placeholder-task-1"


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


def page_body(button=None):
    if button is None:
        button = clash_button()
    return (
        "<!doctype html><html><head><title>Subscription</title></head><body>"
        "<button id=\"Clash1_Vless\" type=\"button\">ignored sibling</button>"
        + button +
        "</body></html>"
    ).encode("utf-8")


def clash_button(**overrides):
    attributes = {
        "id": "Clash1_Anyttls",
        "type": "button",
        "data-action": "/Subscription/Clash",
        "data-type": "anytls_clash",
        "data-sid": SID,
        "data-token": TOKEN,
        "data-pid": PID,
        "data-delay": str(DELAY),
    }
    attributes.update(overrides)
    rendered = []
    for name, value in attributes.items():
        if value is None:
            continue
        rendered.append('%s="%s"' % (name, value))
    return "<button " + " ".join(rendered) + ">Clash</button>"


def page_response(button=None, url=ACTIVATION_URL):
    return FakeResponse(page_body(button), url)


def generation_payload(result, msg):
    return json.dumps({"result": result, "msg": msg}).encode("utf-8")


def generation_response(result, msg):
    return FakeResponse(generation_payload(result, msg), GET_SUBSCRIPTION_URL)


def make_client(*responses, sleeper=None):
    opener = FakeOpener(*responses)
    if sleeper is None:
        sleeper = lambda _seconds: None
    client = AirportPortalClient(opener_factory=lambda: opener, sleeper=sleeper)
    return client, opener


class HappyProtocolTests(unittest.TestCase):
    def test_first_post_returning_the_url_succeeds_without_sleeping(self):
        sleeps = []
        client, opener = make_client(
            page_response(),
            generation_response("url", GENERATED_URL),
            sleeper=sleeps.append,
        )

        page = client.activate(ACTIVATION_URL)
        source_url = client.generate_source_url(page)

        self.assertEqual(source_url, GENERATED_URL)
        self.assertEqual(sleeps, [])
        methods = [request[0] for request in opener.requests]
        self.assertEqual(methods, ["GET", "POST"])

    def test_task_flow_waits_the_page_delay_then_posts_the_task_id(self):
        sleeps = []
        client, opener = make_client(
            page_response(),
            generation_response("subid", TASK_ID),
            generation_response("url", GENERATED_URL),
            sleeper=sleeps.append,
        )

        page = client.activate(ACTIVATION_URL)
        source_url = client.generate_source_url(page)

        self.assertEqual(source_url, GENERATED_URL)
        self.assertEqual(sleeps, [DELAY])
        first_form = parse_qs(opener.requests[1][2].decode("utf-8"))
        second_form = parse_qs(opener.requests[2][2].decode("utf-8"))
        self.assertEqual(first_form.get("subid"), None)
        self.assertEqual(second_form.get("subid"), [TASK_ID])
        for form in (first_form, second_form):
            self.assertEqual(form.get("sid"), [SID])
            self.assertEqual(form.get("token"), [TOKEN])
            self.assertEqual(form.get("pid"), [PID])
            self.assertEqual(form.get("type"), ["anytls_clash"])

    def test_posts_target_the_same_origin_getsubscription_entry(self):
        client, opener = make_client(
            page_response(),
            generation_response("url", GENERATED_URL),
        )

        page = client.activate(ACTIVATION_URL)
        client.generate_source_url(page)

        self.assertEqual(opener.requests[0][1], ACTIVATION_URL)
        self.assertEqual(opener.requests[1][1], GET_SUBSCRIPTION_URL)
        self.assertEqual(opener.requests[1][2], urlencode(
            {"sid": SID, "token": TOKEN, "pid": PID, "type": "anytls_clash"}
        ).encode("utf-8"))

    def test_boundary_delays_are_accepted(self):
        for delay in (0, 30):
            with self.subTest(delay=delay):
                client, _ = make_client(
                    page_response(clash_button(**{"data-delay": str(delay)})),
                    generation_response("url", GENERATED_URL),
                    sleeper=lambda _seconds: None,
                )
                page = client.activate(ACTIVATION_URL)
                self.assertEqual(client.generate_source_url(page), GENERATED_URL)

    def test_absolute_same_origin_clash_action_is_accepted(self):
        client, _ = make_client(
            page_response(clash_button(
                **{"data-action": "https://portal.example/Subscription/Clash"}
            )),
            generation_response("url", GENERATED_URL),
        )
        page = client.activate(ACTIVATION_URL)
        self.assertEqual(client.generate_source_url(page), GENERATED_URL)

    def test_each_activation_uses_a_fresh_cookie_session(self):
        created = []

        def factory():
            opener = FakeOpener(
                page_response(),
                generation_response("url", GENERATED_URL),
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


class PageStructureTests(unittest.TestCase):
    def assert_unsupported(self, button_or_body):
        if isinstance(button_or_body, bytes):
            body = button_or_body
        else:
            body = page_body(button_or_body)
        client, _ = make_client(FakeResponse(body, ACTIVATION_URL))
        with self.assertRaises(AirportPortalError) as caught:
            client.activate(ACTIVATION_URL)
        self.assertEqual(caught.exception.code, "airport_portal_unsupported")
        self.assertEqual(str(caught.exception), "airport_portal_unsupported")

    def test_missing_button_is_unsupported(self):
        self.assert_unsupported(b"<html><body>no button here</body></html>")

    def test_duplicate_button_is_unsupported(self):
        self.assert_unsupported(page_body(clash_button()) + clash_button().encode("utf-8"))

    def test_wrong_clash_entry_or_subscription_type_is_unsupported(self):
        for overrides in (
            {"data-action": "/Other/Entry"},
            {"data-action": "https://other.example/Subscription/Clash"},
            {"data-type": "vless_clash"},
            {"data-type": None},
            {"data-action": None},
        ):
            with self.subTest(overrides=overrides):
                self.assert_unsupported(clash_button(**overrides))

    def test_missing_empty_or_mistyped_dynamic_fields_are_unsupported(self):
        for overrides in (
            {"data-sid": None},
            {"data-sid": ""},
            {"data-token": None},
            {"data-token": ""},
            {"data-pid": None},
            {"data-pid": ""},
            {"data-delay": None},
            {"data-delay": ""},
            {"data-delay": "abc"},
            {"data-delay": "8.5"},
            {"data-delay": "-1"},
            {"data-delay": "31"},
            {"data-delay": "999"},
        ):
            with self.subTest(overrides=overrides):
                self.assert_unsupported(clash_button(**overrides))

    def test_repeated_attribute_is_unsupported(self):
        button = (
            '<button id="Clash1_Anyttls" data-sid="%s" data-sid="%s" '
            'data-token="%s" data-pid="%s" data-delay="%d" '
            'data-action="/Subscription/Clash" data-type="anytls_clash">x</button>'
            % (SID, SID, TOKEN, PID, DELAY)
        )
        self.assert_unsupported(button)


class GenerationBoundaryTests(unittest.TestCase):
    def generate(self, *responses):
        client, opener = make_client(page_response(), *responses)
        page = client.activate(ACTIVATION_URL)
        return client, opener, page

    def test_oversized_json_is_a_generation_failure(self):
        client, _, page = self.generate(
            generation_response("url", "https://portal.example/" + "a" * 5000)
        )
        with self.assertRaises(AirportPortalError) as caught:
            client.generate_source_url(page)
        self.assertEqual(caught.exception.code, "airport_link_generation_failed")

    def test_malformed_json_payloads_are_generation_failures(self):
        payloads = (
            b"not json",
            b"[]",
            b"3",
            json.dumps({}).encode("utf-8"),
            json.dumps({"result": "url"}).encode("utf-8"),
            json.dumps({"result": "url", "msg": "x", "extra": 1}).encode("utf-8"),
            json.dumps({"result": "other", "msg": GENERATED_URL}).encode("utf-8"),
            json.dumps({"result": 1, "msg": GENERATED_URL}).encode("utf-8"),
            json.dumps({"result": "url", "msg": 1}).encode("utf-8"),
            json.dumps({"result": "url", "msg": ""}).encode("utf-8"),
            json.dumps({"result": "subid", "msg": ""}).encode("utf-8"),
        )
        for payload in payloads:
            with self.subTest(payload=payload[:24]):
                client, _, page = self.generate(FakeResponse(payload, GET_SUBSCRIPTION_URL))
                with self.assertRaises(AirportPortalError) as caught:
                    client.generate_source_url(page)
                self.assertEqual(caught.exception.code, "airport_link_generation_failed")
                self.assertNotIn("msg", str(caught.exception))

    def test_generated_link_must_be_https_same_origin_without_credentials(self):
        for link in (
            "http://portal.example/subscription",
            "https://other.example/subscription",
            "https://user@portal.example/subscription",
            "https://user:pass@portal.example/subscription",
            "https://portal.example/subscription#fragment",
        ):
            with self.subTest(link=link):
                client, _, page = self.generate(generation_response("url", link))
                with self.assertRaises(AirportPortalError) as caught:
                    client.generate_source_url(page)
                self.assertEqual(caught.exception.code, "airport_link_generation_failed")

    def test_second_task_answer_instead_of_a_link_is_a_generation_failure(self):
        sleeps = []
        opener = FakeOpener(
            page_response(),
            generation_response("subid", TASK_ID),
            generation_response("subid", TASK_ID),
        )
        client = AirportPortalClient(opener_factory=lambda: opener, sleeper=sleeps.append)
        page = client.activate(ACTIVATION_URL)

        with self.assertRaises(AirportPortalError) as caught:
            client.generate_source_url(page)
        self.assertEqual(caught.exception.code, "airport_link_generation_failed")
        self.assertEqual(sleeps, [DELAY])

    def test_second_answer_must_be_a_url_even_when_the_msg_shaped_like_one(self):
        # A second "subid" answer is a protocol anomaly: it must fail even
        # when its message happens to be a valid same-origin link.
        client, _, page = self.generate(
            generation_response("subid", TASK_ID),
            generation_response("subid", GENERATED_URL),
        )
        with self.assertRaises(AirportPortalError) as caught:
            client.generate_source_url(page)
        self.assertEqual(caught.exception.code, "airport_link_generation_failed")

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
        client2, _ = make_client(FakeResponse(page_body(), ACTIVATION_URL))
        page = client2.activate(ACTIVATION_URL)
        client3, _ = make_client(
            page_response(), generation_response("url", "http://portal.example/leak")
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
                           "/Subscription/index", "/Subscription/Clash"):
                self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
