import subprocess
from collections.abc import Mapping
from pathlib import Path

import yaml

from clash_sub.domain import AIRPORT_FILENAME


class CheckError(ValueError):
    """Raised when a candidate Clash configuration is unsafe."""


_BUILTIN_TARGETS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}
_REQUIRED_TOP_LEVEL = {"dns", "proxies", "proxy-groups", "rule-providers", "rules"}
_REALITY_FIELDS = {
    "uuid",
    "server",
    "port",
    "network",
    "tls",
    "flow",
    "servername",
    "client-fingerprint",
    "reality-opts",
}
_PROVIDER_NAME = "AmyTelecom"
_PROVIDER_PATH = "./proxy_providers/%s" % AIRPORT_FILENAME


def validate_clash(text, forbidden_values, allowed_provider_url=None):
    """Return a checked Clash mapping without exposing private candidate values.

    ``allowed_provider_url`` authorizes exactly one owner ``AmyTelecom``
    provider.  The URL and the owner token inside it are then permitted only
    as the exact value of ``proxy-providers.AmyTelecom.url``; every other
    field occurrence stays a forbidden value.
    """
    if not isinstance(text, str) or "{{" in text or "{%" in text:
        raise CheckError("rendered config contains invalid template data")
    if allowed_provider_url is not None and (
        not isinstance(allowed_provider_url, str)
        or not allowed_provider_url.startswith("https://")
    ):
        raise CheckError("invalid provider authorization")
    forbidden = tuple(
        value for value in forbidden_values if isinstance(value, str) and value
    )
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CheckError("rendered config contains invalid YAML") from exc
    if not isinstance(document, Mapping):
        raise CheckError("rendered config root must be a mapping")
    missing = _REQUIRED_TOP_LEVEL.difference(document)
    if missing:
        raise CheckError("rendered config is missing required section")
    providers = _validate_proxy_providers(document.get("proxy-providers"), allowed_provider_url)
    try:
        _scan_string_scalars(document, forbidden, allowed_provider_url)
    except RecursionError:
        # A YAML alias cycle expands into a recursive structure.
        raise CheckError("rendered config contains invalid YAML") from None
    proxy_names = _validate_proxies(document["proxies"])
    group_names = _validate_groups(document["proxy-groups"])
    if proxy_names & group_names:
        raise CheckError("proxy name conflicts with proxy group name")
    targets = proxy_names | group_names | _BUILTIN_TARGETS
    _validate_group_targets(document["proxy-groups"], targets)
    _validate_group_uses(document["proxy-groups"], providers)
    _validate_rule_providers(document["rule-providers"])
    _validate_rules(document["rules"], targets, set(document["rule-providers"]))
    return document


def _scan_string_scalars(node, forbidden, allowed_provider_url, path=()):
    """Reject forbidden substrings in every field except the provider URL.

    Only the exact string value at ``proxy-providers.AmyTelecom.url`` is
    exempt (and only when it equals the authorized URL); mapping keys and
    every other string scalar, at any depth, stay fully scanned.
    """
    if isinstance(node, str):
        if (
            allowed_provider_url is not None
            and path == ("proxy-providers", _PROVIDER_NAME, "url")
            and node == allowed_provider_url
        ):
            return
        for value in forbidden:
            if value in node:
                raise CheckError("rendered config contains forbidden value")
        if "http://127.0.0.1" in node:
            raise CheckError("rendered config contains forbidden value")
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            _scan_string_scalars(key, forbidden, None)
            _scan_string_scalars(
                value, forbidden, allowed_provider_url, path + (str(key),)
            )
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _scan_string_scalars(item, forbidden, allowed_provider_url, path)


def _validate_proxy_providers(providers, allowed_provider_url):
    if providers is None:
        if allowed_provider_url is not None:
            raise CheckError("owner config must declare the airport proxy-provider")
        return set()
    if not isinstance(providers, Mapping):
        raise CheckError("proxy-providers must declare the airport provider")
    if _PROVIDER_NAME in providers:
        if allowed_provider_url is None:
            raise CheckError("member proxy-providers must not contain the airport provider")
        provider = providers[_PROVIDER_NAME]
        interval = provider.get("interval") if isinstance(provider, Mapping) else None
        if (
            not isinstance(provider, Mapping)
            or provider.get("type") != "http"
            or provider.get("url") != allowed_provider_url
            or type(interval) is not int
            or interval != 86400
            or provider.get("path") != _PROVIDER_PATH
        ):
            raise CheckError("airport proxy-provider mapping is invalid")
    elif allowed_provider_url is not None:
        raise CheckError("proxy-providers must declare the airport provider")
    for name, extra in providers.items():
        if name == _PROVIDER_NAME:
            continue
        if not isinstance(name, str) or not name.strip() or not isinstance(extra, Mapping):
            raise CheckError("proxy-providers mapping is invalid")
        provider_type = extra.get("type")
        path = extra.get("path")
        if provider_type not in {"http", "file"} or not isinstance(path, str) or not path.strip():
            raise CheckError("proxy-providers mapping is invalid")
        if provider_type == "http":
            url = extra.get("url")
            interval = extra.get("interval")
            if (
                not isinstance(url, str)
                or not (url.startswith("http://") or url.startswith("https://"))
                or type(interval) is not int
                or interval <= 0
            ):
                raise CheckError("proxy-providers mapping is invalid")
    return set(providers)


def _validate_proxies(proxies):
    if not isinstance(proxies, list) or not proxies:
        raise CheckError("proxies must be a non-empty list")
    names = set()
    for proxy in proxies:
        if not isinstance(proxy, Mapping) or not _valid_name(proxy.get("name")):
            raise CheckError("proxy name is invalid")
        name = proxy["name"].strip()
        if name in names:
            raise CheckError("duplicate proxy name")
        names.add(name)
        _validate_reality(proxy)
    return names


def _validate_reality(proxy):
    if proxy.get("type") != "vless" or (
        "reality-opts" not in proxy and proxy.get("flow") != "xtls-rprx-vision"
    ):
        return
    if not _REALITY_FIELDS.issubset(proxy):
        raise CheckError("VLESS REALITY options are incomplete")
    if not all(
        _nonempty_string(proxy[key])
        for key in ("uuid", "server", "network", "servername", "client-fingerprint")
    ):
        raise CheckError("VLESS REALITY options are incomplete")
    if type(proxy["port"]) is not int or not 1 <= proxy["port"] <= 65535:
        raise CheckError("VLESS REALITY options are incomplete")
    if proxy["tls"] is not True:
        raise CheckError("VLESS REALITY options are incomplete")
    if proxy["network"] != "tcp" or proxy["flow"] != "xtls-rprx-vision":
        raise CheckError("VLESS REALITY options are incomplete")
    options = proxy["reality-opts"]
    if (
        not isinstance(options, Mapping)
        or not _nonempty_string(options.get("public-key"))
        or not _nonempty_string(options.get("short-id"))
    ):
        raise CheckError("VLESS REALITY options are incomplete")


def _validate_groups(groups):
    if not isinstance(groups, list) or not groups:
        raise CheckError("proxy-groups must be a non-empty list")
    names = set()
    for group in groups:
        if not isinstance(group, Mapping) or not _valid_name(group.get("name")):
            raise CheckError("proxy group name is invalid")
        name = group["name"].strip()
        if name in names:
            raise CheckError("duplicate proxy group name")
        names.add(name)
        proxies = group.get("proxies")
        uses = group.get("use")
        if (
            (not isinstance(proxies, list) or not proxies)
            and group.get("include-all") is not True
            and (not isinstance(uses, list) or not uses)
        ):
            # Mihomo refuses a group whose proxies and use are both empty;
            # mirror that instead of accepting an unloadable configuration.
            raise CheckError("proxy group must define proxies or include-all")
    return names


def _validate_group_targets(groups, targets):
    indexes = {group["name"].strip(): index for index, group in enumerate(groups)}
    references = {index: [] for index in range(len(groups))}
    for index, group in enumerate(groups):
        for target in group.get("proxies", []):
            if not _valid_name(target) or target.strip() not in targets:
                raise CheckError("proxy group references unknown target")
            target_index = indexes.get(target.strip())
            if target_index is not None:
                references[index].append(target_index)
    _validate_group_cycles(references)


def _validate_group_uses(groups, providers):
    for group in groups:
        uses = group.get("use")
        if uses is None:
            continue
        if (
            not isinstance(uses, list)
            or not uses
            or not all(_valid_name(use) for use in uses)
        ):
            raise CheckError("proxy group use entries are invalid")
        if any(use.strip() not in providers for use in uses):
            raise CheckError("proxy group references unknown provider")


def _validate_rule_providers(providers):
    if not isinstance(providers, Mapping):
        raise CheckError("rule-providers must be a mapping")
    if not all(_valid_name(name) and isinstance(provider, Mapping) for name, provider in providers.items()):
        raise CheckError("rule-providers are invalid")


def _validate_rules(rules, targets, providers):
    if not isinstance(rules, list):
        raise CheckError("rules must be a list")
    for rule in rules:
        if not isinstance(rule, str) or not rule.strip():
            raise CheckError("rule is invalid")
        parts = [part.strip() for part in rule.split(",")]
        if parts[0] == "RULE-SET":
            if len(parts) < 3 or parts[1] not in providers:
                raise CheckError("rule references unknown provider")
        target = _rule_target(parts)
        if target is not None and target not in targets:
            raise CheckError("rule references unknown target")


def _rule_target(parts):
    if len(parts) < 2:
        return None
    index = len(parts) - 1
    while index > 1 and parts[index] == "no-resolve":
        index -= 1
    return parts[index] or None


def _valid_name(value):
    return isinstance(value, str) and bool(value.strip())


def _valid_value(value):
    return value is not None and value != ""


def _nonempty_string(value):
    return isinstance(value, str) and bool(value)


def _validate_group_cycles(references):
    visiting = set()
    visited = set()

    def visit(index):
        if index in visiting:
            raise CheckError("recursive proxy group reference")
        if index in visited:
            return
        visiting.add(index)
        for target in references[index]:
            visit(target)
        visiting.remove(index)
        visited.add(index)

    for index in references:
        visit(index)


class MihomoValidator:
    def __init__(self, binary, runner=subprocess.run):
        self.binary = Path(binary)
        self.runner = runner

    def validate(self, path):
        candidate = Path(path)
        arguments = [str(self.binary), "-t", "-f", str(candidate)]
        failure = None
        try:
            result = self.runner(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failure = CheckError("Mihomo validation timed out")
        except OSError:
            failure = CheckError("Mihomo validation could not run")
        if failure is not None:
            raise failure
        if result.returncode != 0:
            raise CheckError("Mihomo validation failed")
