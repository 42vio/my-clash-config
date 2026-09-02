"""Compose the fixed Compat and Balance Clash profiles."""

import copy
from collections.abc import Mapping
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from clash_sub.domain import (
    AIRPORT_FILENAME,
    AirportProvider,
    MEMBER_VARIANTS,
    OWNER_VARIANTS,
)
from clash_sub.sources import merge_proxy_sources
from clash_sub.yaml_rt import (
    RoundTripYamlError,
    clone_round_trip,
    copy_key_comments,
    dump_round_trip,
    load_round_trip,
)


PROVIDER_NAME = "AmyTelecom"
_PROFILE_RECIPES = {"compat": "compat", "balance": "balance"}


def render_user_bundle(
    is_owner: bool,
    xui: list[Mapping],
    airport: AirportProvider | None,
    template_root: Path,
) -> dict[str, str]:
    """Render only the profile variants authorized for one user."""
    if not is_owner and airport is not None:
        raise ValueError("member profiles must not reference an airport provider")
    if is_owner and airport is None:
        raise ValueError("owner profiles require the airport provider")
    variants = OWNER_VARIANTS if is_owner else MEMBER_VARIANTS
    return {variant: _render_variant(template_root, variant, xui, airport) for variant in variants}


def _render_variant(template_root, variant, xui, airport):
    document, injections = _compose_variant(template_root, variant)
    document["proxies"] = CommentedSeq(merge_proxy_sources((("3x-ui", xui),)))
    if airport is not None:
        _with_provider(document, airport)
    _inject_proxy_names(document, injections, airport is not None)
    if airport is None:
        _drop_unusable_groups(document)
    try:
        return dump_round_trip(document)
    except RoundTripYamlError:
        raise ValueError("rendered template could not be serialized") from None


def _drop_unusable_groups(document):
    """Member profiles drop groups left empty after airport removal.

    Mihomo rejects a group whose proxies and use are both empty, so the
    provider-only groups — and anything that only referenced them —
    collapse instead of shipping an unloadable configuration.
    """
    groups = document["proxy-groups"]
    dropped = set()
    changed = True
    while changed:
        changed = False
        for index in range(len(groups)):
            group = groups[index]
            members = group.get("proxies")
            if isinstance(members, list) and dropped:
                kept = [member for member in members if member not in dropped]
                if len(kept) != len(members):
                    group["proxies"] = CommentedSeq(kept)
                    members = kept
            if members or group.get("use") or group.get("include-all") is True:
                continue
            del groups[index]
            dropped.add(group["name"])
            changed = True
            break
    return dropped


def _with_provider(document, airport):
    providers = document.get("proxy-providers")
    if providers is None:
        providers = CommentedMap()
    elif not isinstance(providers, Mapping) or PROVIDER_NAME in providers:
        raise ValueError("public template provider mapping is invalid")
    providers[PROVIDER_NAME] = CommentedMap({
        "type": "http",
        "url": airport.url,
        "interval": 86400,
        "path": "./proxy_providers/%s" % AIRPORT_FILENAME,
    })
    try:
        index = list(document).index("proxies") + 1
    except ValueError:
        raise ValueError("public template must declare proxies") from None
    if "proxy-providers" not in document:
        document.insert(index, "proxy-providers", providers)


def _compose_variant(template_root: Path, variant: str) -> tuple[CommentedMap, dict[str, str]]:
    if variant not in OWNER_VARIANTS:
        raise ValueError("unknown profile")
    root = Path(template_root)
    document = _load_round_trip(root / "base" / "Clash-Compat.yaml")
    if document.get("proxies") != []:
        raise ValueError("public template must carry an empty proxies list")
    manifest = _load_manifest(root / "profiles.yaml")
    if manifest["profiles"][variant]["dns"] == "balance":
        balance = _load_round_trip(root / "dns" / "Clash-Balance.yaml")
        if set(balance) != {"dns"} or not isinstance(balance.get("dns"), Mapping):
            raise ValueError("balance DNS template must contain only dns")
        base_root_comment = copy.deepcopy(getattr(document.ca, "comment", None))
        document["dns"] = clone_round_trip(balance["dns"])
        copy_key_comments(balance, "dns", document, "dns")
        balance_root_comment = copy.deepcopy(getattr(document.ca, "comment", None))
        if base_root_comment is not None and balance_root_comment is not None:
            document.ca.comment = _merge_comment_values(base_root_comment, balance_root_comment)
    injections = {group: "all" for group in manifest["inject-node-groups"]}
    for group in manifest["inject-provider-groups"]:
        injections[group] = "all-provider" if group in injections else "provider"
    return document, injections


def _load_round_trip(path):
    try:
        return load_round_trip(Path(path).read_bytes())
    except (OSError, UnicodeError, RoundTripYamlError):
        raise ValueError("template file could not be read") from None


def _load_manifest(path):
    manifest = _load_round_trip(path)
    if set(manifest) != {"profiles", "inject-node-groups", "inject-provider-groups"}:
        raise ValueError("profile manifest shape is invalid")
    profiles = manifest.get("profiles")
    if not isinstance(profiles, Mapping) or set(profiles) != set(OWNER_VARIANTS):
        raise ValueError("profile manifest profiles are invalid")
    for name, expected_dns in _PROFILE_RECIPES.items():
        recipe = profiles[name]
        if not isinstance(recipe, Mapping) or set(recipe) != {"dns"} or recipe.get("dns") != expected_dns:
            raise ValueError("profile recipe is invalid")
    for key in ("inject-node-groups", "inject-provider-groups"):
        groups = manifest.get(key)
        if not isinstance(groups, list) or any(not isinstance(group, str) or not group.strip() for group in groups) or len(set(groups)) != len(groups):
            raise ValueError("profile injection list is invalid")
    return manifest


def _inject_proxy_names(document, injections, has_provider):
    groups = document.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError("proxy-groups must be a list")
    indexes = {}
    for index, group in enumerate(groups):
        if not isinstance(group, Mapping) or not isinstance(group.get("name"), str):
            raise ValueError("proxy-groups entries must have names")
        indexes.setdefault(group["name"], []).append(index)
    proxy_names = [proxy["name"] for proxy in document["proxies"]]
    for group_name, source in injections.items():
        matches = indexes.get(group_name, [])
        if len(matches) != 1:
            raise ValueError("inject-node-group must exist exactly once")
        group = groups[matches[0]]
        if source != "provider":
            targets = group.setdefault("proxies", CommentedSeq())
            if not isinstance(targets, list):
                raise ValueError("inject-node-group must expose proxies")
            if source == "all-provider":
                source = "all"
            for name in proxy_names:
                if name not in targets:
                    targets.append(name)
        if has_provider and source in {"provider", "all-provider"}:
            uses = group.setdefault("use", CommentedSeq())
            if not isinstance(uses, list):
                raise ValueError("proxy group use entries must be a list")
            if PROVIDER_NAME not in uses:
                uses.append(PROVIDER_NAME)


def _merge_comment_values(existing, incoming):
    incoming_text = _comment_text(incoming)
    if not incoming_text:
        return existing
    token = _last_comment_token(existing)
    if token is not None:
        if token.value and not token.value.endswith("\n"):
            token.value += "\n"
        token.value += incoming_text
    return existing


def _last_comment_token(value):
    if hasattr(value, "value") and isinstance(value.value, str):
        return value
    if isinstance(value, (list, tuple)):
        for item in reversed(value):
            token = _last_comment_token(item)
            if token is not None:
                return token
    return None


def _comment_text(value):
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if isinstance(value, (list, tuple)):
        return "".join(_comment_text(item) for item in value)
    return ""
