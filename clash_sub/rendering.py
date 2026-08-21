from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, Tuple

import yaml
from jinja2 import Environment, StrictUndefined, meta


_JINJA_PATTERN = re.compile(r"[^0-9A-Za-z]+")


@dataclass(frozen=True)
class VariantSpec:
    name: str
    top_level: Mapping[str, object]
    inject_node_groups: Tuple[str, ...]


def build_environment() -> Environment:
    environment = Environment(undefined=StrictUndefined, autoescape=False)
    environment.globals = {}
    environment.filters = {}
    environment.tests = {}
    return environment


def safe_root_key(root_key: str) -> str:
    normalized = _JINJA_PATTERN.sub("_", root_key).strip("_").upper()
    if not normalized:
        raise ValueError("root key cannot be converted into a marker")
    return normalized


def variant_root_marker(root_key: str) -> str:
    return "VARIANT_%s_ROOT_YAML" % safe_root_key(root_key)


def dump_yaml_block(value: object, indent: int = 0) -> str:
    text = yaml.safe_dump(
        value,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()
    prefix = " " * indent
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def dump_root_yaml(root_key: str, value: object) -> str:
    return dump_yaml_block({root_key: value})


def private_proxies(value: Mapping[str, object] | Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    if isinstance(value, Mapping):
        proxies = value.get("proxies")
        if not isinstance(proxies, list):
            raise ValueError("private proxy snapshot must contain a proxies list")
        source = proxies
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        source = value
    else:
        raise ValueError("private proxies must be a mapping or sequence")
    proxies: list[Mapping[str, object]] = []
    for proxy in source:
        if not isinstance(proxy, Mapping):
            raise ValueError("proxy entries must be mappings")
        proxies.append(copy.deepcopy(proxy))
    return proxies


def load_variant(template_dir: Path, variant: str) -> VariantSpec:
    document = yaml.safe_load((template_dir / "variants" / ("%s.yaml" % variant)).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("variant must be a mapping")
    document = copy.deepcopy(document)
    generator = document.pop("_generator", None)
    if not isinstance(generator, dict):
        raise ValueError("variant metadata must be a mapping")
    inject_node_groups = generator.get("inject-node-groups")
    if not isinstance(inject_node_groups, list) or not all(isinstance(item, str) for item in inject_node_groups):
        raise ValueError("variant inject-node-groups must be a string list")
    return VariantSpec(
        name=variant,
        top_level=document,
        inject_node_groups=tuple(inject_node_groups),
    )


def template_markers(template_text: str) -> set[str]:
    environment = build_environment()
    parsed = environment.parse(template_text)
    return set(meta.find_undeclared_variables(parsed))


def render_text(template_text: str, context: Mapping[str, object]) -> str:
    environment = build_environment()
    return environment.from_string(template_text).render(dict(context))


def render_variant(
    template_dir: Path,
    variant: str,
    private_proxy_snapshot: Mapping[str, object] | Sequence[Mapping[str, object]],
) -> str:
    template_text = (template_dir / "clash.yaml.j2").read_text(encoding="utf-8")
    markers = template_markers(template_text)
    variant_spec = load_variant(template_dir, variant)
    top_level = copy.deepcopy(dict(variant_spec.top_level))
    proxies = private_proxies(private_proxy_snapshot)
    proxy_names = []
    seen_proxy_names = set()
    for proxy in proxies:
        name = proxy.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("proxy entries must have names")
        if name in seen_proxy_names:
            raise ValueError("proxy names must be unique")
        seen_proxy_names.add(name)
        proxy_names.append(name)
    groups = top_level.get("proxy-groups")
    if variant_spec.inject_node_groups:
        if not isinstance(groups, list):
            raise ValueError("proxy-groups must exist before node injection")
        matching = {}
        for index, group in enumerate(groups):
            if not isinstance(group, dict):
                raise ValueError("proxy-groups entries must be mappings")
            name = group.get("name")
            if isinstance(name, str):
                matching.setdefault(name, []).append(index)
        for group_name in variant_spec.inject_node_groups:
            indexes = matching.get(group_name, [])
            if len(indexes) != 1:
                raise ValueError("inject-node-group %r must exist exactly once" % group_name)
            group = groups[indexes[0]]
            group_proxies = group.setdefault("proxies", [])
            if not isinstance(group_proxies, list):
                raise ValueError("inject-node-group %r must expose a proxies list" % group_name)
            for proxy_name in proxy_names:
                if proxy_name not in group_proxies:
                    group_proxies.append(proxy_name)
    context = {"PROXIES_ROOT_YAML": dump_root_yaml("proxies", list(copy.deepcopy(proxies)))}
    for root_key, value in top_level.items():
        marker = variant_root_marker(root_key)
        if marker not in markers:
            continue
        context[marker] = dump_root_yaml(root_key, value)
    if markers != set(context):
        raise ValueError("template markers do not match rendering context")
    rendered = render_text(template_text, context)
    parsed = yaml.safe_load(rendered)
    if not isinstance(parsed, dict):
        raise ValueError("rendered template must be a mapping")
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered
