import copy
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

from clash_sub.models import RealitySettings


class SourceError(RuntimeError):
    """Raised when a source cannot be converted into a safe proxy list."""


class SubconverterClient:
    def __init__(
        self,
        base_url: str,
        opener=urlopen,
        timeout: int = 20,
        max_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = opener
        self.timeout = timeout
        self.max_bytes = max_bytes

    def build_url(self, source_url: str) -> str:
        query = urlencode({"target": "clash", "url": source_url, "list": "true"})
        return "%s/sub?%s" % (self.base_url, query)

    def convert(self, source_url: str) -> Tuple[Mapping[str, object], ...]:
        request = Request(self.build_url(source_url), headers={"Accept": "text/yaml"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = response.read(self.max_bytes + 1)
        except (OSError, HTTPError, URLError) as exc:
            raise SourceError("source conversion failed") from exc
        if len(payload) > self.max_bytes:
            raise SourceError("converter response exceeds size limit")
        try:
            document = yaml.safe_load(payload.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise SourceError("converter returned invalid YAML") from exc
        return _extract_proxy_list(document, "converter response")


def load_local_proxies(path: Path) -> Tuple[Mapping[str, object], ...]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        raise SourceError("local source is unreadable or invalid") from exc
    return _extract_proxy_list(document, "local source")


def normalize_reality_proxy(
    proxy: Mapping[str, object],
    reality: RealitySettings,
) -> Mapping[str, object]:
    normalized = copy.deepcopy(dict(proxy))
    if normalized.get("type") != "vless":
        raise SourceError("3x-ui source contains a non-VLESS node")
    require_complete_reality_fields(normalized, reality)
    normalized["server"] = reality.public_address
    normalized["port"] = reality.public_port
    return normalized


def require_complete_reality_fields(
    proxy: Mapping[str, object],
    reality: RealitySettings,
) -> None:
    if proxy.get("network") != "tcp":
        raise SourceError("3x-ui source contains incomplete REALITY settings")
    if proxy.get("tls") is not True:
        raise SourceError("3x-ui source contains incomplete REALITY settings")
    if proxy.get("flow") != reality.required_flow:
        raise SourceError("3x-ui source contains incomplete REALITY settings")
    if not isinstance(proxy.get("servername"), str) or not proxy.get("servername"):
        raise SourceError("3x-ui source contains incomplete REALITY settings")
    if not isinstance(proxy.get("client-fingerprint"), str) or not proxy.get("client-fingerprint"):
        raise SourceError("3x-ui source contains incomplete REALITY settings")
    reality_opts = proxy.get("reality-opts")
    if not isinstance(reality_opts, dict):
        raise SourceError("3x-ui source contains incomplete REALITY settings")
    public_key = reality_opts.get("public-key")
    short_id = reality_opts.get("short-id")
    if not isinstance(public_key, str) or not public_key:
        raise SourceError("3x-ui source contains incomplete REALITY settings")
    if not isinstance(short_id, str) or not short_id:
        raise SourceError("3x-ui source contains incomplete REALITY settings")


def merge_proxy_sources(
    sources: Sequence[Tuple[str, Sequence[Mapping[str, object]]]],
) -> Tuple[Mapping[str, object], ...]:
    occurrences = Counter()
    for _label, proxies in sources:
        for proxy in proxies:
            occurrences[str(proxy.get("name"))] += 1

    used = set()
    merged = []
    for label, proxies in sources:
        for proxy in proxies:
            copied = copy.deepcopy(dict(proxy))
            original = str(copied.get("name"))
            base = original
            if occurrences[original] > 1:
                base = "%s [%s]" % (original, label)
            copied["name"] = unique_name(base, used)
            used.add(copied["name"])
            merged.append(copied)
    return tuple(merged)


def unique_name(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    index = 2
    while True:
        candidate = "%s-%d" % (base, index)
        if candidate not in used:
            return candidate
        index += 1


def _extract_proxy_list(document, label: str) -> Tuple[Mapping[str, object], ...]:
    if not isinstance(document, dict) or not isinstance(document.get("proxies"), list):
        raise SourceError("%s has no proxy list" % label)
    proxies = tuple(copy.deepcopy(document["proxies"]))
    if not proxies or not all(isinstance(item, dict) for item in proxies):
        raise SourceError("%s contains invalid proxies" % label)
    return proxies
