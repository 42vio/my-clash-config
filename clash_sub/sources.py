import copy
import hashlib
import os
import stat
import urllib.request
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request

import yaml

from clash_sub.domain import HomeOverlay, Traffic


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
    """Fetch an HTTPS airport body and return its exact bytes unchanged.

    The document is parsed only far enough to confirm a non-empty proxy
    list; comments, formatting, and ordering survive for publication.
    """
    if not _valid_airport_url(url):
        _source_fail()
    return _fetch_document(url, max_bytes, opener)


def _fetch_document(url, max_bytes, opener):
    if type(max_bytes) is not int or max_bytes <= 0:
        _source_fail()
    request = Request(url, headers={"Accept": "application/yaml"})
    try:
        active_opener = opener
        if active_opener is None:
            active_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _HttpsRedirectHandler()
            )
        response = _open(active_opener, request)
        with response:
            final_url = response.geturl()
            if not _valid_airport_url(final_url):
                _source_fail()
            body = response.read(max_bytes + 1)
        if not isinstance(body, bytes) or len(body) > max_bytes:
            _source_fail()
        _require_proxy_document(body)
        return body
    except SourceError:
        raise
    except Exception:
        pass
    raise SourceError(_SOURCE_ERROR)


def _require_proxy_document(body):
    try:
        document = yaml.safe_load(body)
        valid = (
            isinstance(document, Mapping)
            and isinstance(document.get("proxies"), list)
            and bool(document["proxies"])
        )
    except (TypeError, ValueError, yaml.YAMLError, UnicodeError, RecursionError):
        raise SourceError(_SOURCE_ERROR) from None
    if not valid:
        _source_fail()


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
            for proxy in _normalize_proxies({"proxies": proxies}):
                entries.append((label, proxy))
                names.append(proxy["name"])
        duplicates = Counter(names)
        merged = []
        used_names = set()
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
        return merged
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
            _source_fail()
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


_HOME_KEYS = frozenset(
    (
        "proxies",
        "proxy-groups",
        "extend-proxy-groups",
        "inject-node-groups",
        "inject-home-node-groups",
        "rules",
    )
)
_HOME_ERROR_CODES = frozenset(
    (
        "home_source_invalid",
        "home_yaml_invalid",
        "home_schema_invalid",
        "home_proxy_invalid",
        "home_group_invalid",
        "home_group_reference_invalid",
        "home_rule_invalid",
        "home_extension_invalid",
        "home_mihomo_validation_failed",
    )
)
_HOME_RULE_OPTIONS = frozenset(("no-resolve", "src"))
_HOME_RULE_POLICIES = frozenset(("DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"))


class HomeSourceError(RuntimeError):
    """Raised when the private home overlay cannot be safely used."""

    def __init__(self, code):
        if code not in _HOME_ERROR_CODES:
            raise ValueError("unapproved home error code")
        super().__init__("home overlay rejected: %s" % code)
        self.code = code


def parse_home_overlay(payload, max_bytes):
    """Parse and validate the strict six-field private home overlay."""
    _require_home_payload(payload, max_bytes)
    return _build_home_overlay(_load_home_document(payload))


def load_home_overlay(path, max_bytes):
    """Load one owner-only private home overlay file."""
    overlay_path = Path(path)
    payload = None
    try:
        details = overlay_path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_uid != os.geteuid()
            or details.st_nlink > 1
        ):
            _home_fail("home_source_invalid")
        with overlay_path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
    except HomeSourceError:
        raise
    except (OSError, TypeError, ValueError):
        payload = None
    if payload is None:
        _home_fail("home_source_invalid")
    return parse_home_overlay(payload, max_bytes)


def dump_home_overlay(home):
    """Serialize one home overlay back to canonical overlay bytes."""
    document = {
        "proxies": [dict(proxy) for proxy in home.proxies],
        "proxy-groups": [dict(group) for group in home.proxy_groups],
        "extend-proxy-groups": {
            key: list(value) for key, value in home.extend_proxy_groups.items()
        },
        "inject-node-groups": list(home.inject_node_groups),
        "inject-home-node-groups": list(home.inject_home_node_groups),
        "rules": list(home.rules),
    }
    text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    return (text.rstrip("\n") + "\n").encode("utf-8")


def home_overlay_digest(home):
    """Return the stable sha256 digest of the canonical overlay bytes."""
    return hashlib.sha256(dump_home_overlay(home)).hexdigest()


def _home_fail(code):
    raise HomeSourceError(code)


def _require_home_payload(payload, max_bytes):
    if (
        not isinstance(payload, bytes)
        or type(max_bytes) is not int
        or max_bytes <= 0
        or len(payload) > max_bytes
    ):
        _home_fail("home_source_invalid")


def _load_home_document(payload):
    if not payload or b"{{" in payload or b"{%" in payload:
        _home_fail("home_yaml_invalid")
    try:
        return yaml.safe_load(payload.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
        pass
    _home_fail("home_yaml_invalid")


def _build_home_overlay(document):
    if not isinstance(document, Mapping) or set(document) != _HOME_KEYS:
        _home_fail("home_schema_invalid")
    proxies = _home_named_entries(
        document["proxies"], "home_schema_invalid", "home_proxy_invalid"
    )
    groups = _home_named_entries(
        document["proxy-groups"], "home_schema_invalid", "home_group_invalid"
    )
    group_names = frozenset(group["name"] for group in groups)
    extensions = _home_extensions(document["extend-proxy-groups"], group_names)
    inject_node = _home_injection(document["inject-node-groups"], group_names)
    inject_home = _home_injection(document["inject-home-node-groups"], group_names)
    if set(inject_node) & set(inject_home):
        _home_fail("home_group_reference_invalid")
    return HomeOverlay(
        proxies=proxies,
        proxy_groups=groups,
        extend_proxy_groups=extensions,
        inject_node_groups=inject_node,
        inject_home_node_groups=inject_home,
        rules=_home_rules(document["rules"], group_names),
    )


def _home_named_entries(entries, shape_code, entry_code):
    if not isinstance(entries, list):
        _home_fail(shape_code)
    if not entries:
        _home_fail(entry_code)
    names = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            _home_fail(entry_code)
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            _home_fail(entry_code)
        names.add(name)
    return tuple(entries)


def _home_extensions(extensions, group_names):
    if not isinstance(extensions, Mapping):
        _home_fail("home_schema_invalid")
    normalized = {}
    for key, value in extensions.items():
        if not isinstance(key, str) or not key.strip():
            _home_fail("home_extension_invalid")
        if not isinstance(value, list) or not value:
            _home_fail("home_extension_invalid")
        targets = []
        for target in value:
            if not isinstance(target, str) or not target.strip():
                _home_fail("home_extension_invalid")
            if target not in group_names:
                _home_fail("home_group_reference_invalid")
            targets.append(target)
        normalized[key] = tuple(targets)
    return normalized


def _home_injection(names, group_names):
    if not isinstance(names, list):
        _home_fail("home_schema_invalid")
    for name in names:
        if not isinstance(name, str):
            _home_fail("home_schema_invalid")
        if name not in group_names:
            _home_fail("home_group_reference_invalid")
    if len(set(names)) != len(names):
        _home_fail("home_group_reference_invalid")
    return tuple(names)


def _home_rules(rules, group_names):
    if not isinstance(rules, list):
        _home_fail("home_schema_invalid")
    for rule in rules:
        if not isinstance(rule, str) or not rule.strip():
            _home_fail("home_rule_invalid")
        parts = rule.strip().split(",")
        if len(parts) < 2:
            _home_fail("home_rule_invalid")
        if parts[0].strip().lower() in ("match", "final"):
            _home_fail("home_rule_invalid")
        target = parts[-1]
        if target in _HOME_RULE_OPTIONS:
            if len(parts) < 3:
                _home_fail("home_rule_invalid")
            target = parts[-2]
        if target not in group_names and target not in _HOME_RULE_POLICIES:
            _home_fail("home_rule_invalid")
    return tuple(rules)
