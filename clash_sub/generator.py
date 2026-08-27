import copy
import re
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined, meta

from clash_sub.domain import MEMBER_VARIANTS
from clash_sub.sources import merge_proxy_sources


_JINJA_PATTERN = re.compile(r"[^0-9A-Za-z]+")


def _template_environment():
    environment = Environment(undefined=StrictUndefined, autoescape=False)
    environment.globals = {}
    environment.filters = {}
    environment.tests = {}
    return environment


def _variant_root_marker(root_key):
    normalized = _JINJA_PATTERN.sub("_", root_key).strip("_").upper()
    if not normalized:
        raise ValueError("root key cannot be converted into a marker")
    return "VARIANT_%s_ROOT_YAML" % normalized


def _dump_root_yaml(root_key, value):
    return yaml.safe_dump(
        {root_key: value},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()


def _template_markers(template_text):
    return set(meta.find_undeclared_variables(_template_environment().parse(template_text)))


def _render_text(template_text, context):
    return _template_environment().from_string(template_text).render(dict(context))


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
    template_text = (template_root / "clash.yaml.j2").read_text(encoding="utf-8")
    top_level, injections = _load_variant(template_root, variant)
    _inject_proxy_names(top_level, injections, _source_proxy_names(sources, proxies))
    context = {"PROXIES_ROOT_YAML": _dump_root_yaml("proxies", proxies)}
    for root_key, value in top_level.items():
        context[_variant_root_marker(root_key)] = _dump_root_yaml(root_key, value)
    rendered = _render_text(template_text, context)
    _require_expected_markers(template_text, context)
    if not isinstance(yaml.safe_load(rendered), dict):
        raise ValueError("rendered template must be a mapping")
    return rendered if rendered.endswith("\n") else rendered + "\n"


def _load_variant(template_root, variant):
    document = yaml.safe_load(
        (template_root / "variants" / ("%s.yaml" % variant)).read_text(encoding="utf-8")
    )
    if not isinstance(document, dict):
        raise ValueError("variant must be a mapping")
    document = copy.deepcopy(document)
    generator = document.pop("_generator", None)
    if not isinstance(generator, dict):
        raise ValueError("variant metadata must be a mapping")
    node_groups = generator.get("inject-node-groups")
    if not isinstance(node_groups, list) or not all(
        isinstance(group, str) for group in node_groups
    ):
        raise ValueError("variant inject-node-groups must be a string list")
    home_groups = generator.get("inject-home-node-groups", [])
    if not isinstance(home_groups, list) or not all(isinstance(group, str) for group in home_groups):
        raise ValueError("variant inject-home-node-groups must be a string list")
    injections = {group: "all" for group in node_groups}
    injections.update({group: "home" for group in home_groups})
    return document, injections


def _source_proxy_names(sources, proxies):
    names = {"all": [proxy["name"] for proxy in proxies]}
    index = 0
    for label, source in sources:
        count = len(source)
        names[label] = [proxy["name"] for proxy in proxies[index : index + count]]
        index += count
    names.setdefault("home", [])
    return names


def _inject_proxy_names(top_level, injections, source_names):
    groups = top_level.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError("proxy-groups must exist before node injection")
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


def _require_expected_markers(template_text, context):
    markers = _template_markers(template_text)
    if markers != set(context):
        raise ValueError("template markers do not match rendering context")
