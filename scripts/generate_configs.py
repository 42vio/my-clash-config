from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode


@dataclass(frozen=True)
class TemplateSpec:
    template_name: str
    output_name: str


MARKERS = {
    "proxies": "{{ PRIVATE_PROXIES }}",
    "proxy_groups": "{{ PRIVATE_PROXY_GROUPS }}",
    "rules": "{{ PRIVATE_RULES }}",
}


def build_provider_url(converter_base_url: str, source_url: str) -> str:
    base = converter_base_url.rstrip("/")
    if not base or not source_url.strip():
        raise ValueError("converter base URL and source URL are required")
    return f"{base}/sub?{urlencode({'target': 'clash', 'list': 'true', 'url': source_url.strip()})}"


def render_template(template: str, provider_url: str, fragments: dict[str, str]) -> str:
    result = template.replace("{{ SUBSCRIPTION_PROVIDER_URL }}", provider_url)
    for key, marker in MARKERS.items():
        result = result.replace(marker, fragments.get(key, ""))
    if "{{" in result or "}}" in result:
        raise ValueError("template contains an unknown marker")
    return result
