# Clash Subscription Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a Docker Compose-hosted `subconverter` + `sub-web` service and generate the Balanced, Balanced_Win, and Privacy Clash configurations from credential-free templates with optional local-only private node fragments.

**Architecture:** Docker Compose runs upstream `subconverter` and `sub-web` on loopback-only host ports. A dependency-free Python generator URL-encodes a 3x-ui subscription URL into a Clash Proxy Provider endpoint, fills three text templates, and optionally injects gitignored YAML fragments for private nodes, groups, and rules.

**Tech Stack:** Docker Compose, `tindy2013/subconverter`, `careywong/subweb`, Python 3 standard library (`argparse`, `dataclasses`, `pathlib`, `urllib.parse`, `unittest`), Ruby Psych for local YAML syntax verification.

**Spec:** `docs/superpowers/specs/2026-08-19-clash-subscription-service-design.md`

## Global Constraints

- Do not alter, stage, or delete the user-owned `README.md` deletion, `DNS 设计方案.md`, or the existing untracked root YAML files until their migration is explicitly complete and reviewed.
- Do not commit a real 3x-ui subscription URL, a node password, UUID, private-key, provider token, or a generated personal configuration.
- `subconverter` and `subweb` host ports must bind to `127.0.0.1`, never `0.0.0.0`.
- The online stack must not run Gist upload, short-link, configuration-upload, or `/getprofile` functionality.
- The generator must not require PyYAML or any third-party Python package.
- The three public templates must preserve their current DNS and final GEOIP behavior; private self-hosted nodes must exist only in ignored fragments and generated output.
- Python target is 3.9+ and each generated file must be written atomically so an existing valid output is never truncated by a failed generation.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `compose.yaml` | Runs the two upstream services, loopback port mappings, health checks, and a private Compose network with outbound access. |
| `.env.example` | Documents non-secret service ports and timezone without becoming a deployment secret store. |
| `docker/subconverter/pref.ini` | Enables safe subconverter API behavior without a default source URL or profile endpoint. |
| `templates/*.yaml.tmpl` | Credential-free source configuration for each of the three Clash variants. |
| `private/*.yaml.example` | Documented, safe-to-commit fragments that show the exact indentation and placement expected by templates. |
| `scripts/generate_configs.py` | Pure rendering functions plus the `--source-url`, `--converter-base-url`, `--private`, and `--output-dir` CLI. |
| `tests/test_generate_configs.py` | Standard-library unit tests for URL construction, marker replacement, private mode, and atomic output behavior. |
| `generated/.gitkeep` | Keeps the ignored output directory visible in Git. |
| `.gitignore` | Prevents private fragments, local env files, generated YAML, and Python cache files from being committed. |
| `DEPLOYMENT.md` | Local startup, reverse proxy, sub-web usage, personal generation, rotation, and verification instructions. |

## Interfaces

The generator exposes these Python interfaces for tests and CLI use:

```python
@dataclass(frozen=True)
class TemplateSpec:
    template_name: str
    output_name: str

def build_provider_url(converter_base_url: str, source_url: str) -> str: ...

def render_template(template: str, provider_url: str, fragments: dict[str, str]) -> str: ...

def generate_configs(
    template_dir: Path,
    output_dir: Path,
    converter_base_url: str,
    source_url: str,
    private_dir: Path | None,
    require_private: bool,
) -> list[Path]: ...
```

Markers in every template are exact literal strings:

```text
{{ SUBSCRIPTION_PROVIDER_URL }}
{{ PRIVATE_PROXIES }}
{{ PRIVATE_PROXY_GROUPS }}
{{ PRIVATE_RULES }}
```

When no private directory is requested, the three `PRIVATE_*` markers render to empty text. With `--private`, all three fragment files (`proxies.yaml`, `proxy-groups.yaml`, `rules.yaml`) must exist or the command exits before writing outputs.

### Task 1: Establish the test harness and pure provider URL rendering

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_generate_configs.py`
- Create: `scripts/generate_configs.py`

**Interfaces:**
- Consumes: the `TemplateSpec`, `build_provider_url`, and `render_template` interfaces above.
- Produces: tested pure functions used by later CLI and template tasks.

- [ ] **Step 1: Write the failing URL and marker tests**

Create `tests/test_generate_configs.py` with these tests before production code exists:

```python
import unittest

from scripts.generate_configs import build_provider_url, render_template


class RenderingTests(unittest.TestCase):
    def test_build_provider_url_encodes_source_and_requests_clash_provider(self):
        result = build_provider_url(
            "https://convert.example.com/",
            "https://panel.example/sub?token=a&name=测试",
        )

        self.assertTrue(result.startswith("https://convert.example.com/sub?"))
        self.assertIn("target=clash", result)
        self.assertIn("list=true", result)
        self.assertIn("url=https%3A%2F%2Fpanel.example%2Fsub%3Ftoken%3Da%26name%3D", result)
        self.assertNotIn("测试", result)

    def test_render_template_replaces_all_markers_without_private_fragments(self):
        template = """url: '{{ SUBSCRIPTION_PROVIDER_URL }}'\n{{ PRIVATE_PROXIES }}\n{{ PRIVATE_PROXY_GROUPS }}\n{{ PRIVATE_RULES }}\n"""

        result = render_template(template, "https://convert.example.com/sub?target=clash", {})

        self.assertNotIn("{{", result)
        self.assertIn("url: 'https://convert.example.com/sub?target=clash'", result)
```

- [ ] **Step 2: Run the tests to verify the expected failure**

Run: `python3 -m unittest tests.test_generate_configs -v`

Expected: FAIL with `ModuleNotFoundError` because `scripts/generate_configs.py` does not exist.

- [ ] **Step 3: Implement the minimal pure rendering module**

Create `scripts/generate_configs.py` with the following initial production code:

```python
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
```

- [ ] **Step 4: Run the tests to verify the pure functions pass**

Run: `python3 -m unittest tests.test_generate_configs -v`

Expected: PASS for both tests.

- [ ] **Step 5: Commit the tested rendering foundation**

```bash
git add scripts/generate_configs.py tests/__init__.py tests/test_generate_configs.py
git commit -m "feat: add subscription template renderer"
```

### Task 2: Add safe private-fragment loading and atomic three-file generation

**Files:**
- Modify: `scripts/generate_configs.py`
- Modify: `tests/test_generate_configs.py`

**Interfaces:**
- Consumes: `build_provider_url()` and `render_template()` from Task 1.
- Produces: `generate_configs()` and the CLI invoked in Task 5.

- [ ] **Step 1: Write failing generation tests using temporary directories**

Append these tests:

```python
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.generate_configs import generate_configs


def write_fixture_template(path: Path) -> None:
    path.write_text(
        "proxy-providers:\n"
        "  Subscribe:\n"
        "    type: http\n"
        "    url: '{{ SUBSCRIPTION_PROVIDER_URL }}'\n"
        "proxies:\n{{ PRIVATE_PROXIES }}\n"
        "proxy-groups:\n{{ PRIVATE_PROXY_GROUPS }}\n"
        "rules:\n{{ PRIVATE_RULES }}\n",
        encoding="utf-8",
    )


class GenerationTests(unittest.TestCase):
    def test_generate_configs_injects_private_fragments_only_into_outputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            private = root / "private"
            output = root / "output"
            templates.mkdir()
            private.mkdir()
            for name in ("My-Clash_Balanced", "My-Clash_Balanced_Win", "My-Clash_Privacy"):
                write_fixture_template(templates / f"{name}.yaml.tmpl")
            (private / "proxies.yaml").write_text("  - name: private-node\n", encoding="utf-8")
            (private / "proxy-groups.yaml").write_text("  - name: Private\n", encoding="utf-8")
            (private / "rules.yaml").write_text("  - MATCH,Private\n", encoding="utf-8")

            outputs = generate_configs(
                templates, output, "https://convert.example.com", "https://panel.example/sub", private, True
            )

            self.assertEqual([path.name for path in outputs], [
                "My-Clash_Balanced.yaml",
                "My-Clash_Balanced_Win.yaml",
                "My-Clash_Privacy.yaml",
            ])
            self.assertIn("private-node", outputs[0].read_text(encoding="utf-8"))
            self.assertNotIn("private-node", (templates / "My-Clash_Balanced.yaml.tmpl").read_text(encoding="utf-8"))

    def test_generate_configs_requires_every_private_fragment_before_writing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            private = root / "private"
            templates.mkdir()
            private.mkdir()
            for name in ("My-Clash_Balanced", "My-Clash_Balanced_Win", "My-Clash_Privacy"):
                write_fixture_template(templates / f"{name}.yaml.tmpl")
            (private / "proxies.yaml").write_text("  - name: private-node\n", encoding="utf-8")

            with self.assertRaisesRegex(FileNotFoundError, "proxy-groups.yaml"):
                generate_configs(templates, root / "output", "https://convert.example.com", "https://panel.example/sub", private, True)

            self.assertFalse((root / "output").exists())
```

- [ ] **Step 2: Run the new tests to verify the expected failure**

Run: `python3 -m unittest tests.test_generate_configs -v`

Expected: FAIL because `generate_configs` is not defined.

- [ ] **Step 3: Implement fragment validation and atomic output writes**

Extend `scripts/generate_configs.py` with exact template definitions and helpers:

```python
from pathlib import Path
from tempfile import NamedTemporaryFile


TEMPLATES = (
    TemplateSpec("My-Clash_Balanced.yaml.tmpl", "My-Clash_Balanced.yaml"),
    TemplateSpec("My-Clash_Balanced_Win.yaml.tmpl", "My-Clash_Balanced_Win.yaml"),
    TemplateSpec("My-Clash_Privacy.yaml.tmpl", "My-Clash_Privacy.yaml"),
)


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
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def generate_configs(template_dir, output_dir, converter_base_url, source_url, private_dir, require_private):
    provider_url = build_provider_url(converter_base_url, source_url)
    fragments = load_private_fragments(private_dir, require_private)
    rendered = []
    for spec in TEMPLATES:
        template_path = template_dir / spec.template_name
        if not template_path.is_file():
            raise FileNotFoundError(template_path.name)
        rendered.append((output_dir / spec.output_name, render_template(template_path.read_text(encoding="utf-8"), provider_url, fragments)))
    for output_path, content in rendered:
        atomic_write(output_path, content)
    return [output_path for output_path, _ in rendered]
```

- [ ] **Step 4: Run unit tests and inspect generated temporary output behavior**

Run: `python3 -m unittest tests.test_generate_configs -v`

Expected: PASS; all three names are returned, private fragments appear only in output, and a missing required fragment creates no output directory.

- [ ] **Step 5: Commit private rendering support**

```bash
git add scripts/generate_configs.py tests/test_generate_configs.py
git commit -m "feat: generate private Clash config overlays"
```

### Task 3: Convert the three current configurations into credential-free templates

**Files:**
- Create: `templates/My-Clash_Balanced.yaml.tmpl`
- Create: `templates/My-Clash_Balanced_Win.yaml.tmpl`
- Create: `templates/My-Clash_Privacy.yaml.tmpl`
- Create: `private/proxies.yaml.example`
- Create: `private/proxy-groups.yaml.example`
- Create: `private/rules.yaml.example`
- Modify: `tests/test_generate_configs.py`

**Interfaces:**
- Consumes: marker replacement and `TEMPLATES` from Tasks 1-2.
- Produces: three public template inputs accepted by `generate_configs()`.

- [ ] **Step 1: Write failing structure tests against the future real templates**

Add a `TemplateStructureTests(unittest.TestCase)` class:

```python
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates"


class TemplateStructureTests(unittest.TestCase):
    def test_all_templates_have_required_provider_and_private_markers(self):
        for name in (
            "My-Clash_Balanced.yaml.tmpl",
            "My-Clash_Balanced_Win.yaml.tmpl",
            "My-Clash_Privacy.yaml.tmpl",
        ):
            content = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
            self.assertIn("url: '{{ SUBSCRIPTION_PROVIDER_URL }}'", content)
            self.assertIn("{{ PRIVATE_PROXIES }}", content)
            self.assertIn("{{ PRIVATE_PROXY_GROUPS }}", content)
            self.assertIn("{{ PRIVATE_RULES }}", content)

    def test_dns_variant_rules_remain_distinct(self):
        balanced = (TEMPLATE_DIR / "My-Clash_Balanced.yaml.tmpl").read_text(encoding="utf-8")
        windows = (TEMPLATE_DIR / "My-Clash_Balanced_Win.yaml.tmpl").read_text(encoding="utf-8")
        privacy = (TEMPLATE_DIR / "My-Clash_Privacy.yaml.tmpl").read_text(encoding="utf-8")
        self.assertIn("respect-rules: true", balanced)
        self.assertIn("respect-rules: true", windows)
        self.assertNotIn("respect-rules:", privacy)
        self.assertIn("- GEOIP,CN,🎯 Direct", balanced)
        self.assertIn("- GEOIP,CN,🎯 Direct", windows)
        self.assertIn("- GEOIP,CN,🎯 Direct,no-resolve", privacy)
```

- [ ] **Step 2: Run the structure tests to verify the expected failure**

Run: `python3 -m unittest tests.test_generate_configs.TemplateStructureTests -v`

Expected: FAIL with `FileNotFoundError` because no `templates/` files exist yet.

- [ ] **Step 3: Create sanitized templates and private examples**

Use the current three root YAML files only as read-only migration sources. For each template:

1. Preserve the variant-specific DNS, anchors, public proxy groups, rule providers, and rules.
2. Replace the `Subscribe` provider with:

```yaml
  Subscribe:
    type: http
    url: '{{ SUBSCRIPTION_PROVIDER_URL }}'
    path: ./providers/Subscribe.yaml
    interval: 86400
    health-check:
      enable: true
      url: http://www.gstatic.com/generate_204
      interval: 1800
```

3. Replace every actual self-hosted proxy declaration beneath `proxies:` with the single indented marker:

```yaml
{{ PRIVATE_PROXIES }}
```

4. Do not retain personal domains, IP addresses, node names, UUIDs, passwords, public keys, SNI names, subscriptions, or private LAN ranges from the current root YAML files.
5. Remove public references to private-only groups. Public `g2` groups must use `[🎯 Direct, 加速线路]`; add private-only routes and groups through fragment markers instead.
6. Add this marker as the final item under `proxy-groups`, with two spaces of indentation supplied by the private fragment:

```yaml
{{ PRIVATE_PROXY_GROUPS }}
```

7. Add this marker immediately before the final `GEOIP` rule so private rules take precedence:

```yaml
{{ PRIVATE_RULES }}
```

Create examples containing only placeholders and no real credential:

```yaml
# private/proxies.yaml.example
  - name: Example Private Node
    type: vless
    server: private.example.com
    port: 443
    uuid: REPLACE_WITH_PRIVATE_UUID
    tls: true
```

```yaml
# private/proxy-groups.yaml.example
  - name: Private Relay
    type: select
    proxies:
      - 🎯 Direct
      - Example Private Node
```

```yaml
# private/rules.yaml.example
  - DOMAIN-SUFFIX,private.example.com,Private Relay
```

- [ ] **Step 4: Run generated-config tests and YAML syntax checks**

Run:

```bash
python3 -m unittest tests.test_generate_configs -v
CHECK_DIR="$(mktemp -d /tmp/clash-generated-check.XXXXXX)"
python3 scripts/generate_configs.py --source-url 'https://panel.example/sub?token=placeholder' --converter-base-url 'https://convert.example.com' --output-dir "$CHECK_DIR"
for file in "$CHECK_DIR"/*.yaml; do /usr/local/opt/ruby/bin/ruby -e 'require "yaml"; YAML.load_file(ARGV.fetch(0))' "$file"; done
```

Expected: tests PASS, each command produces valid YAML, and no output includes an unreplaced `{{ ... }}` marker.

- [ ] **Step 5: Commit templates and private examples**

```bash
git add templates private tests/test_generate_configs.py
git commit -m "feat: add public Clash configuration templates"
```

### Task 4: Add the loopback-only Docker Compose conversion stack

**Files:**
- Create: `compose.yaml`
- Create: `.env.example`
- Create: `docker/subconverter/pref.ini`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: upstream container ports `subconverter:25500` and `subweb:80`.
- Produces: `docker compose up -d` startup with externally configurable loopback port numbers.

- [ ] **Step 1: Write a failing static Compose-security test**

Add this test to `tests/test_generate_configs.py`:

```python
class ComposeSecurityTests(unittest.TestCase):
    def test_compose_binds_both_services_only_to_loopback(self):
        content = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:${SUBCONVERTER_PORT:-25500}:25500"', content)
        self.assertIn('"127.0.0.1:${SUBWEB_PORT:-58080}:80"', content)
        self.assertNotIn("0.0.0.0:", content)
```

- [ ] **Step 2: Run the test to verify the expected failure**

Run: `python3 -m unittest tests.test_generate_configs.ComposeSecurityTests -v`

Expected: FAIL with `FileNotFoundError` because `compose.yaml` does not yet exist.

- [ ] **Step 3: Create Compose, safe subconverter preference, and ignores**

Create `compose.yaml`:

```yaml
services:
  subconverter:
    image: tindy2013/subconverter:latest
    restart: unless-stopped
    volumes:
      - ./docker/subconverter/pref.ini:/base/pref.ini:ro
    ports:
      - "127.0.0.1:${SUBCONVERTER_PORT:-25500}:25500"
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:25500/version >/dev/null"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:
      - conversion

  subweb:
    image: careywong/subweb:latest
    restart: unless-stopped
    depends_on:
      subconverter:
        condition: service_healthy
    ports:
      - "127.0.0.1:${SUBWEB_PORT:-58080}:80"
    networks:
      - conversion

networks:
  conversion:
    driver: bridge
```

Create `docker/subconverter/pref.ini`:

```ini
[common]
api_mode=true
default_url=
enable_insert=false
```

Create `.env.example`:

```dotenv
SUBCONVERTER_PORT=25500
SUBWEB_PORT=58080
```

Append these exact ignore rules, preserving unrelated user rules:

```gitignore
.env
private/proxies.yaml
private/proxy-groups.yaml
private/rules.yaml
generated/*.yaml
__pycache__/
*.py[cod]
```

- [ ] **Step 4: Run static tests and Compose rendering validation**

Run:

```bash
python3 -m unittest tests.test_generate_configs.ComposeSecurityTests -v
docker compose --env-file .env.example config
```

Expected: static test PASS; Compose emits two services with only `127.0.0.1` port bindings and a bridge network that retains required outbound access. If Docker is unavailable locally, record that fact and run the same command on the deployment host before starting the stack.

- [ ] **Step 5: Commit the Compose stack**

```bash
git add compose.yaml .env.example docker/subconverter/pref.ini .gitignore tests/test_generate_configs.py
git commit -m "feat: add loopback subscription conversion stack"
```

### Task 5: Expose the generator CLI and deployment documentation

**Files:**
- Modify: `scripts/generate_configs.py`
- Modify: `tests/test_generate_configs.py`
- Create: `generated/.gitkeep`
- Create: `DEPLOYMENT.md`

**Interfaces:**
- Consumes: `generate_configs()` from Task 2 and Compose variables from Task 4.
- Produces: a safe command-line workflow and deployable instructions without modifying user-owned README files.

- [ ] **Step 1: Write failing CLI tests**

Add a subprocess test that invokes the script without inputs and with public inputs:

```python
import os
import subprocess
import sys


class CliTests(unittest.TestCase):
    def test_cli_requires_source_url_and_converter_base_url(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_configs.py")],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--source-url", result.stderr)

    def test_cli_generates_public_configs_without_printing_source_url(self):
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "generate_configs.py"),
                    "--source-url", "https://panel.example/sub?token=private-value",
                    "--converter-base-url", "https://convert.example.com",
                    "--output-dir", directory,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("private-value", result.stdout)
            self.assertTrue((Path(directory) / "My-Clash_Balanced.yaml").is_file())
```

- [ ] **Step 2: Run CLI tests to verify the expected failure**

Run: `python3 -m unittest tests.test_generate_configs.CliTests -v`

Expected: FAIL because the module has no `argparse` CLI and therefore returns success without required inputs.

- [ ] **Step 3: Implement the CLI and safe output messages**

Append this CLI entry point to `scripts/generate_configs.py`:

```python
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
    for output in outputs:
        print(f"generated {output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Add imports `argparse` and `sys`. Ensure failure messages do not interpolate `source_url` or the provider URL.

Create `DEPLOYMENT.md` with these sections and exact commands:

```markdown
## Start

cp .env.example .env
docker compose up -d
curl http://127.0.0.1:25500/version

## Reverse proxy

Proxy the sub-web host to `http://127.0.0.1:58080` and a separate converter host to `http://127.0.0.1:25500`.
Protect both hosts with HTTPS and Basic Auth or an IP allowlist. Do not expose port 25500 directly.

## Personal generated configurations

cp private/proxies.yaml.example private/proxies.yaml
cp private/proxy-groups.yaml.example private/proxy-groups.yaml
cp private/rules.yaml.example private/rules.yaml
python3 scripts/generate_configs.py --source-url 'https://3x-ui.example/subscription' --converter-base-url 'https://convert.example.com' --private
```

Explain that the `sub-web` Advanced Mode backend value is `https://convert.example.com/sub?`; it must not be a localhost address when opening the web page from another device.

- [ ] **Step 4: Run the complete local verification suite**

Run:

```bash
python3 -m unittest discover -s tests -v
CHECK_DIR="$(mktemp -d /tmp/clash-generated-check.XXXXXX)"
python3 scripts/generate_configs.py --source-url 'https://panel.example/sub?token=placeholder' --converter-base-url 'https://convert.example.com' --output-dir "$CHECK_DIR"
for file in "$CHECK_DIR"/*.yaml; do /usr/local/opt/ruby/bin/ruby -e 'require "yaml"; YAML.load_file(ARGV.fetch(0))' "$file"; done
```

Expected: all unit tests PASS, each public generated YAML parses, and no generated file contains an unreplaced `{{ ... }}` marker. The unit tests are the security check: they prove private fragment content is absent from public templates and command stdout. Use `docker compose --env-file .env.example config` where Docker is installed.

- [ ] **Step 5: Commit CLI, documentation, and output directory**

```bash
git add scripts/generate_configs.py tests/test_generate_configs.py generated/.gitkeep DEPLOYMENT.md
git commit -m "docs: add Clash subscription service deployment guide"
```

### Task 6: Safely hand off the existing sensitive root YAML files

**Files:**
- Review only: `My-Clash_Balanced.yaml`
- Review only: `My-Clash_Balanced_Win.yaml`
- Review only: `My-Clash_Privacy.yaml`
- Modify only after user confirmation: the three root YAML files

**Interfaces:**
- Consumes: newly generated `generated/*.yaml` files from Tasks 1-5.
- Produces: an explicit decision about whether root files remain user-local copies, are replaced by generated files, or are removed manually by the user.

- [ ] **Step 1: Compare generated files against the old root YAML files without printing secrets**

Run structural checks only:

```bash
for file in My-Clash_Balanced.yaml My-Clash_Balanced_Win.yaml My-Clash_Privacy.yaml; do
  wc -l "$file"
done
for file in generated/My-Clash_Balanced.yaml generated/My-Clash_Balanced_Win.yaml generated/My-Clash_Privacy.yaml; do
  /usr/local/opt/ruby/bin/ruby -e 'require "yaml"; YAML.load_file(ARGV.fetch(0)); puts "valid: #{File.basename(ARGV.fetch(0))}"' "$file"
done
```

- [ ] **Step 2: Present the migration result and get explicit confirmation**

State whether public functionality, DNS behavior, and private overlays have been verified. Ask the user to choose one of these precise actions:

1. Keep the old root files locally and use `generated/` going forward.
2. Replace the root files with generated personal copies and add them to `.gitignore`.
3. Remove the root files after confirming all secret credentials have been rotated.

- [ ] **Step 3: Apply only the user-selected root-file migration**

For option 1, make no root-file changes. For option 2, copy generated output only after the user confirms the target paths; do not stage the files. For option 3, move each explicit target to Trash rather than deleting it permanently, then report the exact files moved.

- [ ] **Step 4: Commit only tracked non-sensitive work**

```bash
git status --short
git add compose.yaml .env.example docker templates scripts tests private/*.example generated/.gitkeep DEPLOYMENT.md .gitignore
git commit -m "feat: add private Clash configuration workflow"
```

Do not stage `My-Clash_*.yaml`, `private/*.yaml`, `.env`, `generated/*.yaml`, the user-owned DNS document, or the user-owned README change.

## Plan Self-Review

**Spec coverage:** Tasks 1-3 implement the public templates and isolated private fragments; Task 4 implements the Compose service and loopback-only controls; Task 5 supplies error handling, safe CLI behavior, reverse-proxy documentation, and verification; Task 6 protects the pre-existing sensitive root files and requires explicit migration confirmation.

**Placeholder scan:** The plan contains no unfinished implementation markers. All file paths, markers, function names, commands, assertions, and Compose values are explicit.

**Interface consistency:** Tasks 2 and 5 both call `generate_configs(template_dir, output_dir, converter_base_url, source_url, private_dir, require_private)`. Tasks 1-3 use the same four template marker strings. Template output names match the `TEMPLATES` tuple and every test expectation.
