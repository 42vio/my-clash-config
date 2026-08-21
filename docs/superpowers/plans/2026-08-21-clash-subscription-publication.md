# Clash Private Subscription Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a private per-user Clash configuration generator and read-only subscription publisher that converts independent 3x-ui subscriptions, adds owner-only airport and home nodes, renders three complete configurations from one template, and deploys safely beside a native 3x-ui REALITY service on a clean VPS.

**Architecture:** Python services load strict private settings, call a loopback-only MetaCubeX/subconverter, normalize and validate expanded Mihomo nodes, render `balanced`, `balanced-win`, and `privacy`, then atomically publish per-user releases. A read-only publisher serves only current releases through opaque bearer-token paths; native Xray owns public TCP 443, while host Nginx owns TCP 80/8443 and proxies only the panel and subscription paths. Because Docker bridge containers cannot reach host `127.0.0.1`, manager and publisher use Linux host networking but bind their own HTTP listeners only to loopback.

**Tech Stack:** Python 3.13, `unittest`, Jinja2 3.1.6, PyYAML 6.0.3, MetaCubeX/subconverter 0.9.2, Mihomo 1.19.30, 3x-ui 3.6.0, Xray-core 26.6.27, Certbot 5.7.0, Docker Compose, Nginx, UFW, systemd, Debian 12 amd64.

**Spec:** `docs/superpowers/specs/2026-08-21-clash-subscription-publication-design.md`

## Global Constraints

- Public naming is **Clash**. Use `MetaCubeX/subconverter` and `Mihomo` only when naming those upstream projects.
- The target repository name is `my-clash-config` and the only installed management command is `clash-sub`; do not add aliases such as `clashctl` or `refresh-all`.
- Initial production target is a clean Debian 12 amd64 VPS. Repository code remains Python 3.9-compatible even though the container runtime is Python 3.13.
- Pin 3x-ui to `3.6.0`, Xray-core to `26.6.27`, Mihomo to `1.19.30`, MetaCubeX/subconverter to `0.9.2`, Certbot to `5.7.0`, Jinja2 to `3.1.6`, and PyYAML to `6.0.3`. Never follow `latest` automatically.
- Do not upgrade Xray to `26.7.11` or later until a separate Mihomo REALITY compatibility test is approved and passes.
- Public TCP 443 belongs only to native Xray VLESS + RAW/TCP + REALITY. Public TCP 8443 belongs only to Nginx HTTPS. TCP 80 serves ACME HTTP-01 and a generic response. Do not open UDP 443 or public 1443.
- 3x-ui panel and raw subscription listeners must bind `127.0.0.1`. Publisher and subconverter must also bind only host loopback. No converter endpoint, `/sub`, `/getprofile`, file browser, upload endpoint, or web admin page is public.
- The repository installer never downloads or executes the 3x-ui installer. Native 3x-ui installation, strong admin credentials, random Web Base Path, 2FA, raw-subscription loopback binding, REALITY target choice, and first client creation are human prerequisites.
- Other users receive only their own 3x-ui client. Owner receives owner 3x-ui plus the latest validated airport snapshot and owner-maintained home nodes. No Jrohy/Trojan source remains.
- Never commit, log, or print a real domain, VPS IP, source subscription URL, public bearer token, UUID, node password, REALITY private key, airport temporary URL, generated configuration, or release metadata containing credentials.
- Treat the current untracked `1/` directory as user-owned secret data. Move the three exact files without printing or parsing them in Task 1; parse them only through the path-only comparison tool in Task 4. Never stage `1/` or `private/`.
- Do not schedule configuration regeneration. Builds occur only on initial setup, explicit `clash-sub refresh`, successful airport import, or an explicit refresh after source/template changes.
- Certificate renewal and certificate-health checks may be scheduled, but they must never invoke configuration generation.
- A failed source conversion, structural validation, Mihomo validation, or publish operation must leave the current release unchanged.
- Publish all allowed variants for one user atomically and keep exactly the newest five successful releases per user. Reference originals are never pruned.
- The default installer mode is read-only preflight. System changes require `--apply` and separate user approval at execution time. Reinstalling the OS, changing DNS, applying firewall rules, issuing certificates, renaming the remote repository, and modifying the live VPS are external actions that require explicit confirmation.
- Preserve unrelated user changes. Use `apply_patch` for tracked text edits, never `git add -A`, and stage only files named by the current task.
- Use TDD for every behavior change: add one focused failing test, run it and observe the expected failure, implement the minimum behavior, run the focused and affected suites, then commit.

---

## Target File Structure

| Path | Responsibility |
| --- | --- |
| `clash_sub/models.py` | Immutable service, user, source, traffic, candidate, release, request, and response types. |
| `clash_sub/settings.py` | Strictly parse `service.yaml` and `users.yaml`, protect local paths, hash and rotate tokens. |
| `clash_sub/converter.py` | Bounded calls to loopback MetaCubeX/subconverter, local snapshot loading, source merging, and REALITY normalization. |
| `clash_sub/traffic.py` | Parse and fetch bounded 3x-ui `Subscription-Userinfo` metadata. |
| `clash_sub/rendering.py` | Render one common Clash template with three declarative variants. |
| `clash_sub/validation.py` | Validate complete YAML, references, REALITY fields, source leakage, and file hashes. |
| `clash_sub/releases.py` | Build staging candidates, atomically publish, retain five releases, list history, and roll back. |
| `clash_sub/manager.py` | Machine-facing JSON commands for build, publish, airport import, status, history, rollback, and token rotation. |
| `clash_sub/publisher.py` | Read-only loopback HTTP server, token authorization, traffic cache, rate limiting, and sanitized logs. |
| `clash_sub/host_cli.py` | User-facing `clash-sub` help and Docker/Mihomo orchestration. |
| `config/service.example.yaml` | Non-operational global private-settings example. |
| `config/users.example.yaml` | Non-operational owner and ordinary-user schema example. |
| `templates/clash.yaml.j2` | Shared complete Clash template with strict proxy and variant-section insertion points derived from the references. |
| `templates/variants/*.yaml` | The actual balanced, balanced-win, and privacy differences and node-injection group names. |
| `bin/clash-sub` | Thin Python entry point for `clash_sub.host_cli:main`. |
| `Dockerfile` | Fixed non-root application runtime. |
| `compose.yaml` | Loopback subconverter, host-network publisher/manager, and one-shot Mihomo validator. |
| `deploy/nginx/00-acme-http.conf.tmpl` | Port 80 ACME webroot plus generic fallback. |
| `deploy/nginx/10-clash-domain.conf.tmpl` | Domain-mode 8443 default, panel, and subscription virtual hosts. |
| `deploy/nginx/10-clash-ip.conf.tmpl` | IP-mode 8443 path routing for panel and subscription. |
| `deploy/systemd/clash-sub-cert-check.service` | Certificate expiry and renewal-state check. |
| `deploy/systemd/clash-sub-cert-check.timer` | Daily certificate check only. |
| `scripts/check_reality_target.py` | Read-only TLS 1.3, ALPN, X25519, reachability, and certificate-name target test. |
| `scripts/check_certificate.py` | Certificate validity check, sanitized state file, and optional alert-command invocation. |
| `scripts/server_preflight.py` | Redacted clean-host, listener, 3x-ui/Xray, DNS, Compose, and firewall preflight. |
| `scripts/install_server.py` | Testable dry-run/apply deployment engine with backup and rollback. |
| `scripts/install-server.sh` | One-command wrapper that invokes `install_server.py`. |
| `tests/fixtures/` | Synthetic subscriptions, configurations, listener reports, and Nginx/system command output. |
| `tests/test_*.py` | Focused unit, security, integration, deployment, and end-to-end tests. |
| `docs/3x-ui-setup.md` | Manual pinned 3x-ui/Xray and REALITY initialization checklist. |
| `docs/private-data.md` | Private layout, permissions, backup, restore, and reference retention. |
| `docs/operations.md` | Mobile airport update, refresh, status, link rotation, history, rollback, and incident recovery. |

Private runtime data stays under ignored `private/` exactly as defined by the approved spec.

---

## Milestone A — Safe data and configuration generation

### Task 1: Quarantine Reference Configs and Establish Repository Safety

**Files:**
- Modify: `.gitignore`
- Create: `config/service.example.yaml`
- Create: `config/users.example.yaml`
- Create: `docs/private-data.md`
- Create: `tests/test_repository_safety.py`
- Modify: `tests/test_generate_configs.py`
- Move without staging: `1/My-Clash_Balanced.yaml` → `private/reference-configs/2026-08-21/My-Clash_Balanced.yaml`
- Move without staging: `1/My-Clash_Balanced_Win.yaml` → `private/reference-configs/2026-08-21/My-Clash_Balanced_Win.yaml`
- Move without staging: `1/My-Clash_Privacy.yaml` → `private/reference-configs/2026-08-21/My-Clash_Privacy.yaml`

**Interfaces:**
- Consumes: the current tracked repository and three untracked authoritative files, without reading their contents.
- Produces: ignored private roots, `0700`/`0600` permission policy, and safe schema examples consumed by Task 2.

- [ ] **Step 1: Write failing repository-safety tests**

Create `tests/test_repository_safety.py`:

```python
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositorySafetyTests(unittest.TestCase):
    def test_every_runtime_private_path_is_ignored(self):
        paths = (
            "private/config/service.yaml",
            "private/config/users.yaml",
            "private/reference-configs/2026-08-21/reference.yaml",
            "private/sources/owner/airport.yaml",
            "private/sources/owner/home.yaml",
            "private/staging/op/user/config.yaml",
            "private/releases/user/release/config.yaml",
            "private/current/user",
            "private/state/certificate.json",
            "private/logs/operations.jsonl",
        )
        for path in paths:
            result = subprocess.run(
                ["git", "check-ignore", "-q", path],
                cwd=ROOT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, path)

    def test_reference_sources_are_not_tracked(self):
        for name in (
            "My-Clash_Balanced.yaml",
            "My-Clash_Balanced_Win.yaml",
            "My-Clash_Privacy.yaml",
        ):
            result = subprocess.run(
                ["git", "ls-files", "--error-unmatch", f"1/{name}"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, name)
```

Replace the real-value fragments currently stored in `FORBIDDEN_SUBSTRINGS` inside `tests/test_generate_configs.py` with synthetic values from RFC 5737, `example.com`, and synthetic UUID/password fixtures. Tests must not preserve real private indicators.

- [ ] **Step 2: Run the safety test and verify the expected failure**

Run: `python3 -m unittest tests.test_repository_safety -v`

Expected: FAIL because `private/config`, `private/reference-configs`, `private/releases`, and `private/state` are not all ignored yet.

- [ ] **Step 3: Replace the ignore policy and add safe examples**

Use this complete private-data portion in `.gitignore`:

```gitignore
.worktrees/
.venv/
.env
private/**
generated/**
__pycache__/
*.py[cod]
```

Create `config/service.example.yaml`:

```yaml
schema-version: 1
private-root: /data/private
converter-base-url: http://127.0.0.1:25500
publication:
  mode: domain
  subscription-authority: sub.example.com:8443
  panel-authority: panel.example.com:8443
  publisher-listen: 127.0.0.1
  publisher-port: 25501
reality:
  public-address: 192.0.2.10
  public-port: 443
  required-flow: xtls-rprx-vision
xui:
  panel-listen: 127.0.0.1
  panel-port: 2053
  panel-base-path: /example-random-panel-path/
  subscription-listen: 127.0.0.1
  subscription-port: 2096
  xray-config-path: /usr/local/x-ui/bin/config.json
  xray-binary-path: /usr/local/x-ui/bin/xray-linux-amd64
  expected-panel-version: 3.6.0
  expected-xray-version: 26.6.27
certificate:
  fullchain-path: /etc/letsencrypt/live/panel.example.com/fullchain.pem
  alert-before-seconds: 1209600
  alert-command: []
```

Create `config/users.example.yaml`:

```yaml
schema-version: 1
users:
  owner:
    role: owner
    token-sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    variants: [balanced, balanced-win, privacy]
    xui-subscription-url: http://127.0.0.1:2096/sub/example-owner-sub-id
    local-sources:
      airport: sources/owner/airport.yaml
      home: sources/owner/home.yaml
  friend:
    role: member
    token-sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    variants: [balanced]
    xui-subscription-url: http://127.0.0.1:2096/sub/example-friend-sub-id
    local-sources: {}
```

In `docs/private-data.md`, state that examples are copied to `private/config/`, directories use `0700`, files use `0600`, current links are relative symlinks, references are permanent, and neither Git nor backups leave the administrator-controlled encrypted storage.

- [ ] **Step 4: Move the three reference files without displaying them**

Run these exact commands one at a time:

```bash
install -d -m 700 private/reference-configs/2026-08-21
mv -- 1/My-Clash_Balanced.yaml private/reference-configs/2026-08-21/My-Clash_Balanced.yaml
mv -- 1/My-Clash_Balanced_Win.yaml private/reference-configs/2026-08-21/My-Clash_Balanced_Win.yaml
mv -- 1/My-Clash_Privacy.yaml private/reference-configs/2026-08-21/My-Clash_Privacy.yaml
chmod 600 private/reference-configs/2026-08-21/My-Clash_Balanced.yaml
chmod 600 private/reference-configs/2026-08-21/My-Clash_Balanced_Win.yaml
chmod 600 private/reference-configs/2026-08-21/My-Clash_Privacy.yaml
rmdir 1
```

Do not run `cat`, `head`, `sed`, `git diff --no-index`, or `git add` on those files. Verify only names, modes, and ignored status:

```bash
find private/reference-configs/2026-08-21 -maxdepth 1 -type f -exec stat -f '%N %Lp' {} \;
git status --short --ignored
```

On Linux, use `stat -c '%n %a'` instead of the macOS `stat -f` form.

- [ ] **Step 5: Run safety and legacy tests**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests PASS, no output includes reference contents, and `git status --short` does not list `private/`.

- [ ] **Step 6: Commit only tracked safety files**

```bash
git add .gitignore config/service.example.yaml config/users.example.yaml docs/private-data.md tests/test_repository_safety.py tests/test_generate_configs.py
git commit -m "chore: protect private Clash runtime data"
```

### Task 2: Add Strict Service, User, Source, and Token Models

**Files:**
- Create: `requirements.txt`
- Create: `clash_sub/__init__.py`
- Create: `clash_sub/models.py`
- Create: `clash_sub/settings.py`
- Create: `tests/test_settings.py`

**Interfaces:**
- Consumes: `config/service.example.yaml` and `config/users.example.yaml` from Task 1.
- Produces: `load_settings(service_path: Path, users_path: Path) -> Settings`, `hash_token(token: str) -> str`, and `rotate_user_token(users_path: Path, settings: Settings, user_id: str) -> TokenRotation`.

- [ ] **Step 1: Pin Python dependencies and write failing settings tests**

Create `requirements.txt`:

```text
Jinja2==3.1.6
PyYAML==6.0.3
```

Create an ignored environment and install only the pinned requirements:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Create `tests/test_settings.py` with focused cases including:

```python
class SettingsTests(unittest.TestCase):
    def test_member_cannot_declare_owner_local_sources(self):
        service_path, users_path = self.write_settings(
            users={
                "friend": {
                    "role": "member",
                    "token-sha256": "a" * 64,
                    "variants": ["balanced"],
                    "xui-subscription-url": "http://127.0.0.1:2096/sub/friend",
                    "local-sources": {"home": "sources/owner/home.yaml"},
                }
            }
        )
        with self.assertRaisesRegex(SettingsError, "friend.*local-sources"):
            load_settings(service_path, users_path)

    def test_remote_xui_url_must_be_loopback_http(self):
        service_path, users_path = self.write_settings(
            users={
                "friend": {
                    "role": "member",
                    "token-sha256": "a" * 64,
                    "variants": ["balanced"],
                    "xui-subscription-url": "http://192.0.2.20:2096/sub/friend",
                    "local-sources": {},
                }
            }
        )
        with self.assertRaisesRegex(SettingsError, "loopback"):
            load_settings(service_path, users_path)

    def test_hash_token_is_stable_without_storing_plaintext(self):
        self.assertEqual(
            hash_token("sample-token"),
            "0f35d0ae14518b96bd6d3fec3ca15801fd58c9e048b1ccdea11a71378f2acdc9",
        )
```

Also cover unknown keys, malformed YAML, invalid authorities, invalid IPv4/IPv6 values, non-loopback panel/subscription/converter listeners, publisher listen other than `127.0.0.1`, port conflicts, unknown variants, duplicate owners, invalid token hashes, path traversal outside `private-root`, world-readable private files, and domain/IP publication modes.

- [ ] **Step 2: Run settings tests and observe the missing-module failure**

Run: `.venv/bin/python -m unittest tests.test_settings -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'clash_sub'`.

- [ ] **Step 3: Implement immutable public models**

Define these exact types in `clash_sub/models.py` using Python 3.9-compatible annotations:

```python
VARIANTS = ("balanced", "balanced-win", "privacy")
LOCAL_SOURCE_KINDS = ("airport", "home")


@dataclass(frozen=True)
class PublicationSettings:
    mode: str
    subscription_authority: str
    panel_authority: str
    publisher_listen: str
    publisher_port: int


@dataclass(frozen=True)
class RealitySettings:
    public_address: str
    public_port: int
    required_flow: str


@dataclass(frozen=True)
class XuiSettings:
    panel_listen: str
    panel_port: int
    panel_base_path: str
    subscription_listen: str
    subscription_port: int
    xray_config_path: Path
    xray_binary_path: Path
    expected_panel_version: str
    expected_xray_version: str


@dataclass(frozen=True)
class CertificateSettings:
    fullchain_path: Path
    alert_before_seconds: int
    alert_command: Tuple[str, ...]


@dataclass(frozen=True)
class ServiceSettings:
    private_root: Path
    converter_base_url: str
    publication: PublicationSettings
    reality: RealitySettings
    xui: XuiSettings
    certificate: CertificateSettings


@dataclass(frozen=True)
class SourceSpec:
    kind: str
    label: str
    url: Optional[str] = None
    path: Optional[Path] = None


@dataclass(frozen=True)
class UserSpec:
    user_id: str
    role: str
    token_sha256: str
    variants: Tuple[str, ...]
    xui_source: SourceSpec
    local_sources: Tuple[SourceSpec, ...]

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


@dataclass(frozen=True)
class Settings:
    service: ServiceSettings
    users: Mapping[str, UserSpec]


@dataclass(frozen=True)
class TokenRotation:
    user_id: str
    token: str
    urls: Mapping[str, str]
```

- [ ] **Step 4: Implement strict parsing and one-time token rotation**

In `clash_sub/settings.py`:

```python
class SettingsError(ValueError):
    """Raised when private service or user settings are unsafe or invalid."""


def load_settings(service_path: Path, users_path: Path) -> Settings:
    """Load strict YAML settings and return only resolved immutable models."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def rotate_user_token(
    users_path: Path,
    settings: Settings,
    user_id: str,
) -> TokenRotation:
    """Atomically store only a SHA-256 hash and return plaintext once."""
```

Use `yaml.safe_load`, reject unknown keys at every level, require exactly zero or one owner, reject duplicate variants, and require a 64-character lowercase hexadecimal token hash. Resolve local source paths with `Path.resolve()` and `relative_to(private_root.resolve())`. Permit source/converter HTTP only when the parsed host is exactly `127.0.0.1` or `::1`; public authorities contain no scheme and must include port `8443`.

`rotate_user_token()` uses `secrets.token_urlsafe(32)`, writes the updated YAML through a `0600` temporary sibling plus `os.replace()`, and constructs URLs from `subscription_authority` and the user variant allowlist. It never persists the plaintext token.

- [ ] **Step 5: Run settings and repository-safety tests**

Run: `.venv/bin/python -m unittest tests.test_settings tests.test_repository_safety -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit the settings foundation**

```bash
git add requirements.txt clash_sub/__init__.py clash_sub/models.py clash_sub/settings.py tests/test_settings.py
git commit -m "feat: add strict private subscription settings"
```

### Task 3: Convert Sources, Normalize REALITY, and Read Traffic Metadata

**Files:**
- Create: `clash_sub/converter.py`
- Create: `clash_sub/traffic.py`
- Create: `tests/fixtures/reality-subscription.txt`
- Create: `tests/fixtures/reality-converted.yaml`
- Create: `tests/test_converter.py`
- Create: `tests/test_traffic.py`

**Interfaces:**
- Consumes: `SourceSpec` and `RealitySettings` from Task 2, loopback converter URL, 3x-ui source URLs, and local airport/home YAML snapshots.
- Produces: `SubconverterClient.convert(source_url: str) -> Tuple[Mapping[str, object], ...]`, `load_local_proxies(path: Path) -> Tuple[Mapping[str, object], ...]`, `normalize_reality_proxy(proxy, reality) -> Mapping[str, object]`, `merge_proxy_sources(sources) -> Tuple[Mapping[str, object], ...]`, and `TrafficClient.fetch(source_url: str) -> Optional[SubscriptionUserinfo]`.

- [ ] **Step 1: Create synthetic REALITY fixtures and failing conversion tests**

`tests/fixtures/reality-subscription.txt` contains one synthetic VLESS URI using the UUID `00000000-0000-4000-8000-000000000001`, host `192.0.2.10`, TCP 443, `xtls-rprx-vision`, `www.example.com`, a synthetic public key, non-empty short ID `0123456789abcdef`, and `fp=chrome`.

`tests/fixtures/reality-converted.yaml` contains:

```yaml
proxies:
  - name: Example REALITY
    type: vless
    server: 192.0.2.10
    port: 443
    uuid: 00000000-0000-4000-8000-000000000001
    network: tcp
    tls: true
    udp: true
    flow: xtls-rprx-vision
    servername: www.example.com
    client-fingerprint: chrome
    reality-opts:
      public-key: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
      short-id: 0123456789abcdef
```

Create focused tests:

```python
class ConverterTests(unittest.TestCase):
    def test_build_url_encodes_source_and_requests_clash_node_list(self):
        client = SubconverterClient("http://127.0.0.1:25500", opener=FakeOpener())
        url = client.build_url("http://127.0.0.1:2096/sub/example?a=1&b=2")
        self.assertIn("target=clash", url)
        self.assertIn("list=true", url)
        self.assertIn("a%3D1%26b%3D2", url)

    def test_reality_normalization_forces_public_address_and_keeps_credentials(self):
        proxy = yaml.safe_load(REALITY_YAML)["proxies"][0]
        normalized = normalize_reality_proxy(
            proxy,
            RealitySettings("198.51.100.25", 443, "xtls-rprx-vision"),
        )
        self.assertEqual(normalized["server"], "198.51.100.25")
        self.assertEqual(normalized["port"], 443)
        self.assertEqual(normalized["uuid"], proxy["uuid"])
        self.assertEqual(normalized["reality-opts"], proxy["reality-opts"])

    def test_response_over_limit_does_not_echo_source_url(self):
        source_url = "http://127.0.0.1:2096/sub/private-value"
        with self.assertRaisesRegex(SourceError, "response exceeds") as context:
            SubconverterClient(
                "http://127.0.0.1:25500",
                opener=FakeOpener(b"x" * 1025),
                max_bytes=1024,
            ).convert(source_url)
        self.assertNotIn(source_url, str(context.exception))
```

Add tests for missing `proxies`, non-list roots, non-mapping proxy entries, empty outputs, duplicate names, malformed local snapshots, duplicate-name suffixes, immutability of input mappings, non-VLESS 3x-ui nodes, VLESS without REALITY, WebSocket/XHTTP rejection for self-hosted nodes, empty short ID, missing public key, missing SNI, missing client fingerprint, and wrong flow. The normalizer is used only for the 3x-ui source and rejects every non-VLESS or incomplete-REALITY entry; airport/home nodes bypass this normalizer and are validated as their declared protocols later.

- [ ] **Step 2: Write failing traffic-header tests**

Create `tests/test_traffic.py`:

```python
class TrafficTests(unittest.TestCase):
    def test_valid_header_is_canonicalized(self):
        value = "download=20; upload=10; expire=1893456000; total=100"
        info = parse_subscription_userinfo(value)
        self.assertEqual(info.remaining, 70)
        self.assertEqual(
            info.header_value,
            "upload=10; download=20; total=100; expire=1893456000",
        )

    def test_unlimited_total_has_no_numeric_remaining(self):
        info = parse_subscription_userinfo(
            "upload=10; download=20; total=0; expire=0"
        )
        self.assertIsNone(info.remaining)
```

Also test missing headers, unknown fields, duplicate fields, negative/non-integer values, response closure without body reads, 10-second timeout propagation, and errors that omit the source URL.

- [ ] **Step 3: Run the new tests and observe missing-module failures**

Run: `.venv/bin/python -m unittest tests.test_converter tests.test_traffic -v`

Expected: FAIL because `clash_sub.converter` and `clash_sub.traffic` do not exist.

- [ ] **Step 4: Implement bounded conversion and local loading**

Define in `clash_sub/converter.py`:

```python
class SourceError(RuntimeError):
    """Raised when a source cannot be converted into a safe proxy list."""


class SubconverterClient:
    def __init__(
        self,
        base_url: str,
        opener=urlopen,
        timeout: int = 20,
        max_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = opener
        self.timeout = timeout
        self.max_bytes = max_bytes

    def build_url(self, source_url: str) -> str:
        query = urlencode(
            {"target": "clash", "url": source_url, "list": "true"}
        )
        return f"{self.base_url}/sub?{query}"

    def convert(self, source_url: str) -> Tuple[Mapping[str, object], ...]:
        request = Request(
            self.build_url(source_url),
            headers={"Accept": "text/yaml"},
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = response.read(self.max_bytes + 1)
        except (OSError, HTTPError, URLError) as error:
            raise SourceError("source conversion failed") from error
        if len(payload) > self.max_bytes:
            raise SourceError("converter response exceeds size limit")
        try:
            document = yaml.safe_load(payload.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as error:
            raise SourceError("converter returned invalid YAML") from error
        if not isinstance(document, dict) or not isinstance(
            document.get("proxies"), list
        ):
            raise SourceError("converter response has no proxy list")
        proxies = tuple(copy.deepcopy(document["proxies"]))
        if not proxies or not all(isinstance(item, dict) for item in proxies):
            raise SourceError("converter returned invalid proxies")
        return proxies


def load_local_proxies(path: Path) -> Tuple[Mapping[str, object], ...]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise SourceError("local source is unreadable or invalid") from error
    if not isinstance(document, dict) or not isinstance(
        document.get("proxies"), list
    ):
        raise SourceError("local source has no proxy list")
    proxies = tuple(copy.deepcopy(document["proxies"]))
    if not proxies or not all(isinstance(item, dict) for item in proxies):
        raise SourceError("local source contains invalid proxies")
    return proxies


def normalize_reality_proxy(
    proxy: Mapping[str, object],
    reality: RealitySettings,
) -> Mapping[str, object]:
    normalized = copy.deepcopy(dict(proxy))
    if normalized.get("type") != "vless":
        raise SourceError("3x-ui source contains a non-VLESS node")
    require_complete_reality_fields(normalized, reality)
    normalized["server"] = reality.public_address
    normalized["port"] = reality.public_port
    return normalized


def merge_proxy_sources(
    sources: Sequence[Tuple[str, Sequence[Mapping[str, object]]]],
) -> Tuple[Mapping[str, object], ...]:
    occurrences = Counter(
        str(proxy.get("name"))
        for _label, proxies in sources
        for proxy in proxies
    )
    used = set()
    merged = []
    for label, proxies in sources:
        for proxy in proxies:
            copied = copy.deepcopy(dict(proxy))
            original = str(copied.get("name"))
            base = original if occurrences[original] == 1 else f"{original} [{label}]"
            copied["name"] = unique_name(base, used)
            used.add(copied["name"])
            merged.append(copied)
    return tuple(merged)
```

Implement `require_complete_reality_fields()` as the single field gate used here and by validation: it requires `network: tcp`, `tls: true`, the expected flow, SNI, `client-fingerprint`, `reality-opts.public-key`, and non-empty `reality-opts.short-id`. Implement `unique_name()` to return the base name when unused, otherwise append `-2`, `-3`, and so on. No exception includes a source URL, response body, proxy mapping, or credential.

`merge_proxy_sources()` keeps unique names unchanged. For collisions, append only `[3x-ui]`, `[机场]`, or `[家庭]` to colliding names, then `-2`, `-3` when the labeled result also exists. It never mutates cached inputs.

- [ ] **Step 5: Implement strict traffic metadata parsing**

Add to `clash_sub/models.py`:

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

Define in `clash_sub/traffic.py`:

```python
def parse_subscription_userinfo(value: str) -> SubscriptionUserinfo:
    fields = {}
    for item in value.split(";"):
        key, separator, raw_value = item.strip().partition("=")
        if separator != "=" or key in fields:
            raise TrafficError("invalid subscription metadata")
        if key not in {"upload", "download", "total", "expire"}:
            raise TrafficError("unknown subscription metadata field")
        if not raw_value.isdecimal():
            raise TrafficError("subscription metadata must be non-negative")
        fields[key] = int(raw_value, 10)
    if set(fields) != {"upload", "download", "total", "expire"}:
        raise TrafficError("incomplete subscription metadata")
    return SubscriptionUserinfo(**fields)


class TrafficClient:
    def __init__(self, opener=urlopen, timeout: int = 10) -> None:
        self.opener = opener
        self.timeout = timeout

    def fetch(self, source_url: str) -> Optional[SubscriptionUserinfo]:
        request = Request(source_url, headers={"Accept": "*/*"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                value = response.headers.get("Subscription-Userinfo")
        except (OSError, HTTPError, URLError) as error:
            raise TrafficError("subscription metadata fetch failed") from error
        if value is None:
            return None
        return parse_subscription_userinfo(value)
```

Add `TrafficError(RuntimeError)` with a descriptive class docstring. `TrafficClient.fetch()` reads only headers, closes the response immediately, and returns `None` when the header is absent.

- [ ] **Step 6: Run focused source tests**

Run: `.venv/bin/python -m unittest tests.test_converter tests.test_traffic tests.test_settings -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit source conversion**

```bash
git add clash_sub/models.py clash_sub/converter.py clash_sub/traffic.py tests/fixtures/reality-subscription.txt tests/fixtures/reality-converted.yaml tests/test_converter.py tests/test_traffic.py
git commit -m "feat: normalize private Clash node sources"
```

### Task 4: Derive One Template and Three Variants from the Authoritative References

**Files:**
- Create: `clash_sub/rendering.py`
- Create: `scripts/migrate_reference_templates.py`
- Create: `scripts/compare_reference_configs.py`
- Create: `templates/clash.yaml.j2`
- Create: `templates/variants/balanced.yaml`
- Create: `templates/variants/balanced-win.yaml`
- Create: `templates/variants/privacy.yaml`
- Create: `tests/test_rendering.py`
- Delete after replacement passes: `templates/_base.yaml.tmpl`
- Delete after replacement passes: `templates/parts/dns-balanced.part`
- Delete after replacement passes: `templates/parts/dns-privacy.part`
- Delete after replacement passes: `templates/parts/geoip-resolve.part`
- Delete after replacement passes: `templates/parts/geoip-no-resolve.part`

**Interfaces:**
- Consumes: the three ignored reference files and normalized proxy mappings from Task 3.
- Produces: `load_variant(template_dir: Path, variant: str) -> VariantSpec`, `render_variant(template_dir: Path, variant: str, proxies: Sequence[Mapping[str, object]]) -> str`, and path-only structural comparison output.

- [ ] **Step 1: Write failing rendering and strict-template tests**

Create `tests/test_rendering.py`:

```python
class RenderingTests(unittest.TestCase):
    def test_all_variants_render_complete_expanded_documents(self):
        rendered = {
            variant: yaml.safe_load(
                render_variant(TEMPLATE_DIR, variant, SYNTHETIC_PROXIES)
            )
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

    def test_nodes_are_injected_only_into_declared_groups(self):
        variant = load_variant(TEMPLATE_DIR, "balanced")
        document = yaml.safe_load(
            render_variant(TEMPLATE_DIR, "balanced", SYNTHETIC_PROXIES)
        )
        groups = {item["name"]: item for item in document["proxy-groups"]}
        for group_name in variant.inject_node_groups:
            self.assertIn("Synthetic Node", groups[group_name]["proxies"])
        for group_name, group in groups.items():
            if group_name not in variant.inject_node_groups:
                self.assertNotIn("Synthetic Node", group.get("proxies", []))

    def test_strict_undefined_rejects_unknown_template_marker(self):
        with self.assertRaises(jinja2.UndefinedError):
            render_text("{{ UNKNOWN_MARKER }}", {})
```

Add tests for Unicode names, YAML-special password characters, stable key/list ordering, variant metadata removal, missing/duplicate injection groups, no leftover Jinja markers, and no source URL in rendered output.

- [ ] **Step 2: Run rendering tests and observe the expected failure**

Run: `.venv/bin/python -m unittest tests.test_rendering -v`

Expected: FAIL because `clash_sub.rendering` and the new template layout do not exist.

- [ ] **Step 3: Implement a non-printing migration tool**

`scripts/migrate_reference_templates.py` takes exactly:

```text
--reference-dir private/reference-configs/2026-08-21
--template-dir templates
--home-output private/sources/owner/home.yaml
```

Its implementation performs this deterministic transformation without printing scalar values:

1. Load all three files with `yaml.safe_load` and require mapping roots.
2. Require the inline `proxies` lists to be equal across all three references; atomically write that list as `proxies: [...]` to the ignored `home-output` with mode `0600`.
3. Record all inline proxy names and all `proxy-providers` names in memory.
4. Delete top-level `proxies` and `proxy-providers` from every tracked template candidate. This removes the obsolete Jrohy/Trojan provider and every previous upstream URL.
5. In each proxy group, remove inline proxy names from `proxies`, remove provider names from `use`, delete an empty `use` key, and record the group in `_generator.inject-node-groups` when either operation removed an item.
6. Put top-level keys whose full values are identical across all three references into `templates/clash.yaml.j2` as literal YAML in the first reference's order.
7. At each differing top-level key's position, put one strict Jinja marker for that complete root entry; store that key's complete value in each matching variant file. Put the strict proxy-list marker at the original `proxies` position. Require all variants to contain exactly the same differing-key set.
8. Refuse to write when a reference contains an unknown root type, duplicate proxy/group name, unresolved group target, source URL outside `rule-providers`, or a private provider left after transformation.
9. Print only counts and YAML paths such as `balanced: proxy-groups[4].use`; never print proxy mappings, node names, URLs, UUIDs, passwords, or keys.

The generated common template uses these marker forms:

```text
{{ PROXIES_ROOT_YAML }}
{{ VARIANT_<SAFE_ROOT_KEY>_ROOT_YAML }}
```

`<SAFE_ROOT_KEY>` is the upper snake-case form of the real root key and is generated only from keys present in all three references. Each marker expands one complete root YAML entry, including its key, at the first reference's original position. All common entries remain literal YAML. The migration refuses marker collisions, Jinja-looking source scalars, or a top-level key-order difference that would move `proxies` or any differing section across a common section.

- [ ] **Step 4: Run the migration against the ignored references**

Run:

```bash
.venv/bin/python scripts/migrate_reference_templates.py --reference-dir private/reference-configs/2026-08-21 --template-dir templates --home-output private/sources/owner/home.yaml
```

Expected: exit 0; output contains only file labels, counts, and YAML paths. Before staging, run the repository secret scan from Task 1 against every new tracked template. If the tool reports a mismatch, fix the transformation code and rerun it; never edit the ignored references.

- [ ] **Step 5: Implement strict rendering**

Define in `clash_sub/rendering.py`:

```python
@dataclass(frozen=True)
class VariantSpec:
    name: str
    top_level: Mapping[str, object]
    inject_node_groups: Tuple[str, ...]


def load_variant(template_dir: Path, variant: str) -> VariantSpec:
    document = yaml.safe_load(
        (template_dir / "variants" / f"{variant}.yaml").read_text(encoding="utf-8")
    )
    generator = document.pop("_generator")
    return VariantSpec(
        name=variant,
        top_level=document,
        inject_node_groups=tuple(generator["inject-node-groups"]),
    )


def dump_yaml_block(value: object, indent: int = 0) -> str:
    text = yaml.safe_dump(
        value,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()
    prefix = " " * indent
    return "\n".join(prefix + line if line else line for line in text.splitlines())
```

`render_variant()` deep-copies the variant, verifies each injection group exists exactly once, appends final proxy names without duplicates, and prepares a context containing only `PROXIES_ROOT_YAML` plus the exact marker names derived from the variant root keys. It renders through a Jinja2 `Environment(undefined=StrictUndefined, autoescape=False)` with no object globals or arbitrary-callable filters. Before rendering, require the template marker set and context key set to be identical; parse the rendered result once before returning it.

- [ ] **Step 6: Implement path-only structural comparison**

`scripts/compare_reference_configs.py` accepts the same reference and template directories. It renders each variant with a synthetic proxy, normalizes only:

```python
IGNORED_ROOT_KEYS = {"proxies", "proxy-providers"}


def safe_difference(path: Tuple[object, ...], kind: str) -> str:
    rendered_path = ".".join(str(item) for item in path)
    return f"{kind}: {rendered_path}"
```

Before comparison, remove reference inline proxy/provider names only from the variant's declared injection groups and remove the synthetic node from those rendered groups. Compare every remaining mapping key, list position, and scalar, but report only `missing`, `extra`, or `changed` plus its path. The command exits 0 only when all three normalized structures match.

- [ ] **Step 7: Run renderer tests and compare all references**

Run:

```bash
.venv/bin/python -m unittest tests.test_rendering -v
.venv/bin/python scripts/compare_reference_configs.py --reference-dir private/reference-configs/2026-08-21 --template-dir templates
```

Expected: tests PASS and comparison exits 0 without printing any private value.

- [ ] **Step 8: Remove obsolete templates and commit**

```bash
git add clash_sub/rendering.py scripts/migrate_reference_templates.py scripts/compare_reference_configs.py templates/clash.yaml.j2 templates/variants tests/test_rendering.py
git rm templates/_base.yaml.tmpl templates/parts/dns-balanced.part templates/parts/dns-privacy.part templates/parts/geoip-resolve.part templates/parts/geoip-no-resolve.part
git commit -m "feat: derive three Clash variants from one template"
```

### Task 5: Validate Complete Clash Documents and REALITY Nodes

**Files:**
- Create: `clash_sub/validation.py`
- Create: `tests/test_validation.py`

**Interfaces:**
- Consumes: rendered YAML text, exact private source URLs, `RealitySettings`, and the expected variant.
- Produces: `validate_config(text: str, source_urls: Sequence[str], reality: RealitySettings) -> Mapping[str, object]`, `sha256_bytes(data: bytes) -> str`, and `sha256_file(path: Path) -> str`.

- [ ] **Step 1: Write failing structural, leakage, and REALITY tests**

Create `tests/test_validation.py`:

```python
class ValidationTests(unittest.TestCase):
    def test_unknown_proxy_group_target_is_rejected(self):
        document = valid_document()
        document["proxy-groups"][0]["proxies"].append("missing-target")
        with self.assertRaisesRegex(ValidationError, "missing-target"):
            validate_config(dump(document), [], REALITY)

    def test_exact_upstream_url_leak_is_rejected_without_echoing_it(self):
        source_url = "http://127.0.0.1:2096/sub/private-value"
        document = valid_document()
        document["notes"] = source_url
        with self.assertRaisesRegex(ValidationError, "upstream source URL") as context:
            validate_config(dump(document), [source_url], REALITY)
        self.assertNotIn(source_url, str(context.exception))

    def test_self_hosted_reality_node_requires_expected_public_endpoint(self):
        document = valid_document()
        document["proxies"][0]["server"] = "127.0.0.1"
        with self.assertRaisesRegex(ValidationError, "public endpoint"):
            validate_config(dump(document), [], REALITY)
```

Also test malformed YAML, non-mapping root, missing required sections, `proxy-providers` presence, empty proxies, duplicate proxy names, duplicate group names, unresolved group references, unresolved rule targets, leftover Jinja markers, invalid `rule-providers`, wrong VLESS network/flow/port, missing TLS/SNI/fingerprint/public key/short ID, and a valid non-REALITY airport/home node.

- [ ] **Step 2: Run validation tests and observe missing-module failure**

Run: `.venv/bin/python -m unittest tests.test_validation -v`

Expected: FAIL because `clash_sub.validation` does not exist.

- [ ] **Step 3: Implement the structural and credential-leak gate**

Use these exact constants:

```python
BUILTIN_TARGETS = {
    "DIRECT",
    "REJECT",
    "REJECT-DROP",
    "PASS",
    "COMPATIBLE",
    "GLOBAL",
}
REQUIRED_TOP_LEVEL = {
    "dns",
    "proxies",
    "proxy-groups",
    "rule-providers",
    "rules",
}
```

`validate_config()` parses with `yaml.safe_load`, rejects `proxy-providers`, verifies unique names, recursively verifies group targets, and checks rule targets after the last comma against node names, group names, or built-ins where the rule syntax has a target. It may report YAML paths and display names but never full proxy mappings or source URLs.

For each self-hosted VLESS node identified by `type == "vless"` and `flow == reality.required_flow`, require the configured public address/port, `network: tcp`, `tls: true`, `servername`, `client-fingerprint`, and a mapping `reality-opts` with non-empty `public-key` and `short-id`. Reject a VLESS node with partial REALITY fields rather than treating it as an airport node.

- [ ] **Step 4: Run validation, rendering, and conversion tests**

Run: `.venv/bin/python -m unittest tests.test_validation tests.test_rendering tests.test_converter -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit the validation gate**

```bash
git add clash_sub/validation.py tests/test_validation.py
git commit -m "feat: validate complete Clash releases"
```

### Task 6: Build Candidates and Publish Atomic Five-Version Releases

**Files:**
- Modify: `clash_sub/models.py`
- Create: `clash_sub/releases.py`
- Create: `tests/test_releases.py`

**Interfaces:**
- Consumes: `Settings`, converter, traffic client, local loaders, renderer, and validator.
- Produces: `ReleaseBuilder.build_candidate(user_id: str, operation_id: str) -> Candidate`, `publish_candidate(candidate: Candidate, private_root: Path, keep: int = 5) -> Release`, `list_history(private_root: Path, user_id: str) -> Tuple[Release, ...]`, and `rollback(private_root: Path, user_id: str, release_id: str) -> Release`.

- [ ] **Step 1: Write failing isolation, atomicity, retention, and rollback tests**

Create `tests/test_releases.py`:

```python
class ReleaseTests(unittest.TestCase):
    def test_member_candidate_contains_only_its_own_xui_nodes(self):
        candidate = self.builder.build_candidate("friend", "op-friend")
        text = candidate.files["balanced"].read_text(encoding="utf-8")
        self.assertIn("friend-node", text)
        self.assertNotIn("owner-xui-node", text)
        self.assertNotIn("owner-airport-node", text)
        self.assertNotIn("owner-home-node", text)

    def test_owner_switches_all_three_variants_together(self):
        candidate = self.builder.build_candidate("owner", "op-owner")
        release = publish_candidate(candidate, self.private_root, keep=5)
        self.assertEqual(
            (self.private_root / "current" / "owner").resolve(),
            release.path.resolve(),
        )
        self.assertEqual(
            set(release.files),
            {"balanced", "balanced-win", "privacy"},
        )

    def test_failed_build_does_not_change_current(self):
        previous = self.publish_valid_owner_release()
        self.renderer.fail_variant = "privacy"
        with self.assertRaises(BuildError):
            self.builder.build_candidate("owner", "op-failing")
        self.assertEqual(
            (self.private_root / "current" / "owner").resolve(),
            previous.path.resolve(),
        )
```

Add tests for missing/empty xui sources, missing owner snapshots, no local source access for members, manifest sanitization, sidecar traffic data, source-name collision handling, six releases pruning to five, reference preservation, symlink traversal rejection, hash-verified rollback, and operation-log redaction.

- [ ] **Step 2: Run release tests and observe missing-module failure**

Run: `.venv/bin/python -m unittest tests.test_releases -v`

Expected: FAIL because `clash_sub.releases` does not exist.

- [ ] **Step 3: Add candidate and release models**

Add to `clash_sub/models.py`:

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

- [ ] **Step 4: Implement candidate construction**

`ReleaseBuilder.build_candidate()` creates `private/staging/<operation-id>/<user-id>/` with mode `0700` and performs, in order:

1. Convert only that user's xui URL and normalize its REALITY endpoint.
2. For owner only, load the airport and home snapshots.
3. Merge with labels `3x-ui`, `机场`, and `家庭`.
4. Fetch owner/member 3x-ui traffic once.
5. Render every allowed variant.
6. Call `validate_config()` with the user's exact private source URLs.
7. Write each YAML and `<variant>.meta.json` with `0600`.
8. Write `manifest.json` last.

Use this exact non-secret manifest shape:

```json
{
  "schema_version": 1,
  "operation_id": "20260821T120000Z-a1b2c3d4",
  "user_id": "owner",
  "created_at": "2026-08-21T12:00:00Z",
  "variants": ["balanced", "balanced-win", "privacy"],
  "input_hashes": {
    "template": "sha256",
    "xui": "sha256",
    "airport": "sha256",
    "home": "sha256"
  },
  "output_hashes": {
    "balanced": "sha256",
    "balanced-win": "sha256",
    "privacy": "sha256"
  },
  "source_counts": {
    "xui": 1,
    "airport": 2,
    "home": 2
  }
}
```

Omit absent owner sources from the corresponding mappings. Manifests and sidecars never contain URLs, token hashes, proxy mappings, names, UUIDs, or passwords.

- [ ] **Step 5: Implement atomic publication, retention, and rollback**

`publish_candidate()` validates the manifest hashes again, moves the complete candidate to `private/releases/<user-id>/<release-id>/`, creates a temporary relative symlink beside `private/current/<user-id>`, and calls `os.replace()` for the atomic switch. Only after a successful switch may it prune releases older than the newest five.

`rollback()` resolves the requested path under that user's release root, requires every manifest file and hash, and switches the same relative symlink without conversion or rendering. `list_history()` returns only valid successful releases sorted newest first.

- [ ] **Step 6: Run release and security tests**

Run: `.venv/bin/python -m unittest tests.test_releases tests.test_repository_safety tests.test_validation -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit release management**

```bash
git add clash_sub/models.py clash_sub/releases.py tests/test_releases.py
git commit -m "feat: publish atomic per-user Clash releases"
```

### Task 7: Add Machine-Facing Management and Secure Airport Import

**Files:**
- Create: `clash_sub/manager.py`
- Create: `tests/test_manager.py`

**Interfaces:**
- Consumes: settings, release APIs, stdin for a temporary airport URL, and the private JSON-lines operation log.
- Produces: JSON-only subcommands used by `clash_sub.host_cli` in Task 9.

- [ ] **Step 1: Write failing manager command tests**

Create `tests/test_manager.py`:

```python
class ManagerTests(unittest.TestCase):
    def test_airport_import_reads_url_only_from_stdin(self):
        secret = "https://airport.example/temp/private-value"
        result = run_manager(["import-airport"], stdin=secret + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("private-value", result.stdout)
        self.assertNotIn("private-value", result.stderr)
        self.assertNotIn("private-value", self.operation_log_text())

    def test_status_detects_changed_inputs_without_credentials(self):
        self.publish_owner_release()
        self.change_home_snapshot()
        result = run_manager(["status", "owner"])
        payload = json.loads(result.stdout)
        self.assertTrue(payload["users"]["owner"]["needs_refresh"])
        self.assertNotIn("password", result.stdout.lower())

    def test_rotate_token_persists_only_hash_and_returns_urls_once(self):
        result = run_manager(["rotate-token", "friend"])
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload["urls"]), {"balanced"})
        users_text = self.users_path.read_text(encoding="utf-8")
        self.assertNotIn(payload["token"], users_text)
        self.assertEqual(
            load_settings(self.service_path, self.users_path)
            .users["friend"]
            .token_sha256,
            hash_token(payload["token"]),
        )
```

Also test `list-users`, `build`, `publish`, `status` with all users, `history`, `rollback`, malformed/empty stdin, non-HTTPS airport URL, empty conversion, failed atomic airport replacement, and operation logs limited to timestamp, operation, user ID, release ID, status, and non-secret error code.

- [ ] **Step 2: Run manager tests and observe missing-module failure**

Run: `.venv/bin/python -m unittest tests.test_manager -v`

Expected: FAIL because `clash_sub.manager` does not exist.

- [ ] **Step 3: Implement the exact machine command surface**

Expose only:

```text
python -m clash_sub.manager list-users
python -m clash_sub.manager build --operation-id <id> --user <id>
python -m clash_sub.manager publish --operation-id <id> --user <id>
python -m clash_sub.manager status [<user-id>]
python -m clash_sub.manager history <user-id>
python -m clash_sub.manager rollback <user-id> <release-id>
python -m clash_sub.manager rotate-token <user-id>
python -m clash_sub.manager import-airport
python -m clash_sub.manager logs --limit 50
```

All success output is one JSON object. Errors use stable codes such as `settings_invalid`, `source_failed`, `validation_failed`, `release_missing`, and `not_authorized`; they identify a user only when safe and never include a source URL or credential.

- [ ] **Step 4: Implement transactional airport import**

`import-airport` reads exactly one stripped line from stdin, requires HTTPS with no embedded username/password, passes it directly to `SubconverterClient.convert()`, validates a non-empty proxy list, and atomically writes:

```yaml
proxies:
  - name: Synthetic shape shown only in tests
    type: ss
    server: 198.51.100.20
    port: 443
    cipher: aes-128-gcm
    password: synthetic-password
```

The real output remains ignored and `0600`. The temporary URL is never written to disk, environment variables, argv, JSON output, or logs. Failed import preserves the previous airport snapshot. The manager does not refresh owner itself; it returns `{"imported": true, "owner_refresh_required": true}` for the host CLI to orchestrate.

- [ ] **Step 5: Run manager, release, and settings tests**

Run: `.venv/bin/python -m unittest tests.test_manager tests.test_releases tests.test_settings -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit manager commands**

```bash
git add clash_sub/manager.py tests/test_manager.py
git commit -m "feat: add private Clash management commands"
```

## Milestone B — Read-only publication and host orchestration

### Task 8: Add the Tokenized Read-Only Publisher

**Files:**
- Modify: `clash_sub/models.py`
- Create: `clash_sub/publisher.py`
- Create: `tests/test_publisher.py`

**Interfaces:**
- Consumes: `private/config/*.yaml`, `private/current/<user-id>`, release manifests, traffic sidecars, and optional live 3x-ui traffic headers.
- Produces: a loopback-only HTTP service with `GET`/`HEAD /s/<token>/<variant>.yaml` and `GET /healthz`.

- [ ] **Step 1: Write failing authorization, publication, and traffic-cache tests**

Create `tests/test_publisher.py` with focused cases:

```python
class PublisherTests(unittest.TestCase):
    def test_valid_token_serves_only_current_allowed_variant(self):
        response = self.request(
            "GET",
            f"/s/{self.friend_token}/balanced.yaml",
            client_ip="127.0.0.1",
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, self.friend_balanced_bytes)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_unknown_token_and_forbidden_variant_are_indistinguishable(self):
        unknown = self.request("GET", "/s/unknown/balanced.yaml")
        forbidden = self.request(
            "GET",
            f"/s/{self.friend_token}/privacy.yaml",
        )
        self.assertEqual(unknown.status, 404)
        self.assertEqual(forbidden.status, 404)
        self.assertEqual(unknown.body, forbidden.body)
        self.assertEqual(unknown.headers, forbidden.headers)

    def test_live_traffic_failure_falls_back_without_blocking_download(self):
        self.traffic_client.fail_after_first_call = True
        first = self.request("GET", f"/s/{self.friend_token}/balanced.yaml")
        second = self.request("GET", f"/s/{self.friend_token}/balanced.yaml")
        self.assertEqual(second.status, 200)
        self.assertEqual(
            second.headers["Subscription-Userinfo"],
            first.headers["Subscription-Userinfo"],
        )
```

Also test:

- `HEAD` returns the same headers and an empty body.
- Only `GET` and `HEAD` are accepted; mutation methods return `405`.
- `/healthz` succeeds only for a loopback client and returns no user, path, release, or token data.
- Encoded slashes, path traversal, empty tokens, extra path components, query strings, and unknown extensions are rejected.
- Token comparison succeeds from the SHA-256 hash and never requires plaintext persistence.
- A missing current link, a link outside the user's release root, a bad manifest, a hash mismatch, a file over 5 MiB, and an incomplete release fail closed.
- Live traffic is cached for 600 seconds; when live fetch fails, the process-local last good value is used, then the current release sidecar, then the header is omitted. The YAML download still succeeds.
- `total=0` is served unchanged as unlimited metadata.
- Rate limiting is 30 requests per rolling minute with a burst of 10. Unknown requests are keyed only by client address; authorized requests add the configured token hash. Both stores are bounded LRU maps with at most 4096 entries, and rejection does not log either key.
- A settings-file mtime change reloads token hashes and allowlists atomically; invalid replacement settings preserve the last good in-memory settings.
- `X-Real-IP` is trusted only when the socket peer is loopback.
- Sanitized logs contain timestamp, method, status, hashed route class, byte count, and error code only.

- [ ] **Step 2: Run publisher tests and observe the missing-module failure**

Run: `.venv/bin/python -m unittest tests.test_publisher -v`

Expected: FAIL because `clash_sub.publisher` does not exist.

- [ ] **Step 3: Add request and response models**

Add to `clash_sub/models.py`:

```python
@dataclass(frozen=True)
class Request:
    method: str
    path: str
    client_ip: str
    peer_ip: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
```

Use private mutable cache records inside `publisher.py`; do not expose token strings in model representations.

- [ ] **Step 4: Implement authorization and verified current-file loading**

Implement:

```python
class PublicationService:
    def __init__(
        self,
        settings_loader: Callable[[], Settings],
        traffic_client: TrafficClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings_loader = settings_loader
        self._traffic_client = traffic_client
        self._clock = clock
        self._settings = settings_loader()

    def handle(self, request: Request) -> Response:
        method = request.method.upper()
        if request.path == "/healthz":
            return self._health_response(request)
        if method not in {"GET", "HEAD"}:
            return self._method_not_allowed()
        authorization = self._authorize_subscription_path(request.path)
        if authorization is None:
            return self._not_found()
        return self._serve_current(request, authorization)
```

`_authorize_subscription_path()` splits the raw path without URL-decoding, requires exactly `("", "s", token, "<variant>.yaml")`, and rejects `%`, backslashes, dot components, empty segments, and query/fragment characters. Hash the presented UTF-8 token and compare against every configured user hash with `hmac.compare_digest()`; only after a match check that the requested variant is in that user's allowlist. Unknown token, unknown user, forbidden variant, missing release, and bad hash all return the same constant 404 body and headers.

Resolve `private/current/<user-id>` and every selected release file, then require both to remain beneath `private/releases/<user-id>`. Parse `manifest.json`, require the variant to be listed, hash the YAML before serving, and refuse files over 5 MiB. Read the file once into bytes after verification so the response cannot mix two releases during a concurrent symlink switch.

- [ ] **Step 5: Implement safe traffic caching, rate limits, and HTTP headers**

Use an in-process lock around settings reload, token buckets, and traffic cache. Cache each user's successful live `SubscriptionUserinfo` for 600 seconds. When the cache expires, fetch once from the user's loopback 3x-ui URL; on failure use the last process-local good value, then parse the current release's `<variant>.meta.json` sidecar. Never make traffic metadata a prerequisite for serving valid YAML.

Successful YAML responses have exactly these required headers:

```text
Content-Type: text/yaml; charset=utf-8
Content-Disposition: attachment; filename="<variant>.yaml"
Cache-Control: no-store
Pragma: no-cache
X-Content-Type-Options: nosniff
Subscription-Userinfo: upload=...; download=...; total=...; expire=...
```

Omit `Subscription-Userinfo` only when no safe value exists. Do not emit `profile-update-interval` or any client refresh interval. `HEAD` computes the same authorization, validation, metadata, and `Content-Length` but returns an empty body.

- [ ] **Step 6: Add the loopback-only HTTP adapter**

Use `ThreadingHTTPServer` with a handler that translates requests into `Request` and writes `Response`. The startup function rejects any configured listen address other than `127.0.0.1`, sets a 15-second per-connection timeout, caps the request target at 2 KiB, and overrides `log_message()` so the standard library never writes tokenized paths. Ignore forwarded headers unless `peer_ip` is loopback.

- [ ] **Step 7: Run publisher, release, and traffic tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_publisher tests.test_releases tests.test_traffic -v
```

Expected: all tests PASS.

- [ ] **Step 8: Commit the read-only publisher**

```bash
git add clash_sub/models.py clash_sub/publisher.py tests/test_publisher.py
git commit -m "feat: serve tokenized Clash subscriptions"
```

### Task 9: Add the User-Facing `clash-sub` Command

**Files:**
- Create: `clash_sub/host_cli.py`
- Create: `bin/clash-sub`
- Create: `tests/test_host_cli.py`

**Interfaces:**
- Consumes: machine-facing manager JSON, Docker Compose commands, the Mihomo validator container, and `scripts/check_certificate.py` status JSON.
- Produces: exactly `help`, `status`, `refresh [user-id]`, `airport`, `history <user-id>`, `rollback <user-id> <release-id>`, `rotate-link <user-id>`, and `logs [--limit N]`.

- [ ] **Step 1: Write failing command-surface and orchestration tests**

Create `tests/test_host_cli.py` with a fake command runner:

```python
class HostCliTests(unittest.TestCase):
    def test_no_arguments_prints_complete_help(self):
        result = self.run_cli([])
        self.assertEqual(result.returncode, 0)
        for command in (
            "status",
            "refresh",
            "airport",
            "history",
            "rollback",
            "rotate-link",
            "logs",
        ):
            self.assertIn(command, result.stdout)
        self.assertNotIn("refresh-all", result.stdout)

    def test_refresh_publishes_only_after_every_variant_validates(self):
        runner = FakeRunner()
        result = run_cli(["refresh", "owner"], runner=runner)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            runner.manager_actions,
            ["build:owner", "publish:owner"],
        )
        self.assertEqual(
            runner.validated_variants,
            ["balanced", "balanced-win", "privacy"],
        )

    def test_failed_variant_validation_never_publishes(self):
        runner = FakeRunner(failing_variant="privacy")
        result = run_cli(["refresh", "owner"], runner=runner)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(runner.manager_actions, ["build:owner"])
```

Also test unknown commands, exact exit codes, refreshing one user, refreshing all users when no ID is supplied, per-user failure isolation during all-user refresh, airport hidden input, airport import followed by owner-only refresh, failed airport import preserving current state, sanitized status, history, hash-checked rollback, one-time token display, and log limit bounds.

- [ ] **Step 2: Run host CLI tests and observe the expected failure**

Run: `.venv/bin/python -m unittest tests.test_host_cli -v`

Expected: FAIL because `clash_sub.host_cli` and `bin/clash-sub` do not exist.

- [ ] **Step 3: Implement a small injectable command runner**

Define:

```python
class CommandRunner:
    def manager(
        self,
        arguments: Sequence[str],
        stdin_text: Optional[str] = None,
    ) -> Mapping[str, object]:
        completed = subprocess.run(
            ["docker", "compose", "run", "--rm", "-T", "manager", *arguments],
            input=stdin_text,
            text=True,
            capture_output=True,
            check=False,
        )
        return parse_manager_result(completed)

    def validate(self, candidate_path: Path) -> None:
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "run",
                "--rm",
                "validator",
                "-t",
                "-f",
                str(candidate_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        require_success_without_echoing_config(completed)
```

The production runner uses no shell, fixed command prefixes, bounded captured output, and stable redacted errors. Tests inject `FakeRunner` and do not require Docker.

- [ ] **Step 4: Implement the exact help and refresh lifecycle**

`refresh <user-id>`:

1. Ask the manager to build one operation-scoped candidate.
2. Validate every candidate variant with pinned Mihomo `-t -f`.
3. Ask the manager to publish only after all variants pass.
4. Print user ID, release ID, variants, and status only.

`refresh` without a user calls `list-users` and applies the same lifecycle independently in sorted user-ID order. It continues after one user's failure, exits non-zero when any user fails, and never publishes a failed user's candidate. The command remains `refresh`; do not add `refresh-all`.

- [ ] **Step 5: Implement mobile-safe airport import and remaining commands**

`airport` calls `getpass.getpass("Temporary airport subscription URL: ")`, passes the value only through stdin to `import-airport`, immediately drops the local reference, then calls the same owner refresh lifecycle. It never puts the URL in argv, environment variables, terminal echo, output, or logs.

`status` combines manager status with `scripts/check_certificate.py --status-only` and shows:

- service reachability booleans;
- each user's current release ID, available variants, last generation time, and `needs_refresh`;
- 3x-ui traffic metadata when available;
- certificate expiry/renewal state;
- no source URL, token/hash, panel path, node name, or credential.

`rotate-link` prints the new tokenized URLs exactly once and warns that previous links stop working after the publisher reload. `history`, `rollback`, and `logs` expose only sanitized manager fields.

- [ ] **Step 6: Install the thin entry point and run focused tests**

`bin/clash-sub` imports `main` from `clash_sub.host_cli` and exits with its integer return code. It contains no orchestration logic.

Run:

```bash
.venv/bin/python -m unittest tests.test_host_cli tests.test_manager tests.test_publisher -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit the host command**

```bash
git add clash_sub/host_cli.py bin/clash-sub tests/test_host_cli.py
git commit -m "feat: add clash-sub host command"
```

### Task 10: Replace the Legacy Compose Stack with Pinned Loopback Services

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Modify: `compose.yaml`
- Create: `config/subconverter/pref.ini`
- Modify: `.env.example`
- Create: `tests/test_compose.py`
- Create: `tests/fixtures/synthetic-users.yaml`
- Delete after replacement passes: `scripts/generate_configs.py`
- Delete after replacement passes: `tests/test_generate_configs.py`

**Interfaces:**
- Consumes: pinned application requirements, loopback 3x-ui URLs on the host, ignored private data, and staged candidate paths.
- Produces: `subconverter`, `publisher`, `manager`, and one-shot networkless `validator` services with no Docker-published ports.

- [ ] **Step 1: Write failing static Compose-security tests**

Create `tests/test_compose.py`:

```python
class ComposeSecurityTests(unittest.TestCase):
    def test_no_service_publishes_a_port_or_mounts_docker_socket(self):
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
        for name, service in compose["services"].items():
            self.assertNotIn("ports", service, name)
            for mount in service.get("volumes", []):
                self.assertNotIn("/var/run/docker.sock", str(mount), name)

    def test_host_network_http_services_bind_loopback(self):
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
        self.assertEqual(
            compose["services"]["subconverter"]["network_mode"],
            "host",
        )
        self.assertEqual(
            compose["services"]["publisher"]["network_mode"],
            "host",
        )
        self.assertEqual(
            compose["services"]["publisher"]["environment"]["PUBLISHER_LISTEN"],
            "127.0.0.1",
        )

    def test_images_are_version_pinned(self):
        compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertNotIn(":latest", compose_text)
        self.assertIn("metacubex/subconverter:0.9.2@", compose_text)
        self.assertIn("metacubex/mihomo:v1.19.30", compose_text)
```

Also assert `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges:true`, non-root application user, tmpfs for writable runtime files, converter logging disabled, publisher read-only mounts, validator `network_mode: none`, no sub-web image/service, no `restart: always` for one-shot services, and no secret values in `.env.example`.

- [ ] **Step 2: Run static tests and observe failures against the legacy stack**

Run: `python3 -m unittest tests.test_compose -v`

Expected: FAIL because the current Compose file uses `latest`, still contains `subweb`, and lacks the new service boundaries.

- [ ] **Step 3: Build the pinned non-root application image**

Create `Dockerfile`:

```dockerfile
FROM python:3.13.13-alpine3.22

RUN addgroup -g 10001 clash-sub && adduser -D -H -u 10001 -G clash-sub clash-sub
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY clash_sub ./clash_sub
COPY templates ./templates
USER 10001:10001
ENTRYPOINT ["python", "-m"]
```

`.dockerignore` includes Git metadata, `private`, `1`, `generated`, tests, docs, caches, local virtual environments, and environment files. Do not copy private data into an image layer.

- [ ] **Step 4: Define exact Compose security boundaries**

Use this image for the converter:

```text
ghcr.io/metacubex/subconverter:0.9.2@sha256:58c26f49010c0c069a5b20c85e7f1ac909da8ef704650b34f5001dd84cb9f7b9
```

Use `docker.io/metacubex/mihomo:v1.19.30` for the validator. Define:

- `subconverter`: `network_mode: host`, command/config binding `127.0.0.1:25500`, `read_only`, tmpfs, all capabilities dropped, no-new-privileges, `restart: unless-stopped`, and `logging.driver: none` because converter request targets contain source URLs.
- `publisher`: `network_mode: host`, `PUBLISHER_LISTEN=127.0.0.1`, port 25501, read-only application/template/settings/current/release mounts, no source/reference/staging write access, `restart: unless-stopped`, and a loopback health check.
- `manager`: `network_mode: host` so it can reach loopback 3x-ui and subconverter; no server listener, private-root read/write mount, no automatic restart, and operation commands only.
- `validator`: `network_mode: none`, read-only candidate mount, tmpfs, no restart, and all capabilities dropped.

Host networking is mandatory here: Docker bridge containers cannot use the host's `127.0.0.1` listeners. Security is retained by explicit loopback binding and the absence of public Docker port publication.

Keep validator networking disabled. Mihomo's official `-t` path parses the configuration and exits before provider initialization; the Task 10 container integration test is the release-specific proof that the pinned image can validate these templates without downloading remote rule providers.

In `config/subconverter/pref.ini` set the converter API listener to `127.0.0.1:25500`, enable API mode, keep its default source URL empty, disable file upload/write APIs if supported by 0.9.2, and disable update checks. Before committing, verify every option name from the pinned image's shipped example and fail the tests if any required option is unavailable.

- [ ] **Step 5: Remove the old generator only after replacement tests pass**

Delete `scripts/generate_configs.py` and `tests/test_generate_configs.py` after Tasks 2–10 cover their safe behavior. Retain any still-relevant synthetic test cases by moving them to the focused suites before deletion.

- [ ] **Step 6: Verify Compose expansion and container behavior**

Run:

```bash
docker compose config
docker compose build publisher manager
docker compose up -d subconverter publisher
docker compose ps
curl --fail --silent http://127.0.0.1:25500/version
curl --fail --silent http://127.0.0.1:25501/healthz
```

Then run an integration test using only `tests/fixtures/reality-subscription.txt`: call the pinned converter, parse the result, normalize it, render `balanced`, and run:

```bash
docker compose run --rm validator -t -f /staging/synthetic/balanced.yaml
```

Expected: Compose expansion has no `ports:` entries; only loopback listeners exist; health checks pass; the converter emits a VLESS node with REALITY fields preserved; pinned Mihomo accepts the full synthetic configuration.

- [ ] **Step 7: Run all local tests and commit the stack replacement**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q clash_sub scripts
git diff --check
```

Expected: all tests PASS, compilation exits 0, and the diff check is empty.

Commit:

```bash
git add Dockerfile .dockerignore compose.yaml .env.example config/subconverter/pref.ini tests/test_compose.py tests/fixtures/synthetic-users.yaml
git rm scripts/generate_configs.py tests/test_generate_configs.py
git commit -m "build: pin the private Clash service stack"
```

## Milestone C — Clean-server verification and deployment

### Task 11: Document Manual 3x-ui Initialization and Implement Read-Only Preflight

**Files:**
- Create: `docs/3x-ui-setup.md`
- Create: `scripts/check_reality_target.py`
- Create: `scripts/server_preflight.py`
- Create: `tests/fixtures/preflight-clean.json`
- Create: `tests/fixtures/preflight-legacy-trojan.json`
- Create: `tests/fixtures/xray-reality-config.json`
- Create: `tests/test_reality_target.py`
- Create: `tests/test_server_preflight.py`

**Interfaces:**
- Consumes: local process/listener/service state, selected Xray configuration, fixed expected versions, optional domain DNS, and TLS target observations.
- Produces: redacted JSON preflight and REALITY-target reports; neither script changes the host.

- [ ] **Step 1: Write failing REALITY-target parser tests**

Create `tests/test_reality_target.py` around saved synthetic `openssl s_client` output:

```python
class RealityTargetTests(unittest.TestCase):
    def test_accepts_tls13_h2_x25519_and_matching_san(self):
        observation = parse_s_client_output(VALID_OUTPUT)
        result = evaluate_target(observation, expected_server_name="www.example.com")
        self.assertTrue(result.ok)
        self.assertEqual(
            result.checks,
            {
                "reachable": True,
                "tls13": True,
                "alpn_h2": True,
                "x25519": True,
                "certificate_name": True,
            },
        )

    def test_report_never_contains_certificate_or_target_body(self):
        result = evaluate_target(
            parse_s_client_output(INVALID_OUTPUT),
            expected_server_name="www.example.com",
        )
        serialized = json.dumps(result.to_json())
        self.assertNotIn("BEGIN CERTIFICATE", serialized)
        self.assertNotIn("private-marker", serialized)
```

Also test timeout, refused connection, TLS 1.2, no ALPN, ALPN other than `h2`, no X25519, name mismatch, malformed output, and an IPv4/IPv6 connect address with a separate SNI.

- [ ] **Step 2: Write failing clean-host and Xray preflight tests**

Create `tests/test_server_preflight.py` with an injected read-only runner:

```python
class ServerPreflightTests(unittest.TestCase):
    def test_clean_expected_host_passes_with_redacted_summary(self):
        report = run_preflight(self.clean_runner(), self.settings)
        self.assertTrue(report.ok)
        serialized = json.dumps(report.to_json())
        self.assertNotIn("00000000-0000-4000-8000-000000000001", serialized)
        self.assertNotIn("private-key", serialized)
        self.assertNotIn("/secret-panel-path/", serialized)

    def test_legacy_trojan_or_unknown_443_owner_blocks_apply(self):
        report = run_preflight(self.legacy_trojan_runner(), self.settings)
        self.assertFalse(report.ok)
        self.assertIn("legacy_service_present", report.blocking_codes)

    def test_preflight_never_executes_mutating_commands(self):
        runner = self.clean_runner()
        run_preflight(runner, self.settings)
        self.assertFalse(runner.mutating_command_seen)
```

Also test Debian 12 amd64, root/non-root reporting, 3x-ui version mismatch, Xray version mismatch, TCP 443 not owned solely by Xray, UDP 443 open, panel/subscription not loopback, missing/non-REALITY inbound, wrong flow/network/port, empty short ID, no clients, mixed client flow, unexpected public listener, absent/present Nginx, stale Jrohy/Trojan services, Docker/Compose availability, SSH-port detection, UFW inactive/unsafe state, domain A/AAAA mismatch, and all reports excluding UUIDs, paths, keys, URLs, and node names.

- [ ] **Step 3: Run preflight tests and observe missing-script failures**

Run:

```bash
.venv/bin/python -m unittest tests.test_reality_target tests.test_server_preflight -v
```

Expected: FAIL because both scripts do not exist.

- [ ] **Step 4: Write the pinned manual 3x-ui initialization checklist**

In `docs/3x-ui-setup.md` document this order:

1. Reinstall a clean Debian 12 amd64 server and update the OS.
2. Download the official `3.6.0` installation script from the matching Git tag to a local file; inspect it before running it. Do not pipe the network response directly to a shell.
3. Install native 3x-ui `3.6.0` manually. Record the package/source URL and checksum in the administrator's private deployment log.
4. In the panel, set a strong unique username/password, enable 2FA, choose a random Web Base Path, and bind the panel to `127.0.0.1:<configured-panel-port>`.
5. Bind the raw subscription service to `127.0.0.1:<configured-subscription-port>` and use a random subscription path.
6. Select Xray-core `26.6.27` in 3x-ui and verify its binary version. Disable automatic core upgrades.
7. Create one public inbound: VLESS, RAW/TCP, REALITY, TCP 443, `xtls-rprx-vision`, a tested target/SNI, generated REALITY keypair, and at least one non-empty short ID.
8. Create one independent 3x-ui client per person. Do not share UUIDs or raw subscription IDs. Ordinary-user clients are referenced only by their matching user entry; the owner has a separate client.
9. Run `scripts/check_reality_target.py` before accepting the target, then run `scripts/server_preflight.py` before the project installer.

Include a redacted verification table for panel version, Xray version, listener addresses, inbound protocol/network/security/flow, number of clients, and target-test booleans. State that the REALITY private key stays only in Xray/3x-ui and must never enter this repository or a generated Clash file.

Use version-specific downloaded files rather than a remote shell pipeline:

```bash
curl --fail --show-error --location \
  --output /tmp/3x-ui-install-v3.6.0.sh \
  https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.6.0/install.sh
less /tmp/3x-ui-install-v3.6.0.sh
bash /tmp/3x-ui-install-v3.6.0.sh v3.6.0
```

The last command is intentionally a human step and is never called by `scripts/install_server.py`.

- [ ] **Step 5: Implement the read-only REALITY target check**

`scripts/check_reality_target.py` accepts `--connect-address`, `--port`, `--server-name`, and `--timeout`. It invokes:

```bash
openssl s_client -connect CONNECT_ADDRESS:PORT -servername SERVER_NAME -tls1_3 -alpn h2 -groups X25519 -brief
```

Use `subprocess.run()` without a shell, replace the uppercase metavariables with validated arguments, set stdin to `DEVNULL`, and cap stdout/stderr at 256 KiB. The parser returns booleans for reachability, TLS 1.3, negotiated `h2`, X25519, and certificate-name verification. The default human and JSON output include only those booleans, elapsed milliseconds, and stable error codes.

- [ ] **Step 6: Implement the read-only server preflight**

Use a `CommandRunner` that exposes only bounded, timeout-limited read commands. Collect:

- `/etc/os-release` and `uname -m`;
- `systemctl is-active`/`show` for 3x-ui, Xray, Nginx, Docker, Trojan, Trojan-web, MariaDB, and Portainer;
- `ss -H -lntup`;
- 3x-ui and Xray version output;
- selected Xray JSON read directly from the configured local path;
- `docker compose config --format json` from the repository;
- `ufw status numbered`;
- DNS A/AAAA results and the server's public addresses in domain mode;
- the current SSH connection port from `SSH_CONNECTION`, reported only as a number.

Parse the Xray JSON entirely in memory. Report only:

- whether exactly one public TCP 443 listener is Xray;
- whether UDP 443 is closed;
- panel/subscription listeners are loopback;
- count of VLESS+TCP+REALITY inbounds on 443;
- count of clients and whether every client uses `xtls-rprx-vision`;
- whether server names and short IDs are non-empty;
- whether legacy/unknown services or listeners exist.

Never serialize Xray client IDs, keys, server names, raw paths, source URLs, process command lines, or complete configuration fragments. Return exit 0 only when every blocking prerequisite passes. Missing Nginx is acceptable before installation; existing Nginx is accepted only when its loaded configuration contains no 443 listener and no unmanaged 80/8443 conflict.

- [ ] **Step 7: Run focused tests and manually inspect a synthetic report**

Run:

```bash
.venv/bin/python -m unittest tests.test_reality_target tests.test_server_preflight -v
.venv/bin/python scripts/server_preflight.py --fixture tests/fixtures/preflight-clean.json --json
```

Expected: tests PASS; the fixture report contains only booleans, versions, counts, ports, and stable codes.

- [ ] **Step 8: Commit manual setup and read-only checks**

```bash
git add docs/3x-ui-setup.md scripts/check_reality_target.py scripts/server_preflight.py tests/fixtures/preflight-clean.json tests/fixtures/preflight-legacy-trojan.json tests/fixtures/xray-reality-config.json tests/test_reality_target.py tests/test_server_preflight.py
git commit -m "feat: verify clean 3x-ui REALITY hosts"
```

### Task 12: Add Nginx, Certificate, Firewall, and Compose Installation

**Files:**
- Modify: `clash_sub/models.py`
- Modify: `clash_sub/settings.py`
- Modify: `config/service.example.yaml`
- Create: `deploy/nginx/00-acme-http.conf.tmpl`
- Create: `deploy/nginx/10-clash-domain.conf.tmpl`
- Create: `deploy/nginx/10-clash-ip.conf.tmpl`
- Create: `deploy/systemd/clash-sub-cert-renew.service`
- Create: `deploy/systemd/clash-sub-cert-renew.timer`
- Create: `deploy/systemd/clash-sub-cert-check.service`
- Create: `deploy/systemd/clash-sub-cert-check.timer`
- Create: `scripts/check_certificate.py`
- Create: `scripts/install_server.py`
- Create: `scripts/install-server.sh`
- Create: `tests/test_certificate.py`
- Create: `tests/test_install_server.py`
- Create: `tests/test_nginx_templates.py`

**Interfaces:**
- Consumes: approved private `service.yaml`, a clean preflight report, current SSH port, domain DNS or public IP, and an optional alert command.
- Produces: reversible project-owned host files, Nginx TCP 80/8443, trusted certificates, certificate timers, the pinned Compose stack, UFW policy, and `/usr/local/bin/clash-sub`.

- [ ] **Step 1: Extend certificate settings with tested issuance inputs**

Add `acme_email: str` to `CertificateSettings` and `acme-email: admin@example.com` to the certificate section in `config/service.example.yaml`. In IP mode require:

- the publication and panel authorities use the same literal public IP plus port 8443;
- the fullchain path names that IP certificate;
- `alert-command` is a non-empty argv list;
- `alert-before-seconds` is at least 172800.

Add settings tests for invalid email shape, shell-string alert commands, relative certificate paths, domain authorities in IP mode, IP authorities in domain mode, and missing IP-mode alerting.

- [ ] **Step 2: Write failing certificate and Nginx tests**

Create `tests/test_certificate.py`:

```python
class CertificateTests(unittest.TestCase):
    def test_valid_certificate_reports_seconds_without_subject_names(self):
        report = inspect_certificate(VALID_CERT_PATH, now=FIXED_NOW)
        self.assertTrue(report.valid)
        self.assertGreater(report.remaining_seconds, 0)
        self.assertNotIn("example.com", json.dumps(report.to_json()))

    def test_expiring_or_failed_renewal_runs_alert_argv_without_shell(self):
        runner = FakeRunner()
        status = check_and_alert(
            EXPIRING_CERT_PATH,
            ("notify-command", "--channel", "private"),
            runner=runner,
            now=FIXED_NOW,
        )
        self.assertTrue(status.alerted)
        self.assertEqual(runner.commands[0][0], "notify-command")
        self.assertFalse(runner.shell_used)
```

Create `tests/test_nginx_templates.py` to render both templates and assert:

- only TCP 80 and 8443 are declared;
- there is a generic `default_server` on 8443;
- domain mode has separate exact panel/subscription `server_name` values and one shared certificate;
- IP mode has only path routing under the literal IP certificate;
- `/s/` proxies only to `127.0.0.1:25501` and has `access_log off`;
- the panel base path proxies only to the configured loopback panel port with WebSocket headers;
- every unmatched Host/path returns the same generic response;
- request body and response-size limits, TLS 1.2/1.3, security headers, and request-rate limits are present;
- no `stream` block, TCP 443 listener, port 1443, product banner, directory alias, converter route, raw subscription route, or real example domain appears.

- [ ] **Step 3: Write failing installer dry-run, ordering, and rollback tests**

Create `tests/test_install_server.py` with a temporary filesystem root and fake command runner:

```python
class InstallerTests(unittest.TestCase):
    def test_default_mode_is_read_only_and_writes_nothing(self):
        root = self.empty_root()
        runner = FakeRunner()
        result = run_installer(self.arguments(), root=root, runner=runner)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(list(root.rglob("*")), [])
        self.assertFalse(runner.mutating_command_seen)

    def test_apply_opens_ssh_before_enabling_default_deny(self):
        runner = FakeRunner()
        result = run_installer(
            self.arguments("--apply"),
            root=self.empty_root(),
            runner=runner,
        )
        self.assertEqual(result.returncode, 0)
        self.assertLess(
            runner.index(("ufw", "allow", "26019/tcp")),
            runner.index(("ufw", "default", "deny", "incoming")),
        )

    def test_failed_nginx_validation_restores_files_and_never_reloads(self):
        root = self.installed_root()
        before = snapshot(root)
        runner = FakeRunner(fail_on=("nginx", "-t"))
        result = run_installer(
            self.arguments("--apply"),
            root=root,
            runner=runner,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(snapshot(root), before)
        self.assertNotIn(("systemctl", "reload", "nginx"), runner.commands)
```

Also test failed preflight makes no changes, unknown active UFW rules block apply, SSH argument mismatch blocks apply, package failure, certificate failure, Compose failure, idempotent second apply, mode-specific Certbot argv, atomic file replacement, `0600` secrets/`0644` public host configs, backup inventory, rollback of every touched project-owned file, no deletion of unrelated Nginx files, and no command logs containing domains, panel paths, tokens, or certificate email.

- [ ] **Step 4: Implement certificate inspection and alerting**

`scripts/check_certificate.py`:

- invokes `openssl x509 -checkend` and `-enddate` without a shell;
- writes `private/state/certificate.json` atomically with mode `0600`;
- stores only checked time, remaining seconds, valid/renewal booleans, last success time, last stable error code, and last alert time;
- invokes the configured alert argv directly when expiry is inside the threshold, renewal state is failed, or the certificate cannot be read;
- suppresses duplicate identical alerts for 12 hours;
- supports `--status-only`, which reads the sanitized state and performs no mutation.

The command never emits certificate SANs, authorities, emails, filesystem paths, or alert-command arguments.

- [ ] **Step 5: Implement strict Nginx templates**

`00-acme-http.conf.tmpl` defines one TCP 80 default server:

```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location ^~ /.well-known/acme-challenge/ {
        root /var/lib/clash-sub/acme;
        try_files $uri =404;
    }

    location / {
        return 404;
    }
}
```

The 8443 templates define a generic TLS default server plus only the approved routes. Use a shared `limit_req_zone` keyed by binary remote address; cap request bodies at 1 KiB; set bounded proxy connect/read/send timeouts; hide upstream `Server` headers; disable access logs in the tokenized subscription location; and add `X-Content-Type-Options`, `Referrer-Policy`, and a restrictive `Content-Security-Policy` on generic responses.

Domain mode uses one SAN certificate containing both exact authorities. IP mode uses the same route logic under one literal-IP server and certificate. The templates never listen on 443, proxy to the raw 3x-ui subscription listener, or expose subconverter.

- [ ] **Step 6: Implement a deterministic dry-run/apply installer**

`scripts/install_server.py` accepts exactly:

```text
--config /opt/clash-sub/private/config/service.yaml
--ssh-port <1-65535>
--apply
```

`--config` defaults to the path shown. Without `--apply` it:

1. loads strict settings;
2. runs the full read-only preflight;
3. renders all intended host files in memory;
4. prints a redacted action list and exits without writes or mutating commands.

With `--apply`, require the repository's resolved path to be `/opt/clash-sub`, require the supplied SSH port to match the active SSH connection and sshd listener, and execute:

1. Re-run preflight and stop on any blocking code.
2. Back up only target files it will replace to `/var/backups/clash-sub/<UTC-operation-id>/` with a JSON inventory and modes.
3. Install missing Debian packages: `docker.io`, `docker-compose-v2`, `nginx`, `ufw`, `python3-venv`, `curl`, and `ca-certificates`.
4. Create a dedicated `/opt/certbot` virtual environment and install `certbot==5.7.0`.
5. Install only the ACME TCP 80 Nginx file, run `nginx -t`, then start/reload Nginx.
6. Issue the certificate.
7. Atomically install the final 8443 Nginx file and systemd certificate units, run `nginx -t`, and only then reload.
8. Run `docker compose config`, build the application image, start only `subconverter` and `publisher`, and verify loopback health checks.
9. Install `/usr/local/bin/clash-sub` as a root-owned symlink to `/opt/clash-sub/bin/clash-sub`.
10. Configure UFW last: allow the verified SSH TCP port first, then TCP 80, 443, and 8443; set incoming deny/outgoing allow; do not allow UDP 443. On inactive clean UFW, reset before adding these rules. On active UFW, continue only when its existing rules exactly match this approved set.
11. Enable UFW, re-run listener/preflight checks, and print the `clash-sub` help plus a redacted verification summary.

Every external command uses an argv list, a timeout, bounded output, and sanitized failure messages. If a step after backup fails, restore all project-owned files, run `nginx -t` before any rollback reload, and leave existing user data, 3x-ui, Xray, DNS, and unrelated Nginx files untouched. Package installation is reported as non-reversible; the rollback restores configuration and service state, not package databases.

- [ ] **Step 7: Implement domain and IP certificate issuance**

Domain mode runs:

```bash
/opt/certbot/bin/certbot certonly --webroot --webroot-path /var/lib/clash-sub/acme --non-interactive --agree-tos --email ACME_EMAIL --cert-name clash-sub-domain -d PANEL_DOMAIN -d SUBSCRIPTION_DOMAIN
```

IP mode runs:

```bash
/opt/certbot/bin/certbot certonly --preferred-profile shortlived --webroot --webroot-path /var/lib/clash-sub/acme --ip-address PUBLIC_IP --non-interactive --agree-tos --email ACME_EMAIL --cert-name clash-sub-ip
```

Replace uppercase metavariables with validated settings-derived argv values; never use shell interpolation. Install a Certbot deploy hook that first runs `nginx -t` and reloads Nginx only on success.

`clash-sub-cert-renew.timer` runs every six hours with randomized delay and invokes `/opt/certbot/bin/certbot renew --quiet`. `clash-sub-cert-check.timer` runs daily and invokes the certificate checker only; neither unit invokes `clash-sub refresh` or a manager command. In IP mode, apply must fail unless the alert command is configured, the initial certificate is valid for the requested IP, the renewal timer is active, and a renewal dry-run succeeds.

- [ ] **Step 8: Install the one-command wrapper and run local tests**

`scripts/install-server.sh` only validates that Python 3 is available and executes `scripts/install_server.py` with the original argv. It does not use `curl | sh`, install 3x-ui, hide the apply flag, or contain a second implementation.

Run:

```bash
.venv/bin/python -m unittest tests.test_certificate tests.test_nginx_templates tests.test_install_server tests.test_server_preflight -v
.venv/bin/python -m compileall -q scripts clash_sub
git diff --check
```

Expected: all tests PASS, compilation exits 0, and the diff check is empty.

- [ ] **Step 9: Commit the deployment engine**

```bash
git add clash_sub/models.py clash_sub/settings.py config/service.example.yaml deploy/nginx deploy/systemd scripts/check_certificate.py scripts/install_server.py scripts/install-server.sh tests/test_certificate.py tests/test_install_server.py tests/test_nginx_templates.py
git commit -m "feat: deploy the private Clash publication service"
```

### Task 13: Add End-to-End Security Tests, Operations Documentation, and Final Acceptance

**Files:**
- Create: `scripts/scan_tracked_secrets.py`
- Create: `tests/test_end_to_end.py`
- Create: `tests/test_secret_scan.py`
- Create: `docs/operations.md`
- Modify: `README.md`
- Modify: `DEPLOYMENT.md`
- Delete after replacement review: `docker/subconverter/pref.ini`
- Delete after replacement review: `docs/dns-design.md`
- Delete after replacement review: `docs/superpowers/plans/2026-08-19-clash-subscription-service.md`
- Delete after replacement review: `docs/superpowers/specs/2026-08-19-clash-subscription-service-design.md`

**Interfaces:**
- Consumes: the complete local stack, synthetic owner/member inputs, ignored real private inputs for a non-printing scan, and later an explicitly approved clean VPS.
- Produces: repeatable end-to-end evidence, operator/mobile instructions, recovery procedures, and a final redacted deployment report.

- [ ] **Step 1: Write failing end-to-end isolation and recovery tests**

Create `tests/test_end_to_end.py`:

```python
class EndToEndTests(unittest.TestCase):
    def test_member_and_owner_generate_only_authorized_sources(self):
        self.cli.refresh("friend")
        self.cli.refresh("owner")

        friend = self.publisher.get(self.friend_token, "balanced")
        owner = self.publisher.get(self.owner_token, "balanced")

        self.assertIn(b"friend-xui", friend.body)
        self.assertNotIn(b"owner-xui", friend.body)
        self.assertNotIn(b"owner-airport", friend.body)
        self.assertNotIn(b"owner-home", friend.body)
        self.assertIn(b"owner-xui", owner.body)
        self.assertIn(b"owner-airport", owner.body)
        self.assertIn(b"owner-home", owner.body)

    def test_three_variant_failure_preserves_previous_owner_release(self):
        previous = self.cli.refresh("owner").release_id
        self.validator.fail_variant = "privacy"
        failed = self.cli.refresh("owner")
        self.assertFalse(failed.ok)
        self.assertEqual(self.manager.current_release("owner"), previous)

    def test_airport_import_refreshes_owner_without_changing_friend(self):
        friend_before = self.cli.refresh("friend").release_id
        owner_before = self.cli.refresh("owner").release_id
        result = self.cli.airport("https://airport.example/temporary")
        self.assertTrue(result.ok)
        self.assertEqual(self.manager.current_release("friend"), friend_before)
        self.assertNotEqual(self.manager.current_release("owner"), owner_before)
```

Also cover all three variants, member token isolation, identical 404s, traffic-header cache fallback, token rotation invalidating the old link, five-release retention, hash-verified rollback, malformed airport data preserving the old snapshot, no generation on download, no scheduled generation unit, and certificate timer independence.

- [ ] **Step 2: Write a failing tracked-secret scan test**

`scripts/scan_tracked_secrets.py`:

1. gets tracked paths from `git ls-files -z`;
2. rejects tracked generated YAML, runtime manifests, private directories, environment files, private-key extensions, and the legacy `1/` path;
3. optionally scans ignored `private/config` and `private/sources` in memory, extracts only credential-like scalar values of at least 16 characters, and checks whether their exact UTF-8 bytes occur in any tracked file;
4. scans tracked text for concrete `vless://`, `vmess://`, `trojan://`, `ss://`, bearer-token subscription paths, PEM private keys, non-example UUIDs, and URL userinfo;
5. reports only a category and tracked path, never the matched value.

Tests use synthetic private values and prove that a leak fails, a safe example passes, binary files are skipped safely, and output does not echo the secret.

- [ ] **Step 3: Run end-to-end and secret-scan tests and observe failures**

Run:

```bash
.venv/bin/python -m unittest tests.test_end_to_end tests.test_secret_scan -v
```

Expected: FAIL because the end-to-end harness and scanner do not exist.

- [ ] **Step 4: Implement the minimal end-to-end harness and scanner**

Compose existing public interfaces rather than adding an alternate implementation. The harness uses temporary directories, synthetic sources, a fake loopback converter/traffic client, the real renderer/validator/release/publisher code, and an injectable fake Mihomo command result. A separate container integration from Task 10 remains the proof against the actual pinned converter and Mihomo.

The scanner exits 0 only when every tracked path and optional exact-value comparison passes. It never follows ignored symlinks, never scans release output as a tracked candidate, and caps each tracked file at 10 MiB.

- [ ] **Step 5: Rewrite operator documentation around the final command surface**

`README.md` contains:

- the owner/member trust model;
- the four fixed public ports/listener roles;
- a concise diagram from 3x-ui sources through conversion, validation, atomic release, publisher, and Nginx;
- explicit “no sub-web, no public converter, no scheduled generation” statements;
- links to setup, deployment, private-data, and operations documents;
- a warning that anyone possessing a subscription link can download its expanded credentials.

`DEPLOYMENT.md` contains:

1. clean Debian 12 prerequisite;
2. pinned manual 3x-ui/Xray checklist;
3. DNS A/AAAA setup for `panel` and `sub` and one SAN certificate;
4. `/opt/clash-sub` checkout and private-config creation with modes;
5. default read-only preflight command;
6. a visibly separate `--apply` command requiring administrator confirmation;
7. post-install listener, firewall, Nginx, certificate, Compose, panel, subscription, and Mihomo checks;
8. domain-to-IP-certificate migration and its mandatory alert/renewal conditions;
9. rollback of project-owned host files and the reminder that OS reinstall/DNS changes are outside the script.

`docs/operations.md` documents exact examples for every `clash-sub` command, including:

- mobile SSH `clash-sub airport` hidden input and automatic owner refresh;
- the airport website stays logged in only through the phone browser's secure HTTPS Cookie; that Cookie is never exported to the VPS;
- when the airport requires link generation and download from the same public egress, add a Quantumult X rule that routes only the airport portal/API domains through the owner's 3x-ui REALITY node, so the phone can keep QX enabled without global switching, Tailscale, or WireGuard;
- manual client refresh responsibility;
- no `refresh-all` alias;
- user-link rotation and per-user 3x-ui credential revocation after a leak;
- history/rollback and five-release retention;
- `Limit IP` as observed public-IP count rather than reliable device count, with a suggested starting value of 2 for other users;
- certificate alerts, service restart, log review, and safe backup/restore;
- recovery when the domain expires, the VPS IP changes, or the VPS IP is blocked;
- a warning that REALITY reduces some obvious protocol exposure but cannot guarantee avoidance of active probing or blocking.

- [ ] **Step 6: Remove superseded files after link and content review**

Delete the old subconverter config, 2026-08-19 design/plan, and stale DNS-only document only after:

- every still-relevant historical explanation is preserved in the approved 2026-08-21 spec or current operations/deployment docs;
- `rg` finds no tracked link to the deleted paths;
- the new `config/subconverter/pref.ini` is the only converter configuration;
- no old Jrohy/Trojan deployment command remains outside the historical findings section of the approved spec.

- [ ] **Step 7: Run the complete local acceptance suite**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q clash_sub scripts
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
.venv/bin/python scripts/compare_reference_configs.py --reference-dir private/reference-configs/2026-08-21 --template-dir templates
docker compose config
docker compose build publisher manager
docker compose run --rm validator -t -f /staging/synthetic/balanced.yaml
git diff --check
git status --short
```

Expected:

- all tests PASS;
- Python compilation exits 0;
- the secret scanner and all three structural comparisons exit 0 without private values;
- Compose expands and builds with no public port mappings;
- pinned Mihomo validates the synthetic complete configuration;
- the diff check is empty;
- `private/` and the migrated references do not appear in normal Git status.

- [ ] **Step 8: Request an implementation review and resolve findings**

Invoke `superpowers:requesting-code-review` over the complete implementation diff. Review specifically for:

- owner-to-member leakage;
- token/path traversal and timing behavior;
- failed-build atomicity;
- container host-network loopback binding;
- Nginx wrong-Host/wrong-path behavior;
- installer rollback and SSH/firewall ordering;
- certificate-renewal failure visibility;
- version drift and unpinned images.

Apply accepted findings with focused regression tests and rerun Step 7.

- [ ] **Step 9: Commit final tests and documentation**

```bash
git add README.md DEPLOYMENT.md docs/operations.md scripts/scan_tracked_secrets.py tests/test_end_to_end.py tests/test_secret_scan.py
git rm docker/subconverter/pref.ini docs/dns-design.md docs/superpowers/plans/2026-08-19-clash-subscription-service.md docs/superpowers/specs/2026-08-19-clash-subscription-service-design.md
git commit -m "docs: finish private Clash operations and acceptance"
```

- [ ] **Step 10: Stop before live-server changes and obtain separate approval**

Local implementation completion does not authorize a VPS change. Present:

- local test/build evidence;
- the exact redacted preflight command;
- the exact intended `--apply` command;
- expected package, Nginx, systemd, UFW, and Compose changes;
- the backup/rollback location;
- confirmation that 3x-ui, Xray data, DNS, OS disks, and unrelated files are outside the installer.

Wait for explicit user confirmation before connecting to a reinstalled server or running either live command.

- [ ] **Step 11: After approval, run a live read-only preflight checkpoint**

On the specifically approved clean VPS, run only the documented preflight and inspect:

```bash
sudo /opt/clash-sub/scripts/install-server.sh --config /opt/clash-sub/private/config/service.yaml --ssh-port 26019
```

Replace `26019` only with the administrator-confirmed active SSH port. Verify the redacted report shows:

- 3x-ui `3.6.0` and Xray `26.6.27`;
- Xray alone on public TCP 443 and no UDP 443;
- panel, raw subscription, subconverter, and publisher expected on loopback only;
- TCP 80/8443 free before installation or owned only by this project on a repeat run;
- no Trojan/Jrohy/legacy database/Portainer or unknown listener;
- domain DNS or IP-certificate prerequisites satisfied.

If any blocking item fails, stop and return the exact stable code plus a manual corrective checklist. Do not auto-clean the server.

- [ ] **Step 12: After a second approval, apply and verify externally**

Only after the user approves the preflight result, run the same command with `--apply`. Then verify:

```bash
ss -H -lntup
ufw status numbered
nginx -t
docker compose ps
clash-sub status
```

From an external client verify:

- VPS IP TCP 443 reaches the VLESS+REALITY client and does not serve the panel;
- `https://panel.<domain>:8443/<private-base-path>/` reaches 3x-ui only with normal authentication and 2FA;
- wrong 8443 Host/path returns the generic response;
- each token downloads only its allowed current YAML;
- each response has the correct user's `Subscription-Userinfo`;
- the three owner configs pass the client's Mihomo parser;
- an ordinary user's configuration contains only that user's 3x-ui node;
- no download triggers a new release.

Run one controlled failed refresh and one rollback to prove that `current` remains recoverable. Do not paste tokenized URLs, UUIDs, or configuration bodies into task output.

- [ ] **Step 13: Finish the branch only after fresh verification**

Invoke `superpowers:verification-before-completion` and rerun every command relevant to the final claim. Then invoke `superpowers:finishing-a-development-branch` to offer merge/PR/cleanup choices. Renaming the remote repository to `my-clash-config` and renaming the local workspace directory occur only after explicit user approval and after no active task/worktree depends on the old path.

---

## Execution Checkpoints

1. **Local implementation checkpoint:** Tasks 1–13 Steps 1–9 may modify only this repository and ignored `private/` data. Each task ends with a focused commit.
2. **Reference migration checkpoint:** The three ignored references may be parsed only by the non-printing migration/comparison tools; any unexpected mismatch stops implementation for user review.
3. **Live preflight checkpoint:** No live VPS access occurs until the user approves the exact target and command after local implementation evidence.
4. **Live apply checkpoint:** A successful read-only preflight is reported, then the user separately approves `--apply`.
5. **External acceptance checkpoint:** DNS/certificate/public tests use no credential-bearing output; failures stop before unrelated remediation.

## Pinned-Version Rationale

- 3x-ui `3.6.0` is the selected stable panel release and is installed manually from its matching official tag.
- Xray-core is fixed at `26.6.27` because the selected 3x-ui release supports it and current Mihomo documentation warns about REALITY compatibility with Xray-core `26.7.11` and later.
- MetaCubeX/subconverter `0.9.2` is fixed by tag and package digest; its VLESS+REALITY output must still pass the synthetic integration gate.
- Mihomo `1.19.30` is the fixed parser/runtime compatibility gate for every complete generated configuration.
- Certbot `5.7.0` is fixed because the IP-certificate design depends on current webroot and short-lived certificate support.
- Python, Jinja2, and PyYAML versions are fixed in the image and requirements; upgrades require the full local acceptance suite.

Primary references:

- [3x-ui releases](https://github.com/MHSanaei/3x-ui/releases)
- [3x-ui panel configuration](https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/config/panel.mdx)
- [3x-ui subscription configuration](https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/config/subscription.mdx)
- [Mihomo TLS and REALITY compatibility notes](https://github.com/MetaCubeX/Meta-Docs/blob/main/docs/config/proxies/tls.md)
- [Mihomo releases](https://github.com/MetaCubeX/mihomo/releases)
- [Mihomo v1.19.30 `-t` parse-only command path](https://raw.githubusercontent.com/MetaCubeX/mihomo/v1.19.30/main.go)
- [MetaCubeX/subconverter container package](https://github.com/MetaCubeX/subconverter/pkgs/container/subconverter)
- [Certbot releases](https://github.com/certbot/certbot/releases)
- [Let's Encrypt IP certificate guidance](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability.html)
- [Let's Encrypt Certbot short-lived certificate guidance](https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html)
