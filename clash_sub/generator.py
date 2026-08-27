"""Object-level template composition.

The public policy lives once in ``templates/clash.yaml``.  Variant
differences are composed at load time: declared features add or extend
groups and prepend rules, declared overrides recursively merge keys, and
the authorized proxy sources are injected into the declared groups.  The
result is dumped deterministically; no text placeholders are involved.
"""

import copy
from pathlib import Path

import yaml

from clash_sub.domain import MEMBER_VARIANTS, OWNER_VARIANTS
from clash_sub.sources import merge_proxy_sources


# Features may only be composed into the variants listed here; the manifest
# cannot extend this authorization.
_FEATURE_VARIANTS = {
    "home": {"balanced", "privacy"},
}

_FEATURE_KEYS = (
    "add-proxy-groups",
    "extend-proxy-groups",
    "prepend-rules",
    "inject-node-groups",
    "inject-home-node-groups",
)

# Overrides are pure Clash configuration; they must never redefine the node
# supply or any composition control.
_OVERRIDE_FORBIDDEN_KEYS = {
    "proxies",
    "add-proxy-groups",
    "extend-proxy-groups",
    "prepend-rules",
    "append-rules",
    "inject-node-groups",
    "inject-home-node-groups",
}


def render_user_bundle(is_owner, xui, airport, home, template_root):
    """Render only the variants and proxy sources authorized for one user."""
    sources_by_variant = _authorized_sources(is_owner, xui, airport, home)
    return {
        variant: _render_variant(Path(template_root), variant, sources)
        for variant, sources in sources_by_variant
    }


def _authorized_sources(is_owner, xui, airport, home):
    xui_source = ("3x-ui", xui)
    if not is_owner:
        return ((MEMBER_VARIANTS[0], (xui_source,)),)
    owner_sources = (xui_source, ("airport", airport))
    owner_sources_with_home = owner_sources + (("home", home),) if home else owner_sources
    return (
        ("balanced", owner_sources_with_home),
        ("standard", owner_sources),
        ("privacy", owner_sources_with_home),
    )


def _render_variant(template_root, variant, sources):
    proxies = merge_proxy_sources(sources)
    document, injections = _compose_variant(template_root, variant)
    document["proxies"] = proxies
    _inject_proxy_names(document, injections, _source_proxy_names(sources, proxies))
    rendered = yaml.safe_dump(
        document, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return rendered if rendered.endswith("\n") else rendered + "\n"


def _compose_variant(template_root, variant):
    document = _load_mapping(template_root / "clash.yaml")
    if document.get("proxies") != []:
        raise ValueError("public template must carry an empty proxies list")
    manifest = _load_manifest(template_root)
    composition = manifest["variants"][variant]
    injections = {}
    _declare_injections(
        injections, {group: "all" for group in manifest["inject-node-groups"]}
    )
    for feature_name in composition["features"]:
        feature = _load_feature(template_root, feature_name)
        _apply_feature(document, feature)
        _declare_injections(
            injections, {group: "all" for group in feature["inject-node-groups"]}
        )
        _declare_injections(
            injections, {group: "home" for group in feature["inject-home-node-groups"]}
        )
    for override_name in composition["overrides"]:
        _merge_override(document, _load_override(template_root, override_name))
    return document, injections


def _load_mapping(path):
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError("template file could not be read") from None
    if not isinstance(document, dict):
        raise ValueError("template file must be a mapping")
    return document


def _load_manifest(template_root):
    manifest = _load_mapping(template_root / "variants" / "manifest.yaml")
    variants = manifest.get("variants")
    if not isinstance(variants, dict) or set(variants) != set(OWNER_VARIANTS):
        raise ValueError("manifest variants must match the fixed variant set")
    for name, composition in variants.items():
        if not isinstance(composition, dict):
            raise ValueError("variant composition must be a mapping")
        features = composition.get("features")
        overrides = composition.get("overrides")
        if not _string_list(features) or not _string_list(overrides):
            raise ValueError("variant composition must declare string lists")
        for feature in features:
            if feature not in _FEATURE_VARIANTS:
                raise ValueError("unknown feature in variant composition")
            if name not in _FEATURE_VARIANTS[feature]:
                raise ValueError("feature is not authorized for this variant")
    if not _string_list(manifest.get("inject-node-groups")):
        raise ValueError("manifest inject-node-groups must be a string list")
    return manifest


def _load_feature(template_root, name):
    feature = _load_mapping(template_root / "features" / ("%s.yaml" % name))
    unknown = set(feature) - set(_FEATURE_KEYS)
    if unknown:
        raise ValueError("feature declares unknown operations")
    add_groups = feature.get("add-proxy-groups", [])
    if not isinstance(add_groups, list) or not all(
        isinstance(group, dict) and isinstance(group.get("name"), str) and group["name"].strip()
        for group in add_groups
    ):
        raise ValueError("feature add-proxy-groups must be named mappings")
    extend_groups = feature.get("extend-proxy-groups", {})
    if not isinstance(extend_groups, dict) or not all(
        isinstance(group, str) and _string_list(members)
        for group, members in extend_groups.items()
    ):
        raise ValueError("feature extend-proxy-groups must map groups to string lists")
    if not _string_list(feature.get("prepend-rules", [])):
        raise ValueError("feature prepend-rules must be a string list")
    if not _string_list(feature.get("inject-node-groups", [])):
        raise ValueError("feature inject-node-groups must be a string list")
    if not _string_list(feature.get("inject-home-node-groups", [])):
        raise ValueError("feature inject-home-node-groups must be a string list")
    return feature


def _load_override(template_root, name):
    override = _load_mapping(template_root / "variants" / ("%s.yaml" % name))
    if set(override) & _OVERRIDE_FORBIDDEN_KEYS:
        raise ValueError("override must not redefine nodes or composition controls")
    return override


def _apply_feature(document, feature):
    groups = document.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError("proxy-groups must exist before applying features")
    known = {group.get("name") for group in groups if isinstance(group, dict)}
    for group in feature.get("add-proxy-groups", []):
        if group["name"] in known:
            raise ValueError("feature proxy group already exists")
        groups.append(copy.deepcopy(group))
        known.add(group["name"])
    by_name = {group["name"]: group for group in groups}
    for group_name, members in feature.get("extend-proxy-groups", {}).items():
        target = by_name.get(group_name)
        if target is None:
            raise ValueError("feature extends a missing proxy group")
        proxies = target.get("proxies")
        if not isinstance(proxies, list):
            raise ValueError("feature extends a group without a proxies list")
        for member in members:
            if member in proxies:
                raise ValueError("feature extension duplicates a group member")
            proxies.append(member)
    prepend_rules = feature.get("prepend-rules", [])
    if prepend_rules:
        rules = document.get("rules")
        if not isinstance(rules, list):
            raise ValueError("rules must exist before applying features")
        document["rules"] = list(prepend_rules) + rules


def _merge_override(document, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(document.get(key), dict):
            _merge_mapping(document[key], value)
        else:
            document[key] = copy.deepcopy(value)


def _merge_mapping(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_mapping(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _declare_injections(injections, additions):
    for group, source in additions.items():
        if group in injections:
            raise ValueError("proxy group injection is declared twice")
        injections[group] = source


def _source_proxy_names(sources, proxies):
    names = {"all": [proxy["name"] for proxy in proxies]}
    index = 0
    for label, source in sources:
        count = len(source)
        names[label] = [proxy["name"] for proxy in proxies[index : index + count]]
        index += count
    names.setdefault("home", [])
    return names


def _inject_proxy_names(document, injections, source_names):
    groups = document["proxy-groups"]
    indexes = {}
    for index, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            raise ValueError("proxy-groups entries must have names")
        indexes.setdefault(group["name"], []).append(index)
    for group_name, source_name in injections.items():
        matching = indexes.get(group_name, [])
        if len(matching) != 1:
            raise ValueError("inject-node-group %r must exist exactly once" % group_name)
        targets = groups[matching[0]].setdefault("proxies", [])
        if not isinstance(targets, list):
            raise ValueError("inject-node-group %r must expose proxies" % group_name)
        if source_name not in source_names:
            raise ValueError("inject-node-group %r references unknown source" % group_name)
        for name in source_names[source_name]:
            if name not in targets:
                targets.append(name)


def _string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
