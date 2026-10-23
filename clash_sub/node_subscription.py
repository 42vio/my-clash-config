"""Portable node-only subscriptions (Base64-encoded share URI lists)."""

import base64
import ipaddress
from urllib.parse import quote, urlencode


class NodeSubscriptionError(ValueError):
    """Sanitized failure at the portable node export boundary."""


def render_node_subscription(proxies):
    """Export supported nodes without silently losing transport settings."""
    if not proxies:
        raise NodeSubscriptionError("node subscription requires proxies")
    try:
        lines = [_share_uri(proxy) for proxy in proxies]
        return base64.b64encode(("\n".join(lines) + "\n").encode("utf-8")).decode("ascii") + "\n"
    except (KeyError, TypeError, ValueError, AttributeError):
        raise NodeSubscriptionError("node subscription contains unsupported options") from None


def _string(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError
    return value


def _share_uri(proxy):
    # These are the source fields represented by this exporter. Refuse new
    # protocol options instead of publishing a subtly different connection.
    supported = {"name", "type", "server", "port", "uuid", "password", "cipher",
                 "network", "tls", "flow", "servername", "sni", "client-fingerprint",
                 "reality-opts", "skip-cert-verify", "alpn", "udp", "tfo"}
    if set(proxy) - supported or proxy.get("network", "tcp") != "tcp":
        raise ValueError
    host = _string(proxy["server"])
    if ":" in host:
        host = "[%s]" % ipaddress.IPv6Address(host)
    elif any(c in host for c in "/@?#[] "):
        raise ValueError
    port = proxy["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError
    address = "%s:%d" % (host, port)
    tag = quote(_string(proxy["name"]), safe="")
    kind = proxy["type"]
    if kind == "ss":
        if set(proxy) - {"name", "type", "server", "port", "cipher", "password", "udp", "tfo"}:
            raise ValueError
        cipher, password = _string(proxy["cipher"]), _string(proxy["password"])
        if cipher.startswith("2022-"):
            userinfo = quote(cipher, safe="") + ":" + quote(password, safe="")
        else:
            userinfo = base64.urlsafe_b64encode((cipher + ":" + password).encode()).decode().rstrip("=")
        return "ss://%s@%s#%s" % (userinfo, address, tag)
    if kind not in {"vless", "trojan"}:
        raise ValueError
    userinfo = quote(_string(proxy["uuid" if kind == "vless" else "password"]), safe="")
    query = {"type": "tcp"}
    if kind == "vless":
        query["encryption"] = "none"
    tls = proxy.get("tls", kind == "trojan")
    if type(tls) is not bool:
        raise ValueError
    reality = proxy.get("reality-opts")
    if reality is not None:
        if not tls or set(reality) - {"public-key", "short-id", "support-x25519mlkem768"}:
            raise ValueError
        query.update(security="reality", pbk=_string(reality["public-key"]))
        short_id = reality.get("short-id", "")
        if not isinstance(short_id, str) or len(short_id) > 16 or len(short_id) % 2 or any(c not in "0123456789abcdefABCDEF" for c in short_id):
            raise ValueError
        query["sid"] = short_id
        if "support-x25519mlkem768" in reality:
            # Mihomo's share-URI extension preserves this TLS capability.
            # Other clients choose key exchange through their own TLS stack.
            enabled = reality["support-x25519mlkem768"]
            if type(enabled) is not bool:
                raise ValueError
            query["support-x25519mlkem768"] = str(enabled).lower()
    else:
        query["security"] = "tls" if tls else "none"
    if kind == "trojan" and not tls:
        raise ValueError
    for source, target in (("flow", "flow"), ("client-fingerprint", "fp")):
        if proxy.get(source):
            query[target] = _string(proxy[source])
    sni = proxy.get("servername", proxy.get("sni"))
    if sni:
        query["sni"] = _string(sni)
    if "alpn" in proxy:
        if not isinstance(proxy["alpn"], list) or not proxy["alpn"]:
            raise ValueError
        query["alpn"] = ",".join(_string(item) for item in proxy["alpn"])
    if "skip-cert-verify" in proxy:
        if type(proxy["skip-cert-verify"]) is not bool:
            raise ValueError
        query["allowInsecure"] = "1" if proxy["skip-cert-verify"] else "0"
    return "%s://%s@%s?%s#%s" % (kind, userinfo, address, urlencode(query, quote_via=quote), tag)
