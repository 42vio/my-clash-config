"""Pure-HTTP adapter for the airport subscription portal page.

The portal protocol was verified against the live page with plain HTTP on
2026-09-02 (redacted structural capture: tests/fixtures/portal-page.html):

* GET ``/Subscription/index?sid=&token=`` returns the page and enables the
  subscription; the page declares ``delaytime``, ``pid``, ``sid`` and
  ``token`` in a script block and carries one target button whose onclick
  calls ``GetSubscription('<relative url>', 'Clash1_Anyttls')``.
* The button's relative URL points at the same-origin ``/Subscription/Clash``
  entry with ``t=anytls_clash`` and several opaque marker parameters; it
  travels **verbatim** inside the form's ``info`` field.
* POST ``/Subscription/GetSubscription?<random>`` with form fields
  ``sid/token/pid/info`` answers ``{"result": true, "msg": "subid:<uuid>"}``
  (or directly ``"url:<link>"``); after the page-declared wait the same POST
  is repeated with an extra ``subid`` field and answers
  ``{"result": true, "msg": "url:<link>"}``.  Failures answer
  ``{"result": false, "msg": "<human text>"}`` and the text never leaves
  this module.

Nothing here executes remote JavaScript, persists cookies beyond one
activate/generate session, or lets page values (sid, token, pid, task id,
URLs) leak into error text.
"""

import json
import random
import re
import time
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.parse import parse_qs, urljoin, urlsplit, urlencode
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

BUTTON_ID = "Clash1_Anyttls"
CLASH_ENTRY_PATH = "/Subscription/Clash"
GET_SUBSCRIPTION_PATH = "/Subscription/GetSubscription"
SUBSCRIPTION_TYPE = "anytls_clash"

_TIMEOUT_SECONDS = 15
_MAX_REDIRECTS = 3
_MAX_PAGE_BYTES = 1024 * 1024
_MAX_JSON_BYTES = 4096
_DELAY_MIN = 0
_DELAY_MAX = 30
# Every observed task id is a lowercase UUID; anything else is a malformed
# answer, never a task, and must fail before the second POST is sent.
_TASK_ID_PATTERN = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_ONCALL_PATTERN = re.compile(
    r"\AGetSubscription\('([^']*)','%s'\)\Z" % re.escape(BUTTON_ID)
)
_PAGE_VAR_PATTERN = re.compile(
    r"var\s+(delaytime|pid|sid|token)\s*=\s*(?:'([^']*)'|([0-9]+))\s*;"
)


class AirportPortalError(RuntimeError):
    """A redacted, stable portal failure."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _PortalRedirectHandler(HTTPRedirectHandler):
    """Allow at most three HTTPS redirects while fetching the page."""

    def __init__(self):
        super().__init__()
        self._redirects = 0

    def redirect_request(self, request, response, code, message, headers, new_url):
        self._redirects += 1
        parts = urlsplit(new_url)
        if (
            self._redirects > _MAX_REDIRECTS
            or parts.scheme != "https"
            or not parts.hostname
        ):
            raise AirportPortalError("airport_portal_unavailable")
        return super().redirect_request(
            request, response, code, message, headers, new_url
        )


class AirportPortalPage:
    """In-memory context of one activated portal page.

    Holds only what the follow-up generation call needs: the session
    opener (cookies live inside it), the origin every URL must match, and
    the form fields taken from the verified page.  Never persisted; the
    repr never shows field values.
    """

    def __init__(self, opener, origin, fields, delay_seconds):
        self._opener = opener
        self._origin = origin
        self._fields = fields
        self._delay_seconds = delay_seconds

    def __repr__(self):
        return "AirportPortalPage(<redacted>)"


class _ButtonParser(HTMLParser):
    """Collect the onclick attribute of the exact target button, if any."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.matches = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value == BUTTON_ID:
                self.matches.append(
                    next((entry for entry_name, entry in attrs if entry_name == "onclick"), None)
                )
                return


class AirportPortalClient:
    """Drive the portal protocol with strict, redacted validation."""

    def __init__(self, opener_factory=None, sleeper=None):
        if opener_factory is None:
            def opener_factory():
                return build_opener(
                    ProxyHandler({}),
                    HTTPCookieProcessor(CookieJar()),
                    _PortalRedirectHandler(),
                )
        self._opener_factory = opener_factory
        self._sleeper = sleeper if sleeper is not None else time.sleep

    def activate(self, activation_url) -> AirportPortalPage:
        """Fetch the activation page and verify its AnyTLS Clash button."""
        if not _valid_activation_url(activation_url):
            raise AirportPortalError("airport_activation_url_invalid")
        origin = _origin_of(activation_url)
        opener = self._opener_factory()
        try:
            request = Request(activation_url)
            with opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
                final_url = response.geturl()
                body = response.read(_MAX_PAGE_BYTES + 1)
        except AirportPortalError:
            raise
        except Exception:
            raise AirportPortalError("airport_portal_unavailable") from None
        if not _same_origin(_origin_of(final_url), origin) or len(body) > _MAX_PAGE_BYTES:
            raise AirportPortalError("airport_portal_unavailable")
        fields, delay_seconds = _parse_portal_page(body, origin)
        return AirportPortalPage(opener, origin, fields, delay_seconds)

    def generate_source_url(self, page) -> str:
        """Create (and if needed resolve) the real subscription URL."""
        entry = _join_origin(
            page._origin, "%s?%s" % (GET_SUBSCRIPTION_PATH, random.random())
        )
        answer = self._post(page, entry, dict(page._fields))
        if answer[0] == "subid":
            self._sleeper(page._delay_seconds)
            answer = self._post(page, entry, dict(page._fields, subid=answer[1]))
        return _validated_source_url(answer[1], page._origin)

    def _post(self, page, entry, form):
        try:
            request = Request(
                entry,
                data=urlencode(form).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with page._opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
                final_url = response.geturl()
                body = response.read(_MAX_JSON_BYTES + 1)
        except AirportPortalError:
            raise
        except Exception:
            raise AirportPortalError("airport_link_generation_failed") from None
        # The generation entry is fixed and same-origin: a redirect that
        # landed anywhere else (other host, or downgraded scheme) means the
        # answer did not come from the portal, whatever the body claims.
        if not _same_origin(_origin_of(final_url), page._origin):
            raise AirportPortalError("airport_link_generation_failed")
        if len(body) > _MAX_JSON_BYTES:
            raise AirportPortalError("airport_link_generation_failed")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise AirportPortalError("airport_link_generation_failed") from None
        if (
            not isinstance(document, dict)
            or set(document) != {"result", "msg"}
            or document["result"] is not True
            or not isinstance(document["msg"], str)
            or not document["msg"]
        ):
            raise AirportPortalError("airport_link_generation_failed")
        message = document["msg"]
        if message.startswith("url:"):
            return "url", message[len("url:"):]
        task = message[len("subid:"):] if message.startswith("subid:") else None
        if task is None or _TASK_ID_PATTERN.fullmatch(task) is None:
            raise AirportPortalError("airport_link_generation_failed")
        return "subid", task


def _parse_portal_page(body, origin):
    parser = _ButtonParser()
    try:
        parser.feed(body.decode("utf-8", errors="strict"))
    except UnicodeError:
        raise AirportPortalError("airport_portal_unsupported") from None
    if len(parser.matches) != 1 or parser.matches[0] is None:
        raise AirportPortalError("airport_portal_unsupported")
    match = _ONCALL_PATTERN.fullmatch(parser.matches[0])
    if match is None:
        raise AirportPortalError("airport_portal_unsupported")
    button_url = match.group(1)
    if not _is_clash_entry(button_url, origin):
        raise AirportPortalError("airport_portal_unsupported")
    variables = {}
    for name, text, number in _PAGE_VAR_PATTERN.findall(body.decode("utf-8", errors="strict")):
        if name in variables:
            raise AirportPortalError("airport_portal_unsupported")
        variables[name] = number if number else text
    if set(variables) != {"delaytime", "pid", "sid", "token"}:
        raise AirportPortalError("airport_portal_unsupported")
    if not _nonempty_text(variables["sid"]) or not _nonempty_text(variables["token"]) or not _nonempty_text(variables["pid"]):
        raise AirportPortalError("airport_portal_unsupported")
    delay_seconds = _parse_delay(variables["delaytime"])
    if delay_seconds is None:
        raise AirportPortalError("airport_portal_unsupported")
    fields = {
        "sid": variables["sid"],
        "token": variables["token"],
        "pid": variables["pid"],
        "info": button_url,
    }
    return fields, delay_seconds


def _is_clash_entry(button_url, origin):
    if not _nonempty_text(button_url):
        return False
    target = urlsplit(urljoin(_join_origin(origin, "/"), button_url))
    types = parse_qs(target.query).get("t", [])
    return (
        target.scheme == "https"
        and _same_origin((target.scheme, target.netloc), origin)
        and target.path == CLASH_ENTRY_PATH
        and not target.fragment
        and types == [SUBSCRIPTION_TYPE]
    )


def _parse_delay(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    if number < _DELAY_MIN or number > _DELAY_MAX:
        return None
    return number


def _nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def _validated_source_url(link, origin):
    if not isinstance(link, str):
        raise AirportPortalError("airport_link_generation_failed")
    try:
        parts = urlsplit(link)
    except ValueError:
        raise AirportPortalError("airport_link_generation_failed") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or not _same_origin(_origin_of(link), origin)
    ):
        raise AirportPortalError("airport_link_generation_failed")
    return link


def _valid_activation_url(url):
    try:
        parts = urlsplit(url)
        return (
            isinstance(url, str)
            and parts.scheme == "https"
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not parts.fragment
        )
    except (TypeError, ValueError):
        return False


def _origin_of(url):
    parts = urlsplit(url)
    return (parts.scheme.lower(), parts.netloc.lower())


def _same_origin(left, right):
    return left == right


def _join_origin(origin, path):
    return "%s://%s%s" % (origin[0], origin[1], path)
