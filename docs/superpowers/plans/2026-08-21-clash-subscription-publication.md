# Clash Private Subscription Publication Implementation Plan

> **状态：已废止，禁止执行。** 本计划基于保留旧 Trojan/Nginx 443 路由的假设，与已改为“干净服务器、REALITY 独占公网 443、Nginx HTTPS 使用 8443”的当前设计冲突。待设计复审通过后重新编写实施计划。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a private, per-user Clash configuration generator and read-only subscription publisher that combines approved sources, preserves 3x-ui traffic metadata, and integrates safely with the server's existing Trojan/Nginx 443 routing.

**Architecture:** A Python manager fetches subscription sources through an internal MetaCubeX/subconverter, combines them with owner-only snapshots, renders one Clash template into three variants, validates candidates, and atomically publishes per-user releases. A separate read-only publisher serves only the current successful files through opaque token paths and attaches cached 3x-ui traffic headers; host Nginx remains the only public listener.

**Tech Stack:** Python 3.9+, `unittest`, PyYAML 6.0.2, Jinja2 3.1.6, MetaCubeX/subconverter 0.9.2, Mihomo 1.19.28, Docker Compose, Nginx, POSIX shell.

**Spec:** `docs/superpowers/specs/2026-08-21-clash-subscription-publication-design.md`

## Global Constraints

- Use the public name **Clash**; use upstream names only for MetaCubeX/subconverter and the Mihomo validator.
- The final repository name is `my-clash-config`; the installed command is `clash-sub` with no compatibility aliases.
- Never commit or print a real subscription URL, public bearer token, node password, UUID, private key, airport temporary URL, generated configuration, or release metadata containing credentials.
- Treat the current untracked `1/*.yaml` files as user-owned secrets. Move them without reading their values into `private/reference-configs/2026-08-21/`, keep them permanently, and never stage them.
- Other users receive only their own 3x-ui client nodes. Only owner may combine owner 3x-ui, Jrohy/Trojan, airport snapshot, and home nodes.
- Do not deploy sub-web, an arbitrary conversion endpoint, `/sub`, `/getprofile`, a web admin page, cookie automation, or airport login automation.
- Do not create a scheduled regeneration job. Build only on explicit refresh, source/template changes followed by refresh, initial setup, or successful airport import.
- A failed build or Mihomo validation must leave the current release untouched.
- Publish all variants for one user atomically and retain exactly the five newest successful releases per user; reference originals are never pruned.
- Compose must not bind public 80/443. Only publisher may bind a host port, and it must bind `127.0.0.1`.
- Publisher is read-only, cannot invoke the generator, cannot list users or history, and must return indistinguishable 404 responses for invalid tokens and invalid variants.
- Nginx/Trojan deployment begins with read-only inspection. Never rewrite existing Trojan, stream, fallback, 80/443, or site configuration when the observed topology differs from the approved design.
- Use `apply_patch` for repository file edits during execution. Preserve unrelated user changes and stage only files named by each task.
- Use TDD for every behavior change: observe a failing test, implement the minimum behavior, observe a passing test, then commit.

---

## Target File Structure

| Path | Responsibility |
| --- | --- |
| `clash_sub/models.py` | Immutable source, user, traffic, candidate, and response data types. |
| `clash_sub/settings.py` | Parse and validate private `users.yaml`; hash and rotate public tokens. |
| `clash_sub/converter.py` | Call internal subconverter, bound response size, and normalize proxy lists. |
| `clash_sub/traffic.py` | Parse and fetch bounded 3x-ui `Subscription-Userinfo` metadata. |
| `clash_sub/rendering.py` | Load variant data, inject source node names, and render the common Clash template. |
| `clash_sub/validation.py` | Validate YAML structure, references, source URL leakage, and candidate manifests. |
| `clash_sub/releases.py` | Build candidates, publish atomic releases, retain five, inspect history, and roll back. |
| `clash_sub/manager.py` | Container-side management CLI for build, publish, airport import, status, history, rollback, and token rotation. |
| `clash_sub/publisher.py` | Read-only HTTP application, token authorization, current-file serving, traffic cache, and sanitized logging. |
| `clash_sub/host_cli.py` | User-facing `clash-sub` command and Docker Compose/Mihomo orchestration. |
| `config/users.example.yaml` | Non-operational schema example with no real values. |
| `templates/clash.yaml.j2` | Single common Clash template with explicit YAML block markers. |
| `templates/variants/*.yaml` | Balanced, Balanced Win, and Privacy differences plus node-injection group metadata. |
| `bin/clash-sub` | Thin Python entry point for `clash_sub.host_cli:main`. |
| `Dockerfile` | Non-root Python runtime shared by manager and publisher. |
| `compose.yaml` | Internal subconverter, loopback publisher, one-shot manager, and one-shot Mihomo validator. |
| `deploy/nginx/clash-sub.conf.tmpl` | HTTPS 1443 virtual host forwarding only `/s/` to publisher. |
| `scripts/server_preflight.py` | Read-only analysis of listeners, Nginx stream/SNI routing, Trojan safe fields, and certificate SAN. |
| `scripts/install-server.sh` | Dry-run-first one-command installer with explicit `--apply`. |
| `tests/test_*.py` | Focused standard-library tests for every component and cross-user security boundary. |
| `docs/private-data.md` | Private data layout, permissions, backup, and recovery. |
| `docs/operations.md` | `clash-sub` command guide, airport mobile flow, rotation, and rollback. |

Private runtime data is outside Git under `private/` exactly as specified in the design document.

---

### Task 1: Quarantine Reference Configs and Establish Repository Safety Guards

**Files:**
- Modify: `.gitignore`
- Modify: `tests/test_generate_configs.py`
- Create: `tests/test_repository_safety.py`
- Create: `config/users.example.yaml`
- Create: `docs/private-data.md`
- Move without staging: `1/*.yaml` → `private/reference-configs/2026-08-21/*.yaml`

**Interfaces:**
- Consumes: the current untracked `1/` directory and tracked repository layout.
- Produces: ignored private roots and a safe example schema used by Task 2.

- [ ] **Step 1: Write failing ignore-policy tests**

Create `tests/test_repository_safety.py`:

```python
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositorySafetyTests(unittest.TestCase):
    def test_private_runtime_paths_are_gitignored(self):
        paths = (
            "private/config/users.yaml",
            "private/reference-configs/2026-08-21/reference.yaml",
            "private/sources/owner/airport.yaml",
            "private/releases/owner/release/config.yaml",
            "private/current/owner",
            "private/staging/operation/config.yaml",
            "private/logs/operations.jsonl",
        )
        for path in paths:
            result = subprocess.run(
                ["git", "check-ignore", "-q", path], cwd=ROOT, check=False
            )
            self.assertEqual(result.returncode, 0, path)

    def test_reference_source_directory_is_not_tracked(self):
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "1/My-Clash_Balanced.yaml"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
```

Replace the real identifiers in `FORBIDDEN_SUBSTRINGS` inside `tests/test_generate_configs.py` with synthetic fixture values such as `private.example`, `198.51.100.10`, and `00000000-0000-4000-8000-000000000000`. A test must never commit the secret it is intended to detect.

- [ ] **Step 2: Run the safety test and verify it fails**

Run: `python3 -m unittest tests.test_repository_safety -v`

Expected: FAIL because the current `.gitignore` does not ignore all of `private/`.

- [ ] **Step 3: Expand `.gitignore` and create the safe user example**

Use these ignore rules:

```gitignore
.worktrees/
.venv/
.env
private/**
!private/.gitkeep
generated/**
!generated/.gitkeep
__pycache__/
*.py[cod]
```

Create `config/users.example.yaml` with an intentionally non-operational example:

```yaml
schema-version: 1
public-base-url: https://sub.example.com
users:
  example-user:
    owner: false
    token-sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    variants:
      - balanced
    sources:
      - kind: xui
        url: https://panel.example.com/sub/example-user
```

Document `0700` directory and `0600` file permissions in `docs/private-data.md` and explicitly state that the example file must not be edited into a real config in place.

- [ ] **Step 4: Move the three originals without exposing their contents**

Run:

```bash
install -d -m 700 private/reference-configs/2026-08-21
mv -- 1/My-Clash_Balanced.yaml private/reference-configs/2026-08-21/
mv -- 1/My-Clash_Balanced_Win.yaml private/reference-configs/2026-08-21/
mv -- 1/My-Clash_Privacy.yaml private/reference-configs/2026-08-21/
chmod 600 private/reference-configs/2026-08-21/*.yaml
rmdir 1
```

Do not run `git add -A`. Confirm `git status --short --ignored` reports the destination as ignored.

- [ ] **Step 5: Run safety and existing tests**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests PASS, and no test output contains the contents of a reference file.

- [ ] **Step 6: Commit only safety files**

```bash
git add .gitignore config/users.example.yaml docs/private-data.md tests/test_repository_safety.py tests/test_generate_configs.py
git commit -m "chore: protect private Clash configuration data"
```

---

### Task 2: Add Validated Private Settings and Token Models

**Files:**
- Create: `requirements.txt`
- Create: `clash_sub/__init__.py`
- Create: `clash_sub/models.py`
- Create: `clash_sub/settings.py`
- Create: `tests/test_settings.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `config/users.example.yaml` schema from Task 1.
- Produces: `load_settings(path: Path, private_root: Path) -> Settings`, `hash_token(token: str) -> str`, and `rotate_user_token(path: Path, private_root: Path, user_id: str) -> TokenRotation`.

- [ ] **Step 1: Pin production dependencies and write settings tests**

Create `requirements.txt`:

```text
Jinja2==3.1.6
PyYAML==6.0.2
```

Create the ignored development environment before running dependency-backed tests:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Create `tests/test_settings.py` covering a valid ordinary user, a valid owner, invalid token hashes, unknown variants, non-owner extra sources, two owners, and source paths that escape `private_root`:

```python
class SettingsTests(unittest.TestCase):
    def test_non_owner_may_only_have_one_xui_source(self):
        path = self.write_settings(
            users={
                "friend": {
                    "owner": False,
                    "token-sha256": "a" * 64,
                    "variants": ["balanced"],
                    "sources": [
                        {"kind": "xui", "url": "https://panel.example/sub/friend"},
                        {"kind": "home", "path": "sources/owner/home.yaml"},
                    ],
                }
            }
        )
        with self.assertRaisesRegex(SettingsError, "friend.*only one xui"):
            load_settings(path, self.private_root)

    def test_hash_token_is_stable_sha256_without_storing_plaintext(self):
        self.assertEqual(
            hash_token("sample-token"),
            "0f35d0ae14518b96bd6d3fec3ca15801fd58c9e048b1ccdea11a71378f2acdc9",
        )
```

- [ ] **Step 2: Run the new tests and verify import failure**

Run: `.venv/bin/python -m unittest tests.test_settings -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'clash_sub'`.

- [ ] **Step 3: Implement immutable models and strict parsing**

Define these public types in `clash_sub/models.py` using Python 3.9-compatible typing:

```python
VARIANTS = ("balanced", "balanced-win", "privacy")
SOURCE_KINDS = ("xui", "trojan", "airport", "home")

@dataclass(frozen=True)
class SourceSpec:
    kind: str
    url: Optional[str] = None
    path: Optional[Path] = None

@dataclass(frozen=True)
class UserSpec:
    user_id: str
    owner: bool
    token_sha256: str
    variants: Tuple[str, ...]
    sources: Tuple[SourceSpec, ...]

@dataclass(frozen=True)
class Settings:
    public_base_url: str
    private_root: Path
    users: Mapping[str, UserSpec]

@dataclass(frozen=True)
class TokenRotation:
    user_id: str
    token: str
    urls: Mapping[str, str]
```

Implement `load_settings()` with `yaml.safe_load()`. Reject unknown top-level and per-user keys, require HTTPS source/public URLs, require exactly one owner at most, require a 64-character lowercase hexadecimal token hash, and resolve local paths under `private_root` using `Path.resolve()` plus `relative_to()`.

Implement `hash_token()` with SHA-256 and `rotate_user_token()` with `secrets.token_urlsafe(32)`. Update `users.yaml` atomically, save only the hash, and return the plaintext token once in `TokenRotation`.

- [ ] **Step 4: Run settings tests**

Run: `.venv/bin/python -m unittest tests.test_settings -v`

Expected: all settings tests PASS.

- [ ] **Step 5: Commit settings foundation**

```bash
git add .gitignore requirements.txt clash_sub/__init__.py clash_sub/models.py clash_sub/settings.py tests/test_settings.py
git commit -m "feat: add private subscription settings model"
```

---

### Task 3: Normalize Remote and Local Proxy Sources

**Files:**
- Create: `clash_sub/converter.py`
- Create: `clash_sub/traffic.py`
- Create: `tests/test_converter.py`

**Interfaces:**
- Consumes: `SourceSpec` from Task 2 and internal converter base URL `http://subconverter:25500`.
- Produces: `SubconverterClient.convert(source_url: str) -> Tuple[Mapping[str, object], ...]`, `load_local_proxies(path: Path)`, `merge_proxy_sources(sources)`, and `TrafficClient.fetch(source_url: str) -> Optional[SubscriptionUserinfo]`.

- [ ] **Step 1: Write bounded-fetch and normalization tests**

Use a fake opener so tests never access the network:

```python
class ConverterTests(unittest.TestCase):
    def test_build_url_encodes_source_and_requests_clash_list(self):
        client = SubconverterClient("http://subconverter:25500", opener=FakeOpener())
        url = client.build_url("https://panel.example/sub?a=1&b=2")
        self.assertIn("target=clash", url)
        self.assertIn("list=true", url)
        self.assertIn("a%3D1%26b%3D2", url)

    def test_convert_returns_only_proxy_mappings(self):
        opener = FakeOpener("proxies:\n  - name: node-a\n    type: vless\n")
        proxies = SubconverterClient("http://subconverter:25500", opener=opener).convert(
            "https://panel.example/sub/user"
        )
        self.assertEqual(proxies[0]["name"], "node-a")

    def test_response_over_limit_is_rejected_without_echoing_url(self):
        secret_url = "https://panel.example/sub?token=secret-value"
        with self.assertRaisesRegex(SourceError, "response exceeds") as context:
            SubconverterClient(
                "http://subconverter:25500", opener=FakeOpener("x" * 1025), max_bytes=1024
            ).convert(secret_url)
        self.assertNotIn(secret_url, str(context.exception))

    def test_subscription_userinfo_is_parsed_into_non_negative_integers(self):
        metadata = parse_subscription_userinfo(
            "upload=10; download=20; total=100; expire=1893456000"
        )
        self.assertEqual(metadata.remaining, 70)
        self.assertEqual(
            metadata.header_value,
            "upload=10; download=20; total=100; expire=1893456000",
        )
```

Also test missing `proxies`, duplicate names within one source, non-mapping proxy entries, empty local snapshots, unsupported local YAML roots, missing traffic headers, malformed traffic fields, negative traffic values, and `total=0` unlimited metadata.

- [ ] **Step 2: Run the tests and verify missing module failure**

Run: `.venv/bin/python -m unittest tests.test_converter -v`

Expected: FAIL because `clash_sub.converter` does not exist.

- [ ] **Step 3: Implement the converter client and local loader**

Use `urllib.request.urlopen` with a 20-second timeout, a 5 MiB maximum response, and `yaml.safe_load`. Never place the source URL in an exception or log message.

Define `SourceError(RuntimeError)`, `SubconverterClient.__init__(base_url: str, opener=urlopen, timeout: int = 20, max_bytes: int = 5 * 1024 * 1024)`, `SubconverterClient.build_url(source_url: str) -> str`, `SubconverterClient.convert(source_url: str) -> Tuple[Mapping[str, object], ...]`, `load_local_proxies(path: Path) -> Tuple[Mapping[str, object], ...]`, and `merge_proxy_sources(sources: Sequence[Tuple[str, Sequence[Mapping[str, object]]]]) -> Tuple[Mapping[str, object], ...]`.

`merge_proxy_sources()` preserves original names when unique. On collision, append the source label in square brackets only to colliding entries, then append `-2`, `-3` if the labeled name also collides. Copy mappings before changing names so cached inputs remain immutable.

In `clash_sub/traffic.py`, define `parse_subscription_userinfo(value: str) -> SubscriptionUserinfo` and `TrafficClient.fetch(source_url: str) -> Optional[SubscriptionUserinfo]`. The data type is:

```python
@dataclass(frozen=True)
class SubscriptionUserinfo:
    upload: int
    download: int
    total: int
    expire: int

    @property
    def remaining(self) -> Optional[int]:
        if self.total == 0:
            return None
        return max(self.total - self.upload - self.download, 0)

    @property
    def header_value(self) -> str:
        return (
            f"upload={self.upload}; download={self.download}; "
            f"total={self.total}; expire={self.expire}"
        )
```

`TrafficClient.fetch()` performs a direct HTTPS GET, reads only response headers, closes the body immediately, and never includes the URL in errors. `remaining` returns `None` for `total=0`; otherwise it returns `max(total - upload - download, 0)`.

- [ ] **Step 4: Run converter tests**

Run: `.venv/bin/python -m unittest tests.test_converter -v`

Expected: all converter tests PASS.

- [ ] **Step 5: Commit source normalization**

```bash
git add clash_sub/converter.py clash_sub/traffic.py tests/test_converter.py
git commit -m "feat: normalize private subscription sources"
```

---

### Task 4: Migrate the Three Authoritative Configs to One Clash Template

**Files:**
- Create: `clash_sub/rendering.py`
- Create: `templates/clash.yaml.j2`
- Create: `templates/variants/balanced.yaml`
- Create: `templates/variants/balanced-win.yaml`
- Create: `templates/variants/privacy.yaml`
- Create: `scripts/compare_reference_configs.py`
- Create: `tests/test_rendering.py`
- Delete after replacement passes: `templates/_base.yaml.tmpl`
- Delete after replacement passes: `templates/parts/*.part`

**Interfaces:**
- Consumes: normalized proxies from Task 3 and ignored authoritative references from Task 1.
- Produces: `render_variant(template_dir: Path, variant: str, proxies: Sequence[Mapping[str, object]]) -> str` and a structural comparison report containing paths only.

- [ ] **Step 1: Write rendering and variant-separation tests**

```python
class RenderingTests(unittest.TestCase):
    def test_each_variant_renders_a_distinct_complete_config(self):
        rendered = {
            variant: yaml.safe_load(render_variant(TEMPLATE_DIR, variant, DUMMY_PROXIES))
            for variant in VARIANTS
        }
        for document in rendered.values():
            self.assertIn("dns", document)
            self.assertIn("proxies", document)
            self.assertIn("proxy-groups", document)
            self.assertIn("rule-providers", document)
            self.assertIn("rules", document)
            self.assertNotIn("proxy-providers", document)
        self.assertNotEqual(rendered["balanced"], rendered["balanced-win"])
        self.assertNotEqual(rendered["balanced"], rendered["privacy"])

    def test_source_names_are_injected_only_into_declared_groups(self):
        document = yaml.safe_load(render_variant(TEMPLATE_DIR, "balanced", DUMMY_PROXIES))
        groups = {group["name"]: group for group in document["proxy-groups"]}
        configured = load_variant(TEMPLATE_DIR, "balanced")
        for name in configured.inject_node_groups:
            self.assertIn("node-a", groups[name]["proxies"])
```

Add tests that all Jinja variables resolve under `StrictUndefined`, Unicode names remain unchanged, and dumped proxy passwords containing braces or punctuation remain valid YAML.

- [ ] **Step 2: Run rendering tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_rendering -v`

Expected: FAIL because the new renderer and templates do not exist.

- [ ] **Step 3: Extract the common template and three real variants**

Use the ignored references as the source of truth without copying any node credentials into tracked files:

1. Parse each reference with `yaml.safe_load()`.
2. Remove `proxies` and subscription `proxy-providers` before writing tracked data.
3. Identify top-level sections equal in all three references and place them once in `templates/clash.yaml.j2`.
4. Place differing `dns`, `proxy-groups`, `rules`, and platform-specific sections in the matching variant file.
5. Add variant-only metadata under `_generator`, with `inject-node-groups` listing exact group names that should receive generated node names; remove `_generator` from final output.
6. Scan every tracked template for UUID, node password, private key, source URL, public IP literal, and the reference files' exact proxy names before staging.

The common template uses only these strict markers:

```jinja2
dns:
{{ DNS_YAML }}
proxies:
{{ PROXIES_YAML }}
proxy-groups:
{{ PROXY_GROUPS_YAML }}
{{ VARIANT_TOP_LEVEL_YAML }}
rules:
{{ RULES_YAML }}
```

All common top-level settings and the complete common `rule-providers` block remain literal YAML in `clash.yaml.j2`; only the five markers above are dynamic. `VARIANT_TOP_LEVEL_YAML` contains platform-specific top-level keys that are absent from the common base. `rendering.py` uses `StrictUndefined`, registers one `yaml_block` helper backed by `yaml.safe_dump(sort_keys=False, allow_unicode=True, default_flow_style=False)`, and controls indentation before passing strings to Jinja2. Do not allow templates to call Python objects or arbitrary functions.

- [ ] **Step 4: Implement structural reference comparison**

`scripts/compare_reference_configs.py` accepts `--reference-dir` and `--template-dir`, renders each variant with one synthetic proxy, parses references and rendered text, normalizes only approved source differences, and prints differing YAML paths without values:

```python
IGNORED_PATHS = {
    ("proxies",),
    ("proxy-providers",),
}
```

Before comparison, remove reference proxy names from only the groups listed by that variant's `_generator.inject-node-groups`, and remove the synthetic proxy name from the same rendered groups. The script exits 0 only when every remaining key, list order, and scalar matches its corresponding reference. It must not serialize either document or print values.

- [ ] **Step 5: Run renderer tests and compare all three references**

Run:

```bash
.venv/bin/python -m unittest tests.test_rendering -v
.venv/bin/python scripts/compare_reference_configs.py \
  --reference-dir private/reference-configs/2026-08-21 \
  --template-dir templates
```

Expected: tests PASS and comparison exits 0. If comparison reports paths, adjust the tracked template/variant data, not the ignored references.

- [ ] **Step 6: Remove obsolete templates and commit**

```bash
git add clash_sub/rendering.py templates/clash.yaml.j2 templates/variants scripts/compare_reference_configs.py tests/test_rendering.py
git rm templates/_base.yaml.tmpl templates/parts/dns-balanced.part templates/parts/dns-privacy.part templates/parts/geoip-resolve.part templates/parts/geoip-no-resolve.part
git commit -m "feat: render three Clash variants from one template"
```

---

### Task 5: Validate Complete Clash Documents Before Release

**Files:**
- Create: `clash_sub/validation.py`
- Create: `tests/test_validation.py`

**Interfaces:**
- Consumes: rendered YAML text, approved source URLs, and expected variants.
- Produces: `validate_config(text: str, source_urls: Sequence[str]) -> Mapping[str, object]` and `sha256_file(path: Path) -> str`.

- [ ] **Step 1: Write validation failure tests**

```python
class ValidationTests(unittest.TestCase):
    def test_rejects_unknown_proxy_group_reference(self):
        document = valid_document()
        document["proxy-groups"][0]["proxies"].append("missing-node")
        with self.assertRaisesRegex(ValidationError, "missing-node"):
            validate_config(yaml.safe_dump(document, allow_unicode=True), [])

    def test_rejects_exact_upstream_url_leak_without_echoing_it(self):
        source_url = "https://panel.example/sub?token=private-value"
        document = valid_document()
        document["notes"] = source_url
        with self.assertRaisesRegex(ValidationError, "upstream source URL") as context:
            validate_config(yaml.safe_dump(document), [source_url])
        self.assertNotIn(source_url, str(context.exception))
```

Also test malformed YAML, missing required top-level keys, duplicate proxy names, duplicate proxy-group names, unresolved group references, leftover Jinja markers, empty proxy lists, and a valid public rule-provider URL.

- [ ] **Step 2: Run validation tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_validation -v`

Expected: FAIL because `clash_sub.validation` does not exist.

- [ ] **Step 3: Implement structural and leakage validation**

Use these built-in targets when checking group references:

```python
BUILTIN_TARGETS = {
    "DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"
}
REQUIRED_TOP_LEVEL = {"dns", "proxies", "proxy-groups", "rule-providers", "rules"}
```

Reject `proxy-providers` in final output because all private source nodes must be expanded. Permit public `rule-providers`. Errors may include YAML paths and node/group display names but may not include credentials, full proxy mappings, or source URLs.

- [ ] **Step 4: Run validation and rendering tests**

Run: `.venv/bin/python -m unittest tests.test_validation tests.test_rendering -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit the validation gate**

```bash
git add clash_sub/validation.py tests/test_validation.py
git commit -m "feat: validate complete Clash configurations"
```

---

### Task 6: Build Candidates and Publish Atomic Five-Version Releases

**Files:**
- Create: `clash_sub/releases.py`
- Create: `tests/test_releases.py`

**Interfaces:**
- Consumes: `Settings`, `SubconverterClient`, `TrafficClient`, local source loader, renderer, and validator.
- Produces: `build_candidate(settings: Settings, user_id: str, converter: SubconverterClient, traffic: TrafficClient, template_dir: Path, private_root: Path, operation_id: str) -> Candidate`, `publish_candidate(candidate: Candidate, private_root: Path, keep: int = 5) -> Release`, `list_history(private_root: Path, user_id: str) -> Tuple[Release, ...]`, and `rollback(private_root: Path, user_id: str, release_id: str) -> Release`.

- [ ] **Step 1: Write per-user isolation and atomicity tests**

```python
class ReleaseTests(unittest.TestCase):
    def test_friend_candidate_contains_only_friend_xui_nodes(self):
        candidate = self.builder.build_candidate("friend")
        text = candidate.files["balanced"].read_text(encoding="utf-8")
        self.assertIn("friend-node", text)
        self.assertNotIn("owner-xui-node", text)
        self.assertNotIn("owner-airport-node", text)
        self.assertNotIn("owner-home-node", text)
        self.assertNotIn("owner-trojan-node", text)

    def test_owner_publish_switches_all_three_variants_together(self):
        candidate = self.builder.build_candidate("owner")
        release = publish_candidate(candidate, self.private_root, keep=5)
        current = (self.private_root / "current" / "owner").resolve()
        self.assertEqual(current, release.path.resolve())
        self.assertEqual(set(release.files), {"balanced", "balanced-win", "privacy"})

    def test_failed_candidate_does_not_replace_current(self):
        previous = self.publish_valid_owner_release()
        with self.assertRaises(BuildError):
            self.builder.build_candidate("owner", renderer=FailingRenderer())
        self.assertEqual((self.private_root / "current" / "owner").resolve(), previous.path)
```

Add tests for empty remote sources, missing owner snapshots, five-version pruning after six successful releases, reference-directory preservation, rollback, manifest hashes, and sanitized operation logs.

- [ ] **Step 2: Run release tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_releases -v`

Expected: FAIL because `clash_sub.releases` does not exist.

- [ ] **Step 3: Implement candidate construction**

Define these public data types in `models.py`:

```python
@dataclass(frozen=True)
class Candidate:
    operation_id: str
    user_id: str
    path: Path
    files: Mapping[str, Path]
    manifest_path: Path

@dataclass(frozen=True)
class Release:
    release_id: str
    user_id: str
    path: Path
    files: Mapping[str, Path]
```

`build_candidate()` creates `private/staging/<operation-id>/<user-id>/`, resolves only sources declared by that user, converts remote sources separately, fetches the user's xui traffic metadata once, merges names with source labels, renders every allowed variant, validates each file, writes `<variant>.meta.json`, and writes a manifest containing only:

```json
{
  "schema_version": 1,
  "operation_id": "20260821T120000Z-a1b2c3d4",
  "user_id": "owner",
  "created_at": "2026-08-21T12:00:00Z",
  "variants": ["balanced", "balanced-win", "privacy"],
  "input_hashes": {"template": "sha256", "xui": "sha256", "trojan": "sha256", "airport": "sha256", "home": "sha256"},
  "output_hashes": {"balanced": "sha256", "balanced-win": "sha256", "privacy": "sha256"},
  "source_counts": {"xui": 1, "trojan": 1, "airport": 2, "home": 2}
}
```

The manifest never includes source URLs, tokens, proxy mappings, node names, passwords, or UUIDs. Each metadata sidecar contains only the validated `upload`, `download`, `total`, `expire`, fetch timestamp, and output hash; it does not contain the xui URL.

- [ ] **Step 4: Implement atomic publication, retention, and rollback**

`publish_candidate()` moves the complete candidate directory to `private/releases/<user-id>/<release-id>/`, creates a temporary relative symlink beside `private/current/<user-id>`, then uses `os.replace()` to switch it atomically. Only after the switch succeeds may it prune successful releases older than the newest five.

`rollback()` verifies the requested release belongs to the user, contains every manifest-declared file, and has matching hashes before switching the current symlink. It never calls subconverter or the renderer.

- [ ] **Step 5: Run release and security tests**

Run: `.venv/bin/python -m unittest tests.test_releases tests.test_repository_safety -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit release management**

```bash
git add clash_sub/models.py clash_sub/releases.py tests/test_releases.py
git commit -m "feat: publish atomic per-user Clash releases"
```

---

### Task 7: Implement Container-Side Management Commands and Airport Import

**Files:**
- Create: `clash_sub/manager.py`
- Create: `tests/test_manager.py`

**Interfaces:**
- Consumes: settings, candidate/release APIs, stdin for temporary airport URLs, and the private operation log.
- Produces: machine-readable manager subcommands used by Task 9.

- [ ] **Step 1: Write manager command tests**

```python
class ManagerTests(unittest.TestCase):
    def test_import_airport_reads_url_only_from_stdin(self):
        result = run_manager(["import-airport"], stdin="https://airport.example/temp/secret\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("secret", result.stdout)
        self.assertNotIn("secret", result.stderr)

    def test_status_reports_changed_input_hash_without_credentials(self):
        self.publish_owner_release()
        self.change_home_snapshot()
        result = run_manager(["status", "owner"])
        self.assertIn('"needs_refresh": true', result.stdout)
        self.assertNotIn("password", result.stdout.lower())

    def test_rotate_token_prints_each_new_url_once(self):
        result = run_manager(["rotate-token", "friend"])
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload["urls"]), {"balanced"})
        updated = load_settings(self.settings_path, self.private_root)
        self.assertNotIn(payload["token"], self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(updated.users["friend"].token_sha256, hash_token(payload["token"]))
```

Also test `list-users`, `build`, `publish`, `history`, `rollback`, malformed stdin, expired/invalid airport responses, and operation logs containing only timestamp, operation, user ID, release ID, and status.

- [ ] **Step 2: Run manager tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_manager -v`

Expected: FAIL because `clash_sub.manager` does not exist.

- [ ] **Step 3: Implement exact machine-facing subcommands**

The manager parser exposes:

```text
python -m clash_sub.manager list-users
python -m clash_sub.manager build --operation-id <id> --user <id>
python -m clash_sub.manager publish --operation-id <id> --user <id>
python -m clash_sub.manager status [<user-id>]
python -m clash_sub.manager history <user-id>
python -m clash_sub.manager rollback <user-id> <release-id>
python -m clash_sub.manager rotate-token <user-id>
python -m clash_sub.manager import-airport
python -m clash_sub.manager logs [--limit 50]
```

All successful output is JSON. Errors identify the operation and user but redact URLs and credentials. `import-airport` reads one line from stdin, rejects non-HTTPS URLs, converts it immediately through subconverter, validates a non-empty `proxies` list, atomically writes `private/sources/owner/airport.yaml`, and never stores the URL.

- [ ] **Step 4: Run manager, release, and settings tests**

Run: `.venv/bin/python -m unittest tests.test_manager tests.test_releases tests.test_settings -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit manager commands**

```bash
git add clash_sub/manager.py tests/test_manager.py
git commit -m "feat: add private Clash management commands"
```

---

### Task 8: Build the Read-Only Subscription Publisher and Traffic Header Cache

**Files:**
- Create: `clash_sub/publisher.py`
- Create: `tests/test_publisher.py`

**Interfaces:**
- Consumes: token hashes and user mappings from settings, current release symlinks, per-variant metadata, and `TrafficClient` from Task 3.
- Produces: `PublicationService.handle(request: Request) -> Response` and `python -m clash_sub.publisher` HTTP server.

- [ ] **Step 1: Write authorization, serving, and traffic tests**

```python
class PublisherTests(unittest.TestCase):
    def test_valid_token_serves_only_allowed_current_variant(self):
        response = self.app.handle(
            Request("GET", "/s/friend-token/balanced.yaml", "198.51.100.20")
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "text/yaml; charset=utf-8")
        self.assertIn(b"proxies:", response.body)

    def test_invalid_token_and_forbidden_variant_have_same_404_shape(self):
        invalid = self.app.handle(
            Request("GET", "/s/not-a-token/balanced.yaml", "198.51.100.20")
        )
        forbidden = self.app.handle(
            Request("GET", "/s/friend-token/privacy.yaml", "198.51.100.20")
        )
        self.assertEqual((invalid.status, invalid.body), (forbidden.status, forbidden.body))

    def test_traffic_header_is_refreshed_then_cached_for_ten_minutes(self):
        self.traffic_fetcher.return_value = "upload=10; download=20; total=100; expire=1893456000"
        request = Request("GET", "/s/friend-token/balanced.yaml", "198.51.100.20")
        first = self.app.handle(request)
        second = self.app.handle(request)
        self.assertEqual(first.headers["Subscription-Userinfo"], self.traffic_fetcher.return_value)
        self.assertEqual(second.headers["Subscription-Userinfo"], self.traffic_fetcher.return_value)
        self.assertEqual(self.traffic_fetcher.call_count, 1)
```

Add tests for metadata fallback after upstream failure, `total=0`, missing current release, traversal attempts, URL-encoded separators, response-size limit, GET-only behavior, `/healthz`, in-memory per-token rate limiting, settings reload on mtime change, and logs that never contain request paths or tokens.

- [ ] **Step 2: Run publisher tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_publisher -v`

Expected: FAIL because `clash_sub.publisher` does not exist.

- [ ] **Step 3: Implement a pure publication service**

Add these types to `models.py`:

```python
@dataclass(frozen=True)
class Request:
    method: str
    path: str
    client_ip: str

@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
```

`PublicationService` parses only `/s/<token>/<variant>.yaml`, hashes the token, compares hashes with `hmac.compare_digest`, verifies the variant allowlist, resolves the current symlink under the release root, verifies the file hash against the manifest, and returns bytes without modifying disk.

The publisher sets:

```text
Content-Type: text/yaml; charset=utf-8
Content-Disposition: attachment; filename="<variant>.yaml"
Cache-Control: no-store
X-Content-Type-Options: nosniff
Subscription-Userinfo: <validated 3x-ui value when available>
```

Do not set a forced client refresh interval. Clash Verge and other clients retain control of their own subscription update schedule.

- [ ] **Step 4: Implement bounded traffic metadata retrieval**

For the user's source with `kind: xui`, call `TrafficClient.fetch()`. It issues a direct HTTPS GET with a 10-second timeout, reads only the response headers, closes the body immediately, and accepts `Subscription-Userinfo` only when it matches four semicolon-separated non-negative integer fields named `upload`, `download`, `total`, and `expire`.

Cache successful values in memory for 600 seconds. If retrieval fails, use the last in-memory value, then the release sidecar value. Never fail configuration download solely because traffic metadata is unavailable.

- [ ] **Step 5: Add the loopback HTTP adapter**

Use `ThreadingHTTPServer`. Override `BaseHTTPRequestHandler.log_message()` to do nothing and emit sanitized application records after authorization using only user ID, variant, status, and timestamp. Trust `X-Real-IP` only because Compose binds publisher to loopback and Nginx is the sole caller.

- [ ] **Step 6: Run publisher and release tests**

Run: `.venv/bin/python -m unittest tests.test_publisher tests.test_releases -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit the publisher**

```bash
git add clash_sub/models.py clash_sub/publisher.py tests/test_publisher.py
git commit -m "feat: publish tokenized Clash subscriptions"
```

---

### Task 9: Implement the User-Facing `clash-sub` Orchestrator

**Files:**
- Create: `clash_sub/host_cli.py`
- Create: `bin/clash-sub`
- Create: `tests/test_host_cli.py`

**Interfaces:**
- Consumes: Docker Compose manager JSON, the one-shot Mihomo validator, and hidden terminal input.
- Produces: the exact user-facing commands approved in the spec.

- [ ] **Step 1: Write help and orchestration tests with a fake command runner**

```python
class HostCliTests(unittest.TestCase):
    def test_no_arguments_prints_help(self):
        result = run_cli([])
        self.assertEqual(result.returncode, 0)
        self.assertIn("clash-sub refresh [user-id]", result.stdout)
        self.assertIn("clash-sub airport", result.stdout)

    def test_refresh_never_publishes_when_mihomo_validation_fails(self):
        runner = FakeRunner(build_result=owner_candidate_json(), validator_returncode=1)
        result = run_cli(["refresh", "owner"], runner=runner)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(runner.was_called("publish"))

    def test_refresh_all_isolates_user_failures(self):
        runner = FakeRunner(users=["owner", "friend"], failing_build_users={"friend"})
        result = run_cli(["refresh"], runner=runner)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(runner.was_published("owner"))
        self.assertFalse(runner.was_published("friend"))
```

Add tests that `airport` obtains the URL through `getpass.getpass()`, forwards it only on subprocess stdin, refreshes owner after import, and never includes the URL in argv or captured output.

- [ ] **Step 2: Run host CLI tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_host_cli -v`

Expected: FAIL because `clash_sub.host_cli` does not exist.

- [ ] **Step 3: Implement the exact public command surface**

Expose only:

```text
clash-sub
clash-sub help
clash-sub status
clash-sub refresh [user-id]
clash-sub airport
clash-sub history <user-id>
clash-sub rollback <user-id> <release-id>
clash-sub rotate-link <user-id>
clash-sub logs
```

There is no `refresh-all` alias. `refresh` without a user calls `list-users` and processes users separately.

For each user refresh:

1. Generate a non-secret operation ID locally.
2. Run manager `build` and parse candidate file paths from JSON.
3. Run the Mihomo validator once per candidate file.
4. Call manager `publish` only when every file for that user passes.
5. Continue to the next user after a failure, then return non-zero if any user failed.

`bin/clash-sub` contains only:

```python
#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clash_sub.host_cli import main

raise SystemExit(main())
```

- [ ] **Step 4: Run CLI tests**

Run: `.venv/bin/python -m unittest tests.test_host_cli -v`

Expected: all host CLI tests PASS.

- [ ] **Step 5: Commit the host command**

```bash
git add clash_sub/host_cli.py bin/clash-sub tests/test_host_cli.py
git commit -m "feat: add clash-sub host command"
```

---

### Task 10: Replace the Old Compose Stack with Internal Conversion and Read-Only Publication

**Files:**
- Create: `Dockerfile`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `docker/subconverter/pref.ini`
- Create: `tests/test_compose.py`
- Modify: `tests/test_generate_configs.py`

**Interfaces:**
- Consumes: manager, publisher, and host CLI from Tasks 7–9.
- Produces: reproducible container services named `subconverter`, `publisher`, `manager`, and `validator`.

- [ ] **Step 1: Replace old Compose assertions with new security tests**

```python
class ComposeTests(unittest.TestCase):
    def test_subconverter_has_no_host_port(self):
        content = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("subconverter:", content)
        self.assertNotRegex(content, r"SUBCONVERTER_PORT.*25500")

    def test_only_publisher_binds_loopback(self):
        content = COMPOSE.read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:${PUBLISHER_PORT:-25501}:8080"', content)
        self.assertNotIn("0.0.0.0:", content)

    def test_publisher_cannot_mount_reference_or_source_snapshots(self):
        publisher = compose_service("publisher")
        mounts = "\n".join(publisher["volumes"])
        self.assertIn("private/config/users.yaml", mounts)
        self.assertIn("private/current", mounts)
        self.assertIn("private/releases", mounts)
        self.assertNotIn("private/reference-configs", mounts)
        self.assertNotIn("private/sources", mounts)

    def test_subweb_and_latest_tags_are_absent(self):
        content = COMPOSE.read_text(encoding="utf-8")
        self.assertNotIn("subweb", content.lower())
        self.assertNotIn(":latest", content)
```

Add assertions that publisher mounts private data read-only, manager mounts it read-write, manager/validator use a `tools` profile, publisher uses a non-root UID/GID, and no service mounts the Docker socket.

- [ ] **Step 2: Run Compose tests and verify the old stack fails**

Run: `.venv/bin/python -m unittest tests.test_compose -v`

Expected: FAIL because current Compose contains sub-web, latest tags, and a host subconverter port.

- [ ] **Step 3: Build the non-root application image**

Use `python:3.12.11-alpine3.22` as the fixed Python base, install only `requirements.txt`, create UID/GID 10001, copy `clash_sub/`, `templates/`, and `config/`, and set `PYTHONDONTWRITEBYTECODE=1`. The final image contains no `private/`, reference config, test fixture, Git metadata, Nginx config, or source subscription.

The Dockerfile entrypoint remains overridable so Compose can run:

```text
python -m clash_sub.publisher
python -m clash_sub.manager <subcommand>
```

- [ ] **Step 4: Replace Compose services**

Use these fixed upstream images:

```yaml
services:
  subconverter:
    image: ghcr.io/metacubex/subconverter:0.9.2
    expose:
      - "25500"

  publisher:
    build: .
    command: ["python", "-m", "clash_sub.publisher"]
    user: "${CLASH_SUB_UID:-10001}:${CLASH_SUB_GID:-10001}"
    read_only: true
    ports:
      - "127.0.0.1:${PUBLISHER_PORT:-25501}:8080"
    volumes:
      - ./private/config/users.yaml:/data/private/config/users.yaml:ro
      - ./private/current:/data/private/current:ro
      - ./private/releases:/data/private/releases:ro

  manager:
    build: .
    profiles: ["tools"]
    user: "${CLASH_SUB_UID:-10001}:${CLASH_SUB_GID:-10001}"
    volumes:
      - ./private:/data/private:rw

  validator:
    image: docker.io/metacubex/mihomo:v1.19.28
    profiles: ["tools"]
    entrypoint: ["/mihomo"]
    volumes:
      - ./private/staging:/data/staging:ro
```

Set `publisher.command` to `python -m clash_sub.publisher`, `manager.entrypoint` to `python -m clash_sub.manager`, and validator entrypoint to `/mihomo` as provided by the official image. Publisher health check runs `python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=2)'`; subconverter health check requests its container-local `/version`. Add `tmpfs: /tmp`, `security_opt: ["no-new-privileges:true"]`, `cap_drop: ["ALL"]` where supported, and a shared outbound-capable application network. Do not mark the network `internal`, because subconverter and publisher must reach private HTTPS source URLs.

- [ ] **Step 5: Lock down subconverter configuration**

Keep API mode required for the internal manager, but configure no default URL, no profile exposure, no upload, no Gist, and no short-link function. The container has no host port, so `/sub` is unreachable from public Nginx.

- [ ] **Step 6: Validate Compose and run tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_compose -v
docker compose --env-file .env.example config
docker build -t my-clash-config:test .
```

Expected: tests PASS, Compose config exits 0, and the image builds successfully.

- [ ] **Step 7: Commit the new stack**

```bash
git add Dockerfile compose.yaml .env.example docker/subconverter/pref.ini tests/test_compose.py tests/test_generate_configs.py
git commit -m "feat: deploy private Clash publication stack"
```

---

### Task 11: Add Trojan/Nginx Preflight and Dry-Run-First Server Installer

**Files:**
- Create: `scripts/server_preflight.py`
- Create: `scripts/install-server.sh`
- Create: `deploy/nginx/clash-sub.conf.tmpl`
- Create: `tests/fixtures/nginx-sni.conf`
- Create: `tests/fixtures/trojan-safe.json`
- Create: `tests/test_server_preflight.py`
- Create: `tests/test_installer.py`

**Interfaces:**
- Consumes: `ss -lntp`, `nginx -T`, safe Trojan JSON fields, certificate SAN output, domain/certificate/key arguments, and the publisher loopback port.
- Produces: a redacted topology report and a reversible Nginx site installation.

- [ ] **Step 1: Write topology interpretation tests**

Fixture `trojan-safe.json` contains no password block:

```json
{
  "local_addr": "127.0.0.1",
  "local_port": 10443,
  "remote_addr": "127.0.0.1",
  "remote_port": 8080,
  "ssl": {
    "fallback_addr": "127.0.0.1",
    "fallback_port": 1443
  }
}
```

Test:

```python
class ServerPreflightTests(unittest.TestCase):
    def test_recognizes_expected_sni_and_fallback_topology(self):
        report = analyze_topology(
            listeners=LISTENERS,
            nginx_text=NGINX_FIXTURE.read_text(),
            trojan=safe_trojan_fields(TROJAN_FIXTURE),
            domain="sub.example.com",
        )
        self.assertTrue(report.ready)
        self.assertEqual(report.https_internal_port, 1443)
        self.assertEqual(report.trojan_remote_port, 8080)

    def test_unknown_443_owner_stops_installation(self):
        report = analyze_topology(
            listeners="LISTEN 0 511 0.0.0.0:443 users:((\"unknown\",pid=9,fd=3))",
            nginx_text="",
            trojan={},
            domain="sub.example.com",
        )
        self.assertFalse(report.ready)
```

Also test a missing SNI route, missing 1443 TLS listener, `remote_port` not matching 8080, `fallback_port` not matching 1443, certificate SAN missing the subscription domain, and output redaction when the source Trojan JSON contains a password field.

- [ ] **Step 2: Run preflight tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_server_preflight -v`

Expected: FAIL because `scripts.server_preflight` does not exist.

- [ ] **Step 3: Implement read-only preflight**

`server_preflight.py` supports:

```text
python3 scripts/server_preflight.py \
  --domain sub.example.com \
  --trojan-config /usr/local/etc/trojan/config.json \
  --certificate /path/to/fullchain.cer
```

It invokes `ss -lntp`, captures `nginx -T`, loads Trojan JSON but retains only `local_addr`, `local_port`, `remote_addr`, `remote_port`, `ssl.fallback_addr`, and `ssl.fallback_port`, and runs `openssl x509 -noout -ext subjectAltName`. The report never prints raw JSON or certificate private-key paths.

Return 0 only when:

- 443 is owned by the expected Nginx stream or a documented equivalent.
- The subscription SNI routes to the Web/1443 upstream, either explicitly or through a verified default.
- Nginx has a TLS virtual-host path on 1443.
- Trojan safe fields match a recognized topology.
- The certificate SAN covers the exact subscription domain or a matching wildcard.
- Publisher loopback port is not already occupied by another process.

- [ ] **Step 4: Write installer dry-run tests**

Run the shell script against temporary fake `nginx` and `systemctl` executables. Assert default invocation creates no files, `--apply` writes only the named site file and command symlink, failed `nginx -t` restores the previous site file, and reload is never called after failure.

- [ ] **Step 5: Implement the Nginx site template**

Use this exact public boundary:

```nginx
server {
    listen 1443 ssl;
    server_name __SUBSCRIPTION_DOMAIN__;

    ssl_certificate __CERTIFICATE_PATH__;
    ssl_certificate_key __PRIVATE_KEY_PATH__;

    access_log off;

    location ^~ /s/ {
        proxy_pass http://127.0.0.1:__PUBLISHER_PORT__;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_http_version 1.1;
        proxy_connect_timeout 5s;
        proxy_read_timeout 20s;
        client_max_body_size 1k;
    }

    location / {
        return 404;
    }
}
```

Do not add an Nginx public converter location, file root, autoindex, admin location, or history path.

- [ ] **Step 6: Implement dry-run-first installation**

`scripts/install-server.sh` accepts `--domain`, `--certificate`, `--private-key`, `--publisher-port`, and optional `--apply`. Without `--apply`, it runs preflight and prints planned destination files without changing the system.

With `--apply`, it:

1. Re-runs preflight.
2. Runs `docker compose config` before any Nginx write.
3. Backs up only an existing subscription site file to a timestamped sibling.
4. Renders the new site to a temporary file and atomically moves it into the detected Nginx include directory.
5. Installs `/usr/local/bin/clash-sub` as a symlink to the repository's `bin/clash-sub`.
6. Runs `nginx -t`.
7. Restores the backup or removes only the newly created site file if validation fails.
8. Reloads Nginx only after validation succeeds.
9. Starts `subconverter` and `publisher` through Compose.
10. Prints the `clash-sub` help text.

It never edits `/etc/nginx/nginx.conf`, the stream map, Trojan JSON, firewall rules, or an unrelated site automatically. If SNI routing is missing, it exits with a redacted instruction naming the missing domain mapping.

- [ ] **Step 7: Run preflight and installer tests**

Run: `.venv/bin/python -m unittest tests.test_server_preflight tests.test_installer -v`

Expected: all tests PASS.

- [ ] **Step 8: Commit deployment safety**

```bash
git add scripts/server_preflight.py scripts/install-server.sh deploy/nginx/clash-sub.conf.tmpl tests/fixtures/nginx-sni.conf tests/fixtures/trojan-safe.json tests/test_server_preflight.py tests/test_installer.py
git commit -m "feat: add safe Trojan and Nginx deployment preflight"
```

---

### Task 12: Replace Legacy Entry Points, Document Operations, and Verify End to End

**Files:**
- Modify: `README.md`
- Rewrite: `DEPLOYMENT.md`
- Create: `docs/operations.md`
- Modify: `docs/private-data.md`
- Delete: `scripts/generate_configs.py`
- Delete: `tests/test_generate_configs.py`
- Delete: `private/*.yaml.example`
- Delete: `generated/.gitkeep` if no tracked code references `generated/`

**Interfaces:**
- Consumes: all implementation tasks and the approved design.
- Produces: one supported workflow, complete operator documentation, and verification evidence.

- [ ] **Step 1: Write an end-to-end fixture test before removing legacy code**

Create `tests/test_end_to_end.py` that starts a local fake 3x-ui HTTP server returning a sanitized subscription and `Subscription-Userinfo`, uses a fake subconverter client, builds and publishes one friend plus owner, then calls the pure publisher service:

```python
class EndToEndTests(unittest.TestCase):
    def test_build_publish_and_download_preserve_isolation_and_traffic(self):
        friend_candidate = self.builder.build_candidate("friend")
        owner_candidate = self.builder.build_candidate("owner")
        publish_candidate(friend_candidate, self.private_root, keep=5)
        publish_candidate(owner_candidate, self.private_root, keep=5)

        friend = self.publisher.handle(
            Request("GET", "/s/friend-token/balanced.yaml", "198.51.100.30")
        )
        owner = self.publisher.handle(
            Request("GET", "/s/owner-token/privacy.yaml", "198.51.100.31")
        )

        self.assertEqual(friend.status, 200)
        self.assertIn("Subscription-Userinfo", friend.headers)
        self.assertNotIn(b"owner-home-node", friend.body)
        self.assertIn(b"owner-home-node", owner.body)
```

- [ ] **Step 2: Run the end-to-end test and verify it passes before cleanup**

Run: `.venv/bin/python -m unittest tests.test_end_to_end -v`

Expected: PASS using only synthetic credentials and temporary directories.

- [ ] **Step 3: Remove the obsolete generator and documentation paths**

Remove the public-provider generator, old private fragment examples, old generated directory, and old tests only after their replacement suites pass. Keep the 2026-08-19 design/plan files because they are explicitly marked obsolete history.

Rewrite README and deployment docs so they contain only:

- Project name `my-clash-config`.
- Source isolation table.
- `clash-sub` command summary.
- No scheduled regeneration explanation.
- Client-controlled subscription update explanation.
- 3x-ui `Limit IP` limitations.
- Airport update from mobile SSH.
- Last-known-good releases and rollback.
- Trojan/Nginx 443/1443/8080 topology and mandatory preflight.
- Dry-run and `--apply` deployment examples using `sub.example.com` only.

- [ ] **Step 4: Run the full local verification suite**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q clash_sub scripts tests
git diff --check
git status --short --ignored
```

Expected: all tests PASS, compilation exits 0, diff check emits nothing, and every file under `private/` is ignored.

- [ ] **Step 5: Run container and service verification**

Run:

```bash
docker compose --env-file .env.example config
docker compose build publisher manager
docker compose up -d subconverter publisher
docker compose ps
curl --fail --silent http://127.0.0.1:25501/healthz
docker compose --profile tools run --rm validator -v
```

Expected: Compose config and builds succeed, both services are healthy, loopback health returns success, and the validator reports its pinned Mihomo version. Confirm `ss -lntp` shows no new public listener on 80 or 443.

- [ ] **Step 6: Run a sanitized real-shape release rehearsal**

Using copied synthetic settings and synthetic node fixtures, run:

```bash
bin/clash-sub
bin/clash-sub status
bin/clash-sub refresh
bin/clash-sub history owner
```

Expected: no-argument help displays, status contains no credentials, refresh publishes validated releases, and history lists successful release IDs only.

- [ ] **Step 7: Commit the supported workflow**

```bash
git add README.md DEPLOYMENT.md docs/operations.md docs/private-data.md tests/test_end_to_end.py
git rm scripts/generate_configs.py tests/test_generate_configs.py private/proxies.yaml.example private/proxy-groups.yaml.example private/rules.yaml.example
git commit -m "docs: document private Clash subscription operations"
```

If `generated/.gitkeep` is unused, include it in the same `git rm` and commit.

- [ ] **Step 8: Request code review and run completion verification**

Use `superpowers:requesting-code-review`, fix confirmed findings through `superpowers:receiving-code-review`, then use `superpowers:verification-before-completion` to re-run Steps 4–6 from fresh state.

- [ ] **Step 9: Rename the remote repository after explicit external-action confirmation**

After the implementation branch is integrated and the user confirms the external rename, rename the private GitHub repository to `my-clash-config`, then update and verify the local remote:

```bash
git remote set-url origin git@github.com:42vio/my-clash-config.git
git remote -v
git ls-remote --exit-code origin HEAD
```

Rename the local workspace directory only after the Codex task is closed, so the active workspace is not invalidated:

```bash
cd /Users/42vio/Workspace
mv my-mihomo-config my-clash-config
```

The rename step does not alter or publish any private runtime file.

---

## Execution Checkpoints

1. After Task 1, verify reference files are ignored and recoverable before touching templates.
2. After Task 4, review the three structural comparison reports before accepting template migration.
3. After Task 6, inspect cross-user isolation and failed-release tests before adding any HTTP endpoint.
4. After Task 8, review bearer-token behavior and traffic-header fallbacks before exposing publisher through Nginx.
5. After Task 11, review the real server's redacted preflight report before running installer `--apply`.
6. After Task 12, do not rename the GitHub repository until all fresh verification and code review are complete.
