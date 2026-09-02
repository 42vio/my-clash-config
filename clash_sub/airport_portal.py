"""Pure-HTTP adapter for the airport subscription portal page.

The portal speaks a small, fixed protocol: GET the activation page (this
also enables the subscription), then POST form-encoded fields to the
GetSubscription entry, which answers either with the real subscription
URL or with a task id that resolves after the page-declared delay.  This
module keeps every portal-specific detail (HTML shape, form fields, JSON
envelope) inside itself; the rest of the code base only sees stable
error codes and a generated URL.

Nothing here executes remote JavaScript, persists cookies beyond one
activate/generate session, or lets page values (sid, token, pid, task
id, URLs) leak into error text.
"""

import json
import time
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urljoin, urlsplit
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
    opener (cookies live inside it), the origin every URL must match,
    the form fields taken from the verified button, and the page-declared
    wait.  Never persisted; the repr never shows field values.
    """

    def __init__(self, opener, origin, fields, delay_seconds):
        self._opener = opener
        self._origin = origin
        self._fields = fields
        self._delay_seconds = delay_seconds

    def __repr__(self):
        return "AirportPortalPage(<redacted>)"


class _ButtonParser(HTMLParser):
    """Collect the tag carrying the exact target button id, if any."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.matches = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value == BUTTON_ID:
                self.matches.append(list(attrs))
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
        entry = _join_origin(page._origin, GET_SUBSCRIPTION_PATH)
        answer = self._post(page, entry, dict(page._fields))
        if answer[0] == "subid":
            self._sleeper(page._delay_seconds)
            answer = self._post(page, entry, dict(page._fields, subid=answer[1]))
            if answer[0] != "url":
                # The task answer must resolve to a link, never a second task.
                raise AirportPortalError("airport_link_generation_failed")
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
            or document["result"] not in ("url", "subid")
            or not isinstance(document["msg"], str)
            or not document["msg"]
        ):
            raise AirportPortalError("airport_link_generation_failed")
        return document["result"], document["msg"]


def _parse_portal_page(body, origin):
    parser = _ButtonParser()
    try:
        parser.feed(body.decode("utf-8", errors="strict"))
    except UnicodeError:
        raise AirportPortalError("airport_portal_unsupported") from None
    if len(parser.matches) != 1:
        raise AirportPortalError("airport_portal_unsupported")
    attrs = {}
    for name, value in parser.matches[0]:
        if name in attrs:
            raise AirportPortalError("airport_portal_unsupported")
        attrs[name] = value
    action = attrs.get("data-action")
    subscription_type = attrs.get("data-type")
    sid = attrs.get("data-sid")
    token = attrs.get("data-token")
    pid = attrs.get("data-pid")
    delay_text = attrs.get("data-delay")
    if not _is_clash_entry(action, origin) or subscription_type != SUBSCRIPTION_TYPE:
        raise AirportPortalError("airport_portal_unsupported")
    if not _nonempty_text(sid) or not _nonempty_text(token) or not _nonempty_text(pid):
        raise AirportPortalError("airport_portal_unsupported")
    delay_seconds = _parse_delay(delay_text)
    if delay_seconds is None:
        raise AirportPortalError("airport_portal_unsupported")
    fields = {"sid": sid, "token": token, "pid": pid, "type": subscription_type}
    return fields, delay_seconds


def _is_clash_entry(action, origin):
    if not _nonempty_text(action):
        return False
    target = urlsplit(urljoin(_join_origin(origin, "/"), action))
    return (
        target.scheme == "https"
        and _same_origin((target.scheme, target.netloc), origin)
        and target.path == CLASH_ENTRY_PATH
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
