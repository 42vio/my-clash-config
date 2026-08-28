"""Object-level template composition.

The public policy lives once in ``templates/clash.yaml``.  Variant
differences are composed at load time: declared overrides recursively merge
keys, the authorized proxy sources are injected into the declared groups,
and the owner's private home overlay contributes its groups, extensions,
rules, and injection declarations into the balanced and privacy variants
only.  The result is dumped deterministically; no text placeholders are
involved.
"""

import copy
from collections.abc import Mapping
from pathlib import Path

import yaml

from clash_sub.domain import MEMBER_VARIANTS, OWNER_VARIANTS, HomeOverlay
from clash_sub.sources import (
    HomeSourceError,
    merge_proxy_sources_with_aliases,
    rule_is_terminal,
)


PROVIDER_NAME = "AmyTelecom"
HOME_SOURCE_LABEL = "home"

# The private home overlay is composed into exactly these owner variants;
# neither the manifest nor the overlay can widen this authorization.
_HOME_VARIANTS = frozenset({"balanced", "privacy"})

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
    if home is not None and not isinstance(home, HomeOverlay):
        raise ValueError("home source must be a private home overlay")
    sources_by_variant = _authorized_sources(is_owner, xui, airport, home)
    return {
        variant: _render_variant(
            Path(template_root),
            variant,
            sources,
            airport,
            home if variant in _HOME_VARIANTS else None,
        )
        for variant, sources in sources_by_variant
    }


def _authorized_sources(is_owner, xui, airport, home):
    xui_source = ("3x-ui", xui)
    if not is_owner:
        if airport is not None:
            raise ValueError("member profiles must not reference an airport provider")
        return ((MEMBER_VARIANTS[0], (xui_source,)),)
    if airport is None:
        raise ValueError("owner profiles require the airport provider")
    owner_sources = (xui_source,)
    owner_sources_with_home = (
        owner_sources + ((HOME_SOURCE_LABEL, list(home.proxies)),)
        if home
        else owner_sources
    )
    return (
        ("balanced", owner_sources_with_home),
        ("standard", owner_sources),
        ("privacy", owner_sources_with_home),
    )


def _render_variant(template_root, variant, sources, airport, home):
    proxies, source_aliases = merge_proxy_sources_with_aliases(sources)
    document, injections = _compose_variant(template_root, variant)
    document["proxies"] = proxies
    if airport is not None:
        document = _with_provider(document, airport)
    if home is not None:
        home_injections = _apply_home_overlay(
            document, home, source_aliases.get(HOME_SOURCE_LABEL, {})
        )
        try:
            _declare_injections(injections, home_injections)
        except ValueError:
            _home_fail("home_group_reference_invalid")
    _inject_proxy_names(
        document,
        injections,
        _source_proxy_names(sources, proxies),
        PROVIDER_NAME if airport is not None else None,
    )
    rendered = yaml.safe_dump(
        document, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return rendered if rendered.endswith("\n") else rendered + "\n"


def _apply_home_overlay(document, home, source_aliases):
    """Compose one private home overlay into its owner document.

    Private groups are copied with explicit home proxy members rewritten
    through the source alias map, the declared extensions append members to
    the target public groups, private rules are prepended before the public
    rules, and the overlay's two injection declarations are returned for the
    shared runtime injection pass.
    """
    groups = document.get("proxy-groups")
    if not isinstance(groups, list):
        _home_fail("home_group_invalid")
    known = {group.get("name") for group in groups if isinstance(group, dict)}
    home_group_names = set()
    for group in home.proxy_groups:
        name = group.get("name") if isinstance(group, Mapping) else None
        if not isinstance(name, str) or not name.strip() or name in known:
            _home_fail("home_group_invalid")
        copied = copy.deepcopy(dict(group))
        members = copied.get("proxies")
        if isinstance(members, list):
            copied["proxies"] = [
                source_aliases.get(member, member) if isinstance(member, str) else member
                for member in members
            ]
        groups.append(copied)
        known.add(name)
        home_group_names.add(name)
    by_name = {group["name"]: group for group in groups if isinstance(group, dict)}
    for group_name, members in home.extend_proxy_groups.items():
        target = by_name.get(group_name)
        if target is None:
            _home_fail("home_extension_invalid")
        proxies = target.get("proxies")
        if not isinstance(proxies, list):
            _home_fail("home_extension_invalid")
        for member in members:
            if member not in home_group_names:
                _home_fail("home_group_reference_invalid")
            if member in proxies:
                _home_fail("home_extension_invalid")
            proxies.append(member)
    public_rules = document.get("rules")
    if not isinstance(public_rules, list):
        _home_fail("home_rule_invalid")
    for rule in home.rules:
        if not isinstance(rule, str) or not rule.strip() or rule_is_terminal(rule):
            _home_fail("home_rule_invalid")
    document["rules"] = list(home.rules) + public_rules
    declared = list(home.inject_node_groups) + list(home.inject_home_node_groups)
    if len(set(declared)) != len(declared):
        _home_fail("home_group_reference_invalid")
    for group in declared:
        if group not in home_group_names:
            _home_fail("home_group_reference_invalid")
    injections = {group: "all" for group in home.inject_node_groups}
    injections.update({group: "home" for group in home.inject_home_node_groups})
    return injections


def _home_fail(code):
    raise HomeSourceError(code)


def _with_provider(document, airport):
    rebuilt = {}
    for key, value in document.items():
        rebuilt[key] = value
        if key == "proxies":
            rebuilt["proxy-providers"] = {
                PROVIDER_NAME: {
                    "type": "http",
                    "url": airport.url,
                    "path": "./proxy_providers/%s-%s.yaml" % (PROVIDER_NAME, airport.digest),
                    "interval": 0,
                }
            }
    return rebuilt


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
    for composition in variants.values():
        if not isinstance(composition, dict) or set(composition) != {"overrides"}:
            raise ValueError("variant composition must declare overrides only")
        if not _string_list(composition["overrides"]):
            raise ValueError("variant overrides must be a string list")
    if not _string_list(manifest.get("inject-node-groups")):
        raise ValueError("manifest inject-node-groups must be a string list")
    return manifest


def _load_override(template_root, name):
    override = _load_mapping(template_root / "variants" / ("%s.yaml" % name))
    if set(override) & _OVERRIDE_FORBIDDEN_KEYS:
        raise ValueError("override must not redefine nodes or composition controls")
    return override


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


def _inject_proxy_names(document, injections, source_names, provider_name=None):
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
    if provider_name is None:
        return
    # Groups that declared the full node supply ("all" injections) and groups
    # that declared ``include-all`` receive the airport supply through the
    # provider's use list instead of inline airport node names.
    provider_indexes = set()
    for group_name, source_name in injections.items():
        if source_name == "all":
            matching = indexes.get(group_name, [])
            if len(matching) == 1:
                provider_indexes.add(matching[0])
    for index, group in enumerate(groups):
        if group.get("include-all") is True:
            provider_indexes.add(index)
    for index in provider_indexes:
        uses = groups[index].setdefault("use", [])
        if not isinstance(uses, list):
            raise ValueError("proxy group use entries must be a list")
        if provider_name not in uses:
            uses.append(provider_name)


def _string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
