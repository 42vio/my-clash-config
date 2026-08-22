import copy
from pathlib import Path

import yaml

from clash_sub.domain import MEMBER_VARIANTS
from clash_sub.rendering import (
    dump_root_yaml,
    load_variant,
    render_text,
    template_markers,
    variant_root_marker,
)
from clash_sub.sources import merge_proxy_sources


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
    owner_sources_with_home = owner_sources + (("home", home),)
    return (
        ("balanced", owner_sources_with_home),
        ("standard", owner_sources),
        ("privacy", owner_sources_with_home),
    )


def _render_variant(template_root, variant, sources):
    proxies = merge_proxy_sources(sources)
    template_text = (template_root / "clash.yaml.j2").read_text(encoding="utf-8")
    variant_spec = load_variant(template_root, variant)
    top_level = copy.deepcopy(dict(variant_spec.top_level))
    _inject_proxy_names(top_level, variant_spec.inject_node_groups, proxies)
    context = {"PROXIES_ROOT_YAML": dump_root_yaml("proxies", proxies)}
    for root_key, value in top_level.items():
        context[variant_root_marker(root_key)] = dump_root_yaml(root_key, value)
    rendered = render_text(template_text, context)
    _require_expected_markers(template_text, context)
    if not isinstance(yaml.safe_load(rendered), dict):
        raise ValueError("rendered template must be a mapping")
    return rendered if rendered.endswith("\n") else rendered + "\n"


def _inject_proxy_names(top_level, group_names, proxies):
    groups = top_level.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError("proxy-groups must exist before node injection")
    indexes = {}
    for index, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            raise ValueError("proxy-groups entries must have names")
        indexes.setdefault(group["name"], []).append(index)
    for group_name in group_names:
        matching = indexes.get(group_name, [])
        if len(matching) != 1:
            raise ValueError("inject-node-group %r must exist exactly once" % group_name)
        targets = groups[matching[0]].setdefault("proxies", [])
        if not isinstance(targets, list):
            raise ValueError("inject-node-group %r must expose proxies" % group_name)
        for proxy in proxies:
            name = proxy.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("proxy entries must have names")
            if name not in targets:
                targets.append(name)


def _require_expected_markers(template_text, context):
    markers = template_markers(template_text)
    if markers != set(context):
        raise ValueError("template markers do not match rendering context")
