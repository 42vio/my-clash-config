"""Compose fixed Clash profiles from round-trip template documents."""

import copy
from collections.abc import Mapping
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from clash_sub.domain import MEMBER_VARIANTS, OWNER_VARIANTS, HomeOverlay
from clash_sub.sources import (
    HomeSourceError,
    merge_proxy_sources_with_aliases,
    rule_is_terminal,
)
from clash_sub.yaml_rt import (
    RoundTripYamlError,
    clone_round_trip,
    copy_key_comments,
    dump_round_trip,
    load_round_trip,
)


PROVIDER_NAME = "AmyTelecom"
HOME_SOURCE_LABEL = "home"
_HOME_VARIANTS = frozenset({"compat-office", "balance-office"})
_APPROVED_DNS = frozenset({"compat", "balance-office"})
_PROFILE_RECIPES = {
    "compat-office": ("compat", True),
    "compat-universal": ("compat", False),
    "balance-office": ("balance-office", True),
}


def render_user_bundle(is_owner, xui, airport, home, template_root):
    """Render only the profiles and sources authorized for one user."""
    if home is not None and not isinstance(home, HomeOverlay):
        raise ValueError("home source must be a private home overlay")
    sources_by_variant = _authorized_sources(is_owner, xui, airport, home)
    root = Path(template_root)
    return {
        variant: _render_variant(
            root,
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
        if home is not None:
            raise ValueError("member profiles must not reference a home overlay")
        return ((MEMBER_VARIANTS[0], (xui_source,)),)
    if airport is None:
        raise ValueError("owner profiles require the airport provider")
    owner_sources = (xui_source,)
    office_sources = (
        owner_sources + ((HOME_SOURCE_LABEL, list(home.proxies)),)
        if home is not None
        else owner_sources
    )
    return (
        ("compat-office", office_sources),
        ("compat-universal", owner_sources),
        ("balance-office", office_sources),
    )


def _render_variant(template_root, variant, sources, airport, home):
    proxies, source_aliases = _merge_proxy_documents(sources, home)
    document, injections = _compose_variant(template_root, variant)
    document["proxies"] = proxies
    if airport is not None:
        _with_provider(document, airport)
    if home is not None:
        _copy_home_key_comments(document, home)
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
    try:
        return dump_round_trip(document)
    except RoundTripYamlError:
        raise ValueError("rendered template could not be serialized") from None


def _merge_proxy_documents(sources, home):
    """Merge sources while retaining round-trip mapping and sequence comments."""
    merged, source_aliases = merge_proxy_sources_with_aliases(sources)
    result = CommentedSeq()
    offset = 0
    for label, source in sources:
        source_items = tuple(source)
        source_sequence = None
        if label == HOME_SOURCE_LABEL and home is not None:
            home_document = getattr(home, "document", None)
            if isinstance(home_document, Mapping):
                candidate = home_document.get("proxies")
                if isinstance(candidate, list):
                    source_sequence = candidate
        for index, original in enumerate(source_items):
            copied = clone_round_trip(original)
            if not isinstance(copied, Mapping):
                copied = clone_round_trip(merged[offset])
            else:
                copied["name"] = merged[offset]["name"]
            result.append(copied)
            if source_sequence is not None:
                _copy_sequence_item_comment(
                    source_sequence, index, result, offset
                )
                if index == 0:
                    _attach_sequence_comments(
                        result,
                        offset,
                        _leading_sequence_comments(home_document, "proxies"),
                    )
                if getattr(source_sequence.ca, "end", None) is not None:
                    result.ca.end = copy.deepcopy(source_sequence.ca.end)
            offset += 1
    return result, source_aliases


def _apply_home_overlay(document, home, source_aliases):
    """Compose the private home overlay into one office document."""
    groups = document.get("proxy-groups")
    if not isinstance(groups, list):
        _home_fail("home_group_invalid")
    known = {
        group.get("name") for group in groups if isinstance(group, Mapping)
    }
    home_group_names = set()
    home_document = getattr(home, "document", None)
    home_groups = (
        home_document.get("proxy-groups")
        if isinstance(home_document, Mapping)
        else None
    )
    for group_index, group in enumerate(home.proxy_groups):
        name = group.get("name") if isinstance(group, Mapping) else None
        if not isinstance(name, str) or not name.strip() or name in known:
            _home_fail("home_group_invalid")
        copied = clone_round_trip(group)
        if not isinstance(copied, Mapping):
            _home_fail("home_group_invalid")
        members = copied.get("proxies")
        if isinstance(members, list):
            for member_index, member in enumerate(members):
                if isinstance(member, str):
                    members[member_index] = source_aliases.get(member, member)
        target_index = len(groups)
        groups.append(copied)
        if isinstance(home_groups, list):
            _copy_sequence_item_comment(home_groups, group_index, groups, target_index)
            if group_index == 0:
                _attach_sequence_comments(
                    groups,
                    target_index,
                    _leading_sequence_comments(home_document, "proxy-groups"),
                )
        known.add(name)
        home_group_names.add(name)

    by_name = {
        group["name"]: group
        for group in groups
        if isinstance(group, Mapping) and "name" in group
    }
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
    home_rules = (
        home_document.get("rules")
        if isinstance(home_document, Mapping)
        else home.rules
    )
    document["rules"] = _concat_sequences(home_rules, public_rules)
    if isinstance(home_rules, list):
        _attach_sequence_comments(
            document["rules"],
            0,
            _leading_sequence_comments(home_document, "rules"),
        )

    declared = list(home.inject_node_groups) + list(home.inject_home_node_groups)
    if len(set(declared)) != len(declared):
        _home_fail("home_group_reference_invalid")
    for group in declared:
        if group not in home_group_names:
            _home_fail("home_group_reference_invalid")
    injections = {group: "all" for group in home.inject_node_groups}
    injections.update({group: "home" for group in home.inject_home_node_groups})
    return injections


def _copy_home_key_comments(document, home):
    """Copy only home section key comments into an office document."""
    source = getattr(home, "document", None)
    if not isinstance(source, Mapping):
        return
    source_comments = getattr(getattr(source, "ca", None), "items", {})
    if not source_comments:
        return
    target_comments = getattr(getattr(document, "ca", None), "items", {})
    original_root = copy.deepcopy(getattr(document.ca, "comment", None))
    original_slots = {
        key: copy.deepcopy(target_comments.get(key))
        for key in ("proxies", "proxy-groups", "rules")
    }
    prepared_slots = {
        key: _key_comment_slot(source, key)
        for key in ("proxies", "proxy-groups", "rules")
    }
    copy_key_comments(source, "proxies", document, "proxies")
    if prepared_slots["proxies"] is not None:
        target_comments["proxies"] = prepared_slots["proxies"]
    for key in ("proxy-groups", "rules"):
        if prepared_slots[key] is not None:
            target_comments[key] = prepared_slots[key]
    for key, original in original_slots.items():
        incoming = prepared_slots[key]
        if original is not None and incoming is not None:
            target_comments[key] = _merge_comment_values(original, incoming)
    home_root = copy.deepcopy(getattr(source.ca, "comment", None))
    if original_root is not None and home_root is not None:
        document.ca.comment = _merge_comment_values(original_root, home_root)
    elif original_root is not None:
        document.ca.comment = original_root


def _concat_sequences(first, second):
    result = CommentedSeq()
    offset = 0
    for source in (first, second):
        if not isinstance(source, (list, tuple)):
            continue
        for index, value in enumerate(source):
            result.append(clone_round_trip(value))
            _copy_sequence_item_comment(source, index, result, offset)
            offset += 1
        if getattr(getattr(source, "ca", None), "end", None) is not None:
            result.ca.end = copy.deepcopy(source.ca.end)
    return result


def _copy_sequence_item_comment(source, source_index, target, target_index):
    source_items = getattr(getattr(source, "ca", None), "items", {})
    comments = source_items.get(source_index)
    if comments is not None:
        target.ca.items[target_index] = copy.deepcopy(comments)


def _key_comment_slot(source, key):
    """Return a key comment slot with sequence-leading comments removed."""
    source_items = getattr(getattr(source, "ca", None), "items", {})
    comments = source_items.get(key)
    if comments is None:
        return None
    result = copy.deepcopy(comments)
    if isinstance(result, list) and len(result) > 3:
        if isinstance(result[3], list) and result[3]:
            result[3] = None
        if hasattr(result[2], "value") and isinstance(result[2].value, str):
            lines = result[2].value.splitlines(keepends=True)
            if len(lines) > 1:
                result[2].value = lines[0]
    return result


def _leading_sequence_comments(source, key):
    """Extract comments that precede the first item of a source sequence."""
    source_items = getattr(getattr(source, "ca", None), "items", {})
    comments = source_items.get(key)
    if comments is None:
        return ()
    result = []
    if isinstance(comments, list) and len(comments) > 3:
        if isinstance(comments[3], list):
            result.extend(copy.deepcopy(comments[3]))
        if hasattr(comments[2], "value") and isinstance(comments[2].value, str):
            lines = comments[2].value.splitlines(keepends=True)
            if len(lines) > 1:
                token = copy.deepcopy(comments[2])
                token.value = "".join(lines[1:])
                token.column = 0
                result.append(token)
    return tuple(result)


def _attach_sequence_comments(target, target_index, comments):
    """Attach leading comments to one sequence item without dropping inline comments."""
    if not comments:
        return
    slots = target.ca.items.get(target_index)
    if slots is None:
        slots = [None, None, None, None]
        target.ca.items[target_index] = slots
    existing = slots[1]
    if existing is None:
        slots[1] = list(copy.deepcopy(comments))
    elif isinstance(existing, list):
        slots[1] = list(copy.deepcopy(comments)) + existing
    else:
        slots[1] = list(copy.deepcopy(comments)) + [existing]


def _merge_comment_values(existing, incoming):
    """Append two ruamel comment slots without dropping either value."""
    result = copy.deepcopy(existing)
    existing_text = _comment_text(result)
    incoming_text = _comment_text(incoming)
    if existing_text and incoming_text:
        existing_lines = set(existing_text.splitlines())
        incoming_text = "".join(
            line
            for line in incoming_text.splitlines(keepends=True)
            if line.rstrip("\r\n") not in existing_lines
        )
    if not incoming_text:
        return result
    token = _last_comment_token(result)
    if token is not None:
        if token.value and not token.value.endswith("\n"):
            token.value += "\n"
        token.value += incoming_text
        return result
    if isinstance(result, list):
        result.append(copy.deepcopy(incoming))
        return result
    return copy.deepcopy(incoming)


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


def _home_fail(code):
    raise HomeSourceError(code)


def _with_provider(document, airport):
    if "proxy-providers" in document:
        raise ValueError("public template must not carry a provider")
    provider = CommentedMap()
    provider["type"] = "http"
    provider["url"] = airport.url
    provider["path"] = "./proxy_providers/%s-%s.yaml" % (
        PROVIDER_NAME,
        airport.digest,
    )
    provider["interval"] = 0
    providers = CommentedMap()
    providers[PROVIDER_NAME] = provider
    try:
        index = list(document).index("proxies") + 1
    except ValueError:
        raise ValueError("public template must declare proxies") from None
    document.insert(index, "proxy-providers", providers)


def _compose_variant(template_root: Path, variant: str) -> tuple[CommentedMap, dict[str, str]]:
    root = Path(template_root)
    if variant not in OWNER_VARIANTS:
        raise ValueError("unknown profile")
    document = _load_round_trip(root / "base" / "compat-office.yaml")
    if document.get("proxies") != []:
        raise ValueError("public template must carry an empty proxies list")
    if "proxy-providers" in document:
        raise ValueError("public template must not carry a provider")
    manifest = _load_manifest(root / "profiles.yaml")
    recipe = manifest["profiles"][variant]
    if recipe["dns"] == "balance-office":
        balance = _load_round_trip(root / "dns" / "balance-office.yaml")
        if set(balance) != {"dns"} or not isinstance(balance.get("dns"), Mapping):
            raise ValueError("balance DNS template must contain only dns")
        base_root_comment = copy.deepcopy(getattr(document.ca, "comment", None))
        document["dns"] = clone_round_trip(balance["dns"])
        copy_key_comments(balance, "dns", document, "dns")
        balance_root_comment = copy.deepcopy(getattr(document.ca, "comment", None))
        if base_root_comment is not None and balance_root_comment is not None:
            document.ca.comment = _merge_comment_values(
                base_root_comment, balance_root_comment
            )
        elif base_root_comment is not None:
            document.ca.comment = base_root_comment
    injections = {group: "all" for group in manifest["inject-node-groups"]}
    return document, injections


def _load_round_trip(path):
    try:
        return load_round_trip(Path(path).read_bytes())
    except (OSError, UnicodeError, RoundTripYamlError):
        raise ValueError("template file could not be read") from None


def _load_manifest(path):
    manifest = _load_round_trip(path)
    if set(manifest) != {"profiles", "inject-node-groups"}:
        raise ValueError("profile manifest shape is invalid")
    profiles = manifest.get("profiles")
    if not isinstance(profiles, Mapping) or set(profiles) != set(OWNER_VARIANTS):
        raise ValueError("profile manifest profiles are invalid")
    for name in OWNER_VARIANTS:
        recipe = profiles[name]
        expected_dns, expected_home = _PROFILE_RECIPES[name]
        if (
            not isinstance(recipe, Mapping)
            or set(recipe) != {"dns", "home"}
            or recipe.get("dns") not in _APPROVED_DNS
            or recipe.get("dns") != expected_dns
            or type(recipe.get("home")) is not bool
            or recipe.get("home") is not expected_home
        ):
            raise ValueError("profile recipe is invalid")
    groups = manifest.get("inject-node-groups")
    if (
        not isinstance(groups, list)
        or any(not isinstance(group, str) or not group.strip() for group in groups)
        or len(set(groups)) != len(groups)
    ):
        raise ValueError("profile injection list is invalid")
    return manifest


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
    groups = document.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError("proxy-groups must be a list")
    indexes = {}
    for index, group in enumerate(groups):
        if not isinstance(group, Mapping) or not isinstance(group.get("name"), str):
            raise ValueError("proxy-groups entries must have names")
        indexes.setdefault(group["name"], []).append(index)
    for group_name, source_name in injections.items():
        matching = indexes.get(group_name, [])
        if len(matching) != 1:
            raise ValueError("inject-node-group must exist exactly once")
        targets = groups[matching[0]].setdefault("proxies", CommentedSeq())
        if not isinstance(targets, list):
            raise ValueError("inject-node-group must expose proxies")
        if source_name not in source_names:
            raise ValueError("inject-node-group references unknown source")
        for name in source_names[source_name]:
            if name not in targets:
                targets.append(name)
    if provider_name is None:
        return
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
        uses = groups[index].setdefault("use", CommentedSeq())
        if not isinstance(uses, list):
            raise ValueError("proxy group use entries must be a list")
        if provider_name not in uses:
            uses.append(provider_name)
