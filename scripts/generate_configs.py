from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
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

TEMPLATES = (
    TemplateSpec("My-Clash_Balanced.yaml.tmpl", "My-Clash_Balanced.yaml"),
    TemplateSpec("My-Clash_Balanced_Win.yaml.tmpl", "My-Clash_Balanced_Win.yaml"),
    TemplateSpec("My-Clash_Privacy.yaml.tmpl", "My-Clash_Privacy.yaml"),
)


def build_provider_url(converter_base_url: str, source_url: str) -> str:
    base = converter_base_url.rstrip("/")
    if not base or not source_url.strip():
        raise ValueError("converter base URL and source URL are required")
    return f"{base}/sub?{urlencode({'target': 'clash', 'list': 'true', 'url': source_url.strip()})}"


def render_template(template: str, provider_url: str, fragments: dict[str, str]) -> str:
    result = template.replace("{{ SUBSCRIPTION_PROVIDER_URL }}", provider_url)
    for key, marker in MARKERS.items():
        result = result.replace(marker, fragments.get(key, ""))
    start = result.find("{{")
    if start != -1:
        snippet = result[start : start + 40]
        raise ValueError(f"template contains an unknown marker: {snippet!r}")
    return result


def load_private_fragments(private_dir: Path | None, require_private: bool) -> dict[str, str]:
    if private_dir is None:
        if require_private:
            raise FileNotFoundError("private directory is required")
        return {}
    filenames = {
        "proxies": "proxies.yaml",
        "proxy_groups": "proxy-groups.yaml",
        "rules": "rules.yaml",
    }
    paths = {key: private_dir / filename for key, filename in filenames.items()}
    missing = next((path for path in paths.values() if not path.is_file()), None)
    if missing:
        if require_private:
            raise FileNotFoundError(missing.name)
        return {}
    return {key: path.read_text(encoding="utf-8").rstrip() for key, path in paths.items()}


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def generate_configs(
    template_dir: Path,
    output_dir: Path,
    converter_base_url: str,
    source_url: str,
    private_dir: Path | None,
    require_private: bool,
) -> list[Path]:
    provider_url = build_provider_url(converter_base_url, source_url)
    fragments = load_private_fragments(private_dir, require_private)
    rendered = []
    for spec in TEMPLATES:
        template_path = template_dir / spec.template_name
        if not template_path.is_file():
            raise FileNotFoundError(template_path.name)
        rendered.append(
            (output_dir / spec.output_name, render_template(template_path.read_text(encoding="utf-8"), provider_url, fragments))
        )
    for output_path, content in rendered:
        atomic_write(output_path, content)
    return [output_path for output_path, _ in rendered]
