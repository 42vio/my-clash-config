from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode


BASE_TEMPLATE_NAME = "_base.yaml.tmpl"
PARTS_DIR_NAME = "parts"


@dataclass(frozen=True)
class TemplateSpec:
    output_name: str
    dns_part: str
    geoip_part: str


MARKERS = {
    "proxies": "{{ PRIVATE_PROXIES }}",
    "proxy_groups": "{{ PRIVATE_PROXY_GROUPS }}",
    "rules": "{{ PRIVATE_RULES }}",
    "dns": "{{ DNS_VARIANT }}",
    "geoip": "{{ GEOIP_VARIANT }}",
}

TEMPLATES = (
    TemplateSpec(output_name="My-Clash_Balanced.yaml", dns_part="dns-balanced.part", geoip_part="geoip-resolve.part"),
    TemplateSpec(output_name="My-Clash_Balanced_Win.yaml", dns_part="dns-balanced.part", geoip_part="geoip-resolve.part"),
    TemplateSpec(output_name="My-Clash_Privacy.yaml", dns_part="dns-privacy.part", geoip_part="geoip-no-resolve.part"),
)


def build_provider_url(converter_base_url: str, source_url: str) -> str:
    base = converter_base_url.rstrip("/")
    if not base or not source_url.strip():
        raise ValueError("converter base URL and source URL are required")
    return f"{base}/sub?{urlencode({'target': 'clash', 'list': 'true', 'url': source_url.strip()})}"


def render_template(
    template: str, provider_url: str, fragments: dict[str, str], variants: dict[str, str]
) -> str:
    result = template.replace("{{ SUBSCRIPTION_PROVIDER_URL }}", provider_url)
    values = {**fragments, **variants}
    for key, marker in MARKERS.items():
        result = result.replace(marker, values.get(key, ""))
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


def load_part(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path.name)
    return path.read_text(encoding="utf-8").rstrip("\n")


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
    base_path = template_dir / BASE_TEMPLATE_NAME
    if not base_path.is_file():
        raise FileNotFoundError(base_path.name)
    base_template = base_path.read_text(encoding="utf-8")
    parts_dir = template_dir / PARTS_DIR_NAME
    rendered = []
    for spec in TEMPLATES:
        variants = {
            "dns": load_part(parts_dir / spec.dns_part),
            "geoip": load_part(parts_dir / spec.geoip_part),
        }
        rendered.append(
            (output_dir / spec.output_name, render_template(base_template, provider_url, fragments, variants))
        )
    for output_path, content in rendered:
        atomic_write(output_path, content)
    return [output_path for output_path, _ in rendered]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate personal Clash configurations from templates.")
    parser.add_argument("--source-url", required=True, help="3x-ui subscription URL; never stored in this repository")
    parser.add_argument("--converter-base-url", required=True, help="Public base URL of subconverter without /sub")
    parser.add_argument("--output-dir", type=Path, default=Path("generated"))
    parser.add_argument("--private-dir", type=Path, default=Path("private"))
    parser.add_argument("--private", action="store_true", help="Require and inject all private YAML fragments")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        outputs = generate_configs(
            Path(__file__).resolve().parents[1] / "templates",
            args.output_dir,
            args.converter_base_url,
            args.source_url,
            args.private_dir if args.private else None,
            args.private,
        )
    except (FileNotFoundError, ValueError) as error:
        print(f"generation failed: {error}", file=sys.stderr)
        return 1
    if not args.private:
        print("notice: 未注入私有节点 (private nodes not injected; add --private after filling private/*.yaml)")
    for output in outputs:
        print(f"generated {output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
