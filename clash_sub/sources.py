import copy
import os
import stat
import urllib.request
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request

import yaml

from clash_sub.domain import AirportDownload, Traffic


class SourceError(RuntimeError):
    """Raised when a proxy source cannot be safely used."""


_SOURCE_ERROR = "proxy source rejected"
_SNAPSHOT_ERROR = "proxy snapshot rejected"
_USERINFO_ERROR = "subscription traffic rejected"
_TIMEOUT_SECONDS = 15
_MAX_AIRPORT_REDIRECTS = 3
_MAX_USERINFO_BYTES = 512
XUI_INBOUND_PORT = 10443


def fetch_xui_proxies(url, max_bytes, opener=None):
    """Fetch a validated 3x-ui loopback Clash source."""
    if not _valid_xui_url(url):
        _source_fail()
    return _fetch_proxies(url, max_bytes, opener, _valid_xui_url)


def download_airport_document(url, max_bytes, opener=None):
    """Fetch an HTTPS airport body unchanged with its traffic metadata.

    The body is never parsed or converted: any non-empty response within
    the transport limits publishes verbatim, byte for byte.  The final
    response's single Subscription-Userinfo header rides along as
    ``traffic`` when present and valid; no other header is captured.
    """
    if not _valid_airport_url(url):
        raise SourceError("airport_url_invalid")
    return _fetch_document(url, max_bytes, opener)


def _fetch_document(url, max_bytes, opener):
    if type(max_bytes) is not int or max_bytes <= 0:
        raise SourceError("airport_download_failed")
    try:
        request = Request(url, headers={"Accept": "application/yaml"})
        active_opener = opener
        if active_opener is None:
            active_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _HttpsRedirectHandler()
            )
        response = _open(active_opener, request)
        with response:
            final_url = response.geturl()
            if not _valid_airport_url(final_url):
                raise SourceError("airport_redirect_invalid")
            body = response.read(max_bytes + 1)
            traffic = _final_traffic(response)
        if not isinstance(body, bytes) or len(body) > max_bytes:
            raise SourceError("airport_document_too_large")
        if not body:
            raise SourceError("airport_download_failed")
        return AirportDownload(document=body, traffic=traffic)
    except SourceError:
        raise
    except Exception:
        pass
    raise SourceError("airport_download_failed")


def _final_traffic(response):
    """Read the final response's single Subscription-Userinfo header.

    A missing, duplicated, unreadable, or unparsable header only drops
    the metadata; the downloaded document itself is never rejected for it.
    """
    try:
        values = response.headers.get_all("Subscription-Userinfo")
        if values is None or len(values) != 1:
            return None
        return parse_subscription_userinfo(values[0])
    except Exception:
        return None


def load_proxy_snapshot(path):
    """Load one root-only proxy snapshot."""
    snapshot = Path(path)
    try:
        details = snapshot.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_uid != (0 if os.geteuid() == 0 else os.geteuid())
            or details.st_nlink != 1
        ):
            _snapshot_fail()
        return _parse_proxy_document(snapshot.read_bytes(), _SNAPSHOT_ERROR)
    except SourceError:
        raise
    except (OSError, TypeError, ValueError, yaml.YAMLError, UnicodeError):
        _snapshot_fail()


def normalize_xui_endpoints(proxies, endpoint):
    """Rewrite 3x-ui node addresses to the public entry endpoint."""
    host, port = _parse_public_endpoint(endpoint)
    normalized = []
    for proxy in _normalize_proxies({"proxies": proxies}):
        copied = copy.deepcopy(proxy)
        if copied.get("port") != XUI_INBOUND_PORT or not isinstance(copied.get("server"), str):
            _source_fail()
        copied["server"] = host
        copied["port"] = port
        normalized.append(copied)
    return normalized


def merge_proxy_sources(labeled_sources):
    """Copy sources in order and make same-name cross-source nodes distinct."""
    merged, _aliases = merge_proxy_sources_with_aliases(labeled_sources)
    return merged


def merge_proxy_sources_with_aliases(labeled_sources):
    """Merge sources and record each source's original-to-final name map.

    The alias maps exist so explicit references into one source (home group
    members) can follow the collision rewrites; a duplicate name inside the
    home source could never resolve to one final name and is rejected before
    an ambiguous map is built.  Sources other than ``home`` keep the
    historical numbered disambiguation for same-name entries.
    """
    try:
        pairs = tuple(
            labeled_sources.items()
            if isinstance(labeled_sources, Mapping)
            else labeled_sources
        )
        entries = []
        names = []
        for label, proxies in pairs:
            if not isinstance(label, str) or not label:
                _source_fail()
            normalized = _normalize_proxies({"proxies": proxies})
            if label == "home":
                label_names = [proxy["name"] for proxy in normalized]
                if len(set(label_names)) != len(label_names):
                    _source_fail()
            for proxy in normalized:
                entries.append((label, proxy))
                names.append(proxy["name"])
        duplicates = Counter(names)
        merged = []
        used_names = set()
        aliases = {}
        for label, proxy in entries:
            copied = copy.deepcopy(proxy)
            name = copied["name"]
            if duplicates[name] > 1:
                name = "%s [%s]" % (name, label)
            base_name = name
            number = 2
            while name in used_names:
                name = "%s [%s]" % (base_name, number)
                number += 1
            copied["name"] = name
            used_names.add(name)
            merged.append(copied)
            if name != proxy["name"]:
                aliases.setdefault(label, {})[proxy["name"]] = name
        return merged, aliases
    except SourceError:
        raise
    except (TypeError, ValueError, RecursionError):
        _source_fail()


def parse_subscription_userinfo(value):
    """Parse the exact non-negative traffic fields from a response header."""
    try:
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8")) > _MAX_USERINFO_BYTES
        ):
            _userinfo_fail()
        fields = {}
        for item in value.split(";"):
            name, separator, number = item.strip().partition("=")
            if not separator or name in fields or not _nonnegative_decimal(number):
                _userinfo_fail()
            fields[name] = int(number)
        if set(fields) != {"upload", "download", "total", "expire"}:
            _userinfo_fail()
        return Traffic(
            upload=fields["upload"],
            download=fields["download"],
            total=fields["total"],
            expiry_ms=fields["expire"],
        )
    except SourceError:
        raise
    except (TypeError, ValueError, UnicodeError, OverflowError):
        _userinfo_fail()


class _HttpsRedirectHandler(HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self._redirects = 0

    def redirect_request(self, request, response, code, message, headers, new_url):
        self._redirects += 1
        if self._redirects > _MAX_AIRPORT_REDIRECTS or not _valid_airport_url(new_url):
            raise SourceError("airport_redirect_invalid")
        return super().redirect_request(
            request, response, code, message, headers, new_url
        )


def _fetch_proxies(url, max_bytes, opener, valid_final_url):
    if type(max_bytes) is not int or max_bytes <= 0:
        _source_fail()
    request = Request(url, headers={"Accept": "application/yaml"})
    try:
        active_opener = opener
        if active_opener is None:
            active_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        response = _open(active_opener, request)
        with response:
            final_url = response.geturl()
            if not valid_final_url(final_url):
                _source_fail()
            body = response.read(max_bytes + 1)
        if not isinstance(body, bytes) or len(body) > max_bytes:
            _source_fail()
        return _parse_proxy_document(body, _SOURCE_ERROR)
    except SourceError:
        raise
    except Exception:
        pass
    raise SourceError(_SOURCE_ERROR)


def _open(opener, request):
    if callable(opener):
        return opener(request, _TIMEOUT_SECONDS)
    return opener.open(request, timeout=_TIMEOUT_SECONDS)


def _parse_proxy_document(body, error):
    try:
        if not body:
            raise SourceError(error)
        document = yaml.safe_load(body)
        return _normalize_proxies(document)
    except SourceError:
        raise
    except (TypeError, ValueError, yaml.YAMLError, UnicodeError, RecursionError):
        raise SourceError(error) from None


def _normalize_proxies(document):
    if not isinstance(document, Mapping):
        _source_fail()
    proxies = document.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        _source_fail()
    normalized = []
    for proxy in proxies:
        if isinstance(proxy, Mapping):
            proxy = _repair_unicode(copy.deepcopy(dict(proxy)))
        if (
            not isinstance(proxy, Mapping)
            or not isinstance(proxy.get("name"), str)
            or not proxy["name"].strip()
        ):
            _source_fail()
        normalized.append(proxy)
    return normalized


def _repair_unicode(value):
    if isinstance(value, str):
        try:
            return value.encode("utf-16", "surrogatepass").decode("utf-16")
        except UnicodeError:
            _source_fail()
    if isinstance(value, list):
        return [_repair_unicode(item) for item in value]
    if isinstance(value, Mapping):
        return {
            _repair_unicode(key): _repair_unicode(item)
            for key, item in value.items()
        }
    return value


def _parse_public_endpoint(endpoint):
    if not isinstance(endpoint, str):
        _source_fail()
    try:
        parts = urlsplit("//" + endpoint)
        host, port = parts.hostname, parts.port
        valid = (
            host is not None
            and port == 443
            and parts.username is None
            and parts.password is None
            and not parts.path
            and not parts.query
            and not parts.fragment
        )
    except ValueError:
        _source_fail()
    if not valid:
        _source_fail()
    return host, port


def _valid_xui_url(url):
    try:
        parts = urlsplit(url)
        return (
            isinstance(url, str)
            and parts.scheme == "http"
            and parts.hostname == "127.0.0.1"
            and parts.port is not None
            and 1 <= parts.port <= 65535
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and parts.path.startswith("/")
            and parts.path != "/"
        )
    except (TypeError, ValueError):
        return False


def _valid_airport_url(url):
    try:
        parts = urlsplit(url)
        return (
            isinstance(url, str)
            and parts.scheme == "https"
            and bool(parts.hostname)
            and not parts.username
            and not parts.password
            and not parts.fragment
        )
    except (TypeError, ValueError):
        return False


def _nonnegative_decimal(value):
    return isinstance(value, str) and value.isascii() and value.isdecimal()


def _source_fail():
    raise SourceError(_SOURCE_ERROR)


def _snapshot_fail():
    raise SourceError(_SNAPSHOT_ERROR)


def _userinfo_fail():
    raise SourceError(_USERINFO_ERROR)
