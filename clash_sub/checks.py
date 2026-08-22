import subprocess
from collections.abc import Mapping
from pathlib import Path

import yaml


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


def validate_clash(text, forbidden_values):
    """Return a checked Clash mapping without exposing private candidate values."""
    if not isinstance(text, str) or "{{" in text or "}}" in text:
        raise CheckError("rendered config contains invalid template data")
    for value in forbidden_values:
        if isinstance(value, str) and value and value in text:
            raise CheckError("rendered config contains forbidden value")
    if "http://127.0.0.1" in text:
        raise CheckError("rendered config contains forbidden value")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CheckError("rendered config contains invalid YAML") from exc
    if not isinstance(document, Mapping):
        raise CheckError("rendered config root must be a mapping")
    missing = _REQUIRED_TOP_LEVEL.difference(document)
    if missing:
        raise CheckError("rendered config is missing required section")
    if "proxy-providers" in document:
        raise CheckError("rendered config must not contain proxy-providers")
    proxy_names = _validate_proxies(document["proxies"])
    group_names = _validate_groups(document["proxy-groups"])
    if proxy_names & group_names:
        raise CheckError("proxy name conflicts with proxy group name")
    targets = proxy_names | group_names | _BUILTIN_TARGETS
    _validate_group_targets(document["proxy-groups"], targets)
    _validate_rule_providers(document["rule-providers"])
    _validate_rules(document["rules"], targets)
    return document


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
        if not isinstance(group.get("proxies"), list) and group.get("include-all") is not True:
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


def _validate_rule_providers(providers):
    if not isinstance(providers, Mapping):
        raise CheckError("rule-providers must be a mapping")
    if not all(_valid_name(name) and isinstance(provider, Mapping) for name, provider in providers.items()):
        raise CheckError("rule-providers are invalid")


def _validate_rules(rules, targets):
    if not isinstance(rules, list):
        raise CheckError("rules must be a list")
    for rule in rules:
        if not isinstance(rule, str) or not rule.strip():
            raise CheckError("rule is invalid")
        parts = [part.strip() for part in rule.split(",")]
        target = _rule_target(parts)
        if target is not None and target not in targets:
            raise CheckError("rule references unknown target")


def _rule_target(parts):
    if len(parts) < 2:
        return None
    index = len(parts) - 1
    while index > 1 and parts[index] == "no-resolve":
        index -= 1
    return parts[index]


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
