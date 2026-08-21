import hashlib
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from clash_sub.converter import SourceError, require_complete_reality_fields
from clash_sub.models import RealitySettings


BUILTIN_TARGETS = {
    "DIRECT",
    "REJECT",
    "REJECT-DROP",
    "PASS",
    "COMPATIBLE",
    "GLOBAL",
}

REQUIRED_TOP_LEVEL = {
    "dns",
    "proxies",
    "proxy-groups",
    "rule-providers",
    "rules",
}

_REALITY_HINT_KEYS = (
    "flow",
    "servername",
    "client-fingerprint",
    "reality-opts",
)


class ValidationError(ValueError):
    """Raised when a rendered Clash document is unsafe or incomplete."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def validate_config(
    text: str,
    source_urls: Sequence[str],
    reality: RealitySettings,
) -> Mapping[str, object]:
    if "{{" in text or "}}" in text:
        raise ValidationError("rendered config contains leftover template markers")
    for source_url in source_urls:
        if source_url and source_url in text:
            raise ValidationError("rendered config leaks an upstream source URL")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError("rendered config contains invalid YAML") from exc
    if not isinstance(document, dict):
        raise ValidationError("rendered config root must be a mapping")
    _validate_required_top_level(document)
    if "proxy-providers" in document:
        raise ValidationError("rendered config must not contain proxy-providers")
    proxies = _require_list(document["proxies"], "proxies")
    proxy_names = _validate_proxies(proxies, reality)
    groups = _require_list(document["proxy-groups"], "proxy-groups")
    group_names = _validate_groups(groups)
    all_targets = proxy_names | group_names | BUILTIN_TARGETS
    _validate_group_targets(groups, all_targets)
    _validate_rule_providers(document["rule-providers"])
    _validate_rules(document["rules"], all_targets)
    return document


def _validate_required_top_level(document) -> None:
    missing = sorted(REQUIRED_TOP_LEVEL.difference(document))
    if missing:
        raise ValidationError("rendered config is missing required section: %s" % missing[0])


def _validate_proxies(proxies, reality: RealitySettings):
    if not proxies:
        raise ValidationError("proxies must contain at least one entry")
    names = set()
    for index, proxy in enumerate(proxies):
        path = "proxies[%d]" % index
        mapping = _require_mapping(proxy, path)
        name = _require_non_empty_string(mapping.get("name"), "%s.name" % path)
        _require_unique(name, names, "duplicate proxy name: %s" % name)
        _validate_reality_proxy(mapping, reality, path)
    return names


def _validate_groups(groups):
    if not groups:
        raise ValidationError("proxy-groups must contain at least one entry")
    names = set()
    for index, group in enumerate(groups):
        path = "proxy-groups[%d]" % index
        mapping = _require_mapping(group, path)
        name = _require_non_empty_string(mapping.get("name"), "%s.name" % path)
        _require_unique(name, names, "duplicate proxy group name: %s" % name)
        proxies = mapping.get("proxies")
        if proxies is None:
            raise ValidationError("%s must define proxies" % path)
        _require_list(proxies, "%s.proxies" % path)
    return names


def _validate_group_targets(groups, all_targets) -> None:
    for index, group in enumerate(groups):
        proxies = group["proxies"]
        for target_index, target in enumerate(proxies):
            if not isinstance(target, str) or not target:
                raise ValidationError(
                    "proxy-groups[%d].proxies[%d] must be a non-empty string"
                    % (index, target_index)
                )
            if target not in all_targets:
                raise ValidationError(
                    "proxy-groups[%d].proxies[%d] references unknown target %s"
                    % (index, target_index, target)
                )


def _validate_rule_providers(rule_providers) -> None:
    providers = _require_mapping(rule_providers, "rule-providers")
    for name, provider in providers.items():
        if not isinstance(name, str) or not name:
            raise ValidationError("rule-providers keys must be non-empty strings")
        _require_mapping(provider, "rule-providers.%s" % name)


def _validate_rules(rules, all_targets) -> None:
    entries = _require_list(rules, "rules")
    for index, rule in enumerate(entries):
        if not isinstance(rule, str) or not rule:
            raise ValidationError("rules[%d] must be a non-empty string" % index)
        target = _extract_rule_target(rule)
        if target is None:
            continue
        if target not in all_targets:
            raise ValidationError("rules[%d] references unknown target %s" % (index, target))


def _extract_rule_target(rule: str):
    parts = [part.strip() for part in rule.split(",")]
    if len(parts) < 2:
        return None
    rule_type = parts[0]
    if rule_type == "MATCH":
        return parts[1]
    if len(parts) >= 3:
        return parts[2]
    return None


def _validate_reality_proxy(proxy, reality: RealitySettings, path: str) -> None:
    if proxy.get("type") != "vless":
        return
    if not _looks_like_reality_proxy(proxy, reality):
        return
    try:
        require_complete_reality_fields(proxy, reality)
    except SourceError as exc:
        raise ValidationError("%s contains incomplete REALITY settings" % path) from exc
    if proxy.get("server") != reality.public_address or proxy.get("port") != reality.public_port:
        raise ValidationError("%s must use the configured public endpoint" % path)


def _looks_like_reality_proxy(proxy, reality: RealitySettings) -> bool:
    if proxy.get("flow") == reality.required_flow:
        return True
    for key in _REALITY_HINT_KEYS:
        if key in proxy:
            return True
    return False


def _require_mapping(value, label: str):
    if not isinstance(value, dict):
        raise ValidationError("%s must be a mapping" % label)
    return value


def _require_list(value, label: str):
    if not isinstance(value, list):
        raise ValidationError("%s must be a list" % label)
    return value


def _require_non_empty_string(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("%s must be a non-empty string" % label)
    return value


def _require_unique(name: str, seen, message: str) -> None:
    if name in seen:
        raise ValidationError(message)
    seen.add(name)
