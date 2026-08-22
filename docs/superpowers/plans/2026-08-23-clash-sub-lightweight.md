# Clash Subscription Service Lightweight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Docker/subconverter/publisher stack with an on-demand Python command that discovers 3x-ui clients read-only, generates isolated static Clash configurations, and lets host Nginx serve them directly.

**Architecture:** New focused modules are built beside the legacy implementation, proven with synthetic SQLite and end-to-end fixtures, and switched into `bin/clash-sub` only after the new path passes. 3x-ui SQLite is opened read-only for identity and traffic, while its loopback Clash endpoint remains the canonical node source. Immutable release files and generated exact-match Nginx locations make configuration, token, traffic-header, rollback, and revocation changes fail-safe without a resident Python service.

**Tech Stack:** Python 3.11 standard library (`sqlite3`, `urllib`, `secrets`, `subprocess`), Jinja2 3.1.6, PyYAML 6.0.3, `unittest`, host Nginx, systemd, acme.sh, pinned Mihomo 1.19.30, 3x-ui 3.6.0, Xray-core 26.6.27, Debian 12 amd64.

**Spec:** `docs/superpowers/specs/2026-08-23-clash-sub-lightweight-redesign.md`

## Global Constraints

- The installed command is only `clash-sub`; do not provide `refresh`, `refresh-all`, `clashctl`, or compatibility aliases.
- The public variants are exactly `balanced`, `standard`, and `privacy`; remove `balanced-win` without an alias.
- Public filenames are exactly `clash-balanced.yaml`, `clash-standard.yaml`, and `clash-privacy.yaml`; response titles are `Clash Balanced`, `Clash Standard`, and `Clash Privacy`.
- Public URLs and response headers never contain the 3x-ui email. The root-only links view groups every full URL by internal email and displays all users at once.
- Every bearer token is `<43-character unpadded Base64URL encoding of 32 random bytes>-<six-character readable code>`. The readable alphabet excludes `I`, `L`, `O`, `0`, and `1`; the code is unique among retained user mappings and is never accepted alone.
- Do not implement a short-link or redirect endpoint. A full token is required for every request.
- Use 3x-ui's SQLite database only through a read-only URI plus `PRAGMA query_only=ON`; no SQL statement may mutate it. Treat any missing table, missing column, duplicate identity, invalid setting, or owner mismatch as a global fail-closed error.
- Construct source URLs only as `http://127.0.0.1:<subPort>/<subClashPath>/<subId>` from validated database values. No public 3x-ui subscription endpoint is exposed.
- Ordinary users receive only their own 3x-ui proxies in `standard`. Owner receives: `balanced = owner 3x-ui + airport + home`, `standard = owner 3x-ui + airport`, and `privacy = owner 3x-ui + airport + home`.
- Airport input is HTTPS, entered with hidden input, bounded to 5 MiB and three HTTPS redirects, and never placed in argv, environment, logs, state, manifests, exceptions, or generated files. Commit a new airport snapshot only with a successful owner three-variant activation.
- Home and airport snapshots, state, operation data, manifests, reference originals, and tokens are root-only and Git-ignored. Generated immutable YAML is readable only by the Nginx group and reachable only through exact generated locations.
- Keep five successful releases per 3x-ui client. Owner's three variants activate atomically. One ordinary-user failure leaves that user's old release active and does not stop other ordinary users.
- Structural YAML validation and pinned Mihomo validation both run before activation. Unchanged output hashes do not create a release or invoke Mihomo.
- Nginx serves immutable files directly. There is no resident publisher, converter, Python process, or request-time generation/query path.
- A daily systemd timer updates only `Subscription-Userinfo`. Manual sync and airport update also refresh applicable traffic headers. Traffic failure preserves the previous headers and every YAML.
- Reuse the 3x-ui installation's acme.sh client, but install SAN certificate files into stable Nginx paths and reload Nginx only after successful renewal. Do not add Certbot.
- No implementation or test connects to the real VPS. Any future VPS write still requires a separate explicit user approval.
- Preserve the untracked user file `pr-body.md`; never inspect, modify, stage, or delete it.
- Use TDD for every task. Stage only named paths, commit each completed task, and keep the worktree recoverable at every checkpoint.
- If Codex exposes a remaining-allowance warning below 30%, estimate the unfinished tasks before dispatching another agent. Stop after a clean commit when the remaining allowance is unlikely to cover implementation plus Sol review.

---

## Target File Structure

| Path | Responsibility |
| --- | --- |
| `clash_sub/domain.py` | Immutable config, 3x-ui snapshot, user state, traffic, release, and operation types. |
| `clash_sub/config.py` | Strictly parse one root-only service configuration; derive private paths without user lists or source URLs. |
| `clash_sub/state.py` | Generate readable tokens, reconcile stable database IDs, persist plaintext root-only state, and resolve owner identity. |
| `clash_sub/xui.py` | Validate the pinned 3x-ui SQLite schema/settings and return clients plus canonical loopback Clash URLs read-only. |
| `clash_sub/sources.py` | Bounded loopback/HTTPS fetches, safe YAML extraction, traffic-header parsing, source merge, and atomic private snapshots. |
| `clash_sub/generator.py` | Choose authorized sources per role/variant and render the shared Jinja template with declarative variants. |
| `clash_sub/checks.py` | Structural/leak checks and a sanitized pinned-Mihomo subprocess runner. |
| `clash_sub/release_store.py` | Create immutable release bundles, compare hashes, verify history, retain five, and prepare rollback targets. |
| `clash_sub/nginx.py` | Render exact static subscription locations and transactionally activate state/routes with Nginx test/reload rollback. |
| `clash_sub/service.py` | Coordinate sync, airport update, traffic update, links, status, history, rollback, and token rotation. |
| `clash_sub/cli.py` | Four-option interactive menu plus the documented non-interactive commands. |
| `config/service.example.yaml` | Synthetic, non-operational configuration example. |
| `templates/clash.yaml.j2` | One shared complete Clash configuration skeleton. |
| `templates/variants/{balanced,standard,privacy}.yaml` | Only the real DNS/group/rule differences. |
| `deploy/nginx/clash-sub.conf.tmpl` | Port 80 ACME fallback, TLS 8443 panel/subscription vhosts, and generated-route include. |
| `deploy/nginx/routes.empty.conf` | Safe initial generated include. |
| `deploy/systemd/clash-sub-traffic.{service,timer}` | One short daily traffic-header update. |
| `tests/fixtures/xui-3.6.0.sql` | Synthetic pinned-schema SQLite fixture with no real credentials. |
| `tests/test_lightweight_*.py` | Focused unit, integration, CLI, deployment, and security tests for the replacement path. |
| `docs/legacy-trojan-topology.md` | Historical explanation of Trojan 443, fallback 1443, trojan-web 80, and Nginx 8080/1443. |

The old modules and tests remain untouched until Task 11, where the new end-to-end path has already passed.

---

### Task 1: Domain Models and Strict Service Configuration

**Files:**
- Create: `clash_sub/domain.py`
- Create: `clash_sub/config.py`
- Replace: `config/service.example.yaml`
- Create: `tests/test_lightweight_config.py`

**Interfaces:**
- Produces: `ServiceConfig`, `XuiClient`, `XuiSnapshot`, `UserState`, `RuntimeState`, `Traffic`, `PreparedRelease`, `load_config(path, repo_root)`.
- Consumes: only a root-only YAML config and repository template path; no user list or source URL.

- [ ] **Step 1: Write failing configuration/model tests**

Create tests proving the exact variant tuple, immutable dataclasses, successful parsing of the example shape, rejection of unknown keys, relative/symlink-escaped paths, URL schemes in `subscription-authority`, authorities without port `8443`, empty owner email, and private config modes other than `0600`.

```python
def test_loads_minimal_service_config(self):
    config = load_config(self.path, self.root)
    self.assertEqual(config.owner_email, "owner-example")
    self.assertEqual(config.subscription_authority, "sub.example.com:8443")
    self.assertEqual(VARIANTS, ("balanced", "standard", "privacy"))
    self.assertEqual(config.template_root, self.root / "templates")

def test_rejects_unknown_key(self):
    self.write_config(extra="publisher-port: 25501\n")
    with self.assertRaisesRegex(ConfigError, "unsupported configuration"):
        load_config(self.path, self.root)
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `.venv/bin/python -m unittest tests.test_lightweight_config -v`

Expected: FAIL because `clash_sub.domain` and `clash_sub.config` do not exist.

- [ ] **Step 3: Implement immutable types and strict parsing**

Use these public shapes:

```python
VARIANTS = ("balanced", "standard", "privacy")
OWNER_VARIANTS = VARIANTS
MEMBER_VARIANTS = ("standard",)

@dataclass(frozen=True)
class ServiceConfig:
    owner_email: str
    subscription_authority: str
    xui_database: Path
    private_root: Path
    public_root: Path
    nginx_routes: Path
    mihomo_binary: Path
    nginx_binary: Path
    systemctl_binary: Path
    template_root: Path
    max_source_bytes: int = 5 * 1024 * 1024

@dataclass(frozen=True)
class XuiClient:
    client_id: int
    email: str
    sub_id: str
    enabled: bool
    upload: int
    download: int
    total: int
    expiry_ms: int

@dataclass(frozen=True)
class UserState:
    client_id: int
    email: str
    token: str
    readable_code: str
    active: bool
    current_release: str | None

@dataclass(frozen=True)
class RuntimeState:
    schema_version: int
    owner_client_id: int
    users: Mapping[int, UserState]
```

The accepted config keys are exactly:

```yaml
schema-version: 1
owner-email: owner-example
subscription-authority: sub.example.com:8443
xui-database: /etc/x-ui/x-ui.db
private-root: /var/lib/clash-sub/private
public-root: /var/lib/clash-sub/public
nginx-routes: /etc/nginx/clash-sub/routes.conf
mihomo-binary: /usr/local/lib/clash-sub/mihomo
nginx-binary: /usr/sbin/nginx
systemctl-binary: /usr/bin/systemctl
max-source-bytes: 5242880
```

- [ ] **Step 4: Run focused tests and commit**

Run: `.venv/bin/python -m unittest tests.test_lightweight_config -v`

Expected: PASS.

Commit:

```bash
git add clash_sub/domain.py clash_sub/config.py config/service.example.yaml tests/test_lightweight_config.py
git commit -m "feat: define lightweight service configuration"
```

---

### Task 2: Root-Only State, Readable Tokens, and Stable Identity

**Files:**
- Create: `clash_sub/state.py`
- Create: `tests/test_lightweight_state.py`

**Interfaces:**
- Consumes: `RuntimeState`, current `XuiClient` values, configured `owner_email`.
- Produces: `generate_token(existing_codes) -> tuple[str, str]`, `load_state(path)`, `save_state(path, state)`, `reconcile_state(previous, clients, owner_email)`, `rotate_user_token(state, client_id)`.

- [ ] **Step 1: Write failing token and reconciliation tests**

Cover 32-byte core length, unpadded Base64URL, six-character unambiguous code, collision retry, exact full-token validation, mode `0600`, atomic replacement, first-run owner matching, owner persisted by database ID after email rename, same-ID disable/re-enable retaining token, deleted/recreated client receiving a new token, duplicate emails/subIds failing closed, and missing persisted owner requiring explicit reinitialization.

```python
def test_token_has_random_core_and_readable_suffix(self):
    token, code = generate_token(set(), random_bytes=lambda size: b"x" * size,
                                 choose=lambda alphabet: "K")
    core, suffix = token.rsplit("-", 1)
    self.assertEqual(len(base64.urlsafe_b64decode(core + "=")), 32)
    self.assertEqual(suffix, "KKKKKK")
    self.assertEqual(code, suffix)

def test_email_rename_does_not_rotate_identity(self):
    updated = reconcile_state(self.state, [client(7, "renamed")], "old-owner")
    self.assertEqual(updated.owner_client_id, 7)
    self.assertEqual(updated.users[7].token, self.state.users[7].token)
    self.assertEqual(updated.users[7].email, "renamed")
```

- [ ] **Step 2: Run the focused test and observe the missing-module failure**

Run: `.venv/bin/python -m unittest tests.test_lightweight_state -v`

- [ ] **Step 3: Implement secure token/state behavior**

Use `secrets.token_bytes(32)`, unpadded `base64.urlsafe_b64encode`, and this readable alphabet:

```python
READABLE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}$")
```

Generate a new six-character code until it is absent from every retained mapping. Save canonical JSON via a same-directory temporary file, `fsync`, mode `0600`, then `os.replace`. Never include token, subId, UUID, or URL in an exception message or object representation.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m unittest tests.test_lightweight_state -v`

Expected: PASS.

```bash
git add clash_sub/state.py tests/test_lightweight_state.py
git commit -m "feat: persist stable subscription identities"
```

---

### Task 3: Read-Only 3x-ui Discovery and Traffic Snapshot

**Files:**
- Create: `clash_sub/xui.py`
- Create: `tests/fixtures/xui-3.6.0.sql`
- Create: `tests/test_lightweight_xui.py`

**Interfaces:**
- Consumes: `ServiceConfig.xui_database`.
- Produces: `read_xui_snapshot(path) -> XuiSnapshot`, where `XuiSnapshot.source_url(client)` returns only a validated loopback Clash URL.

- [ ] **Step 1: Add a synthetic pinned-schema fixture and failing tests**

The fixture defines only the required 3x-ui 3.6.0 surface:

```sql
CREATE TABLE clients (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  sub_id TEXT NOT NULL UNIQUE,
  enable NUMERIC NOT NULL,
  total_gb INTEGER NOT NULL,
  expiry_time INTEGER NOT NULL
);
CREATE TABLE client_traffics (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  up INTEGER NOT NULL,
  down INTEGER NOT NULL
);
CREATE TABLE settings (`key` TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO settings VALUES ('subListen', '127.0.0.1');
INSERT INTO settings VALUES ('subPort', '2096');
INSERT INTO settings VALUES ('subEnable', 'true');
INSERT INTO settings VALUES ('subClashEnable', 'true');
INSERT INTO settings VALUES ('subClashPath', '/clash/');
```

Tests must prove `mode=ro`, `PRAGMA query_only=ON`, no write SQL, stable ordering by client ID, traffic left join, positive/negative expiry normalization, URL quoting of `subId`, and global failure for any missing table/column, duplicate/empty identity, non-loopback `subListen`, invalid port/path, or disabled subscription/Clash output.

```python
def test_constructs_only_loopback_clash_urls(self):
    snapshot = read_xui_snapshot(self.database)
    self.assertEqual(snapshot.source_url(snapshot.clients[0]),
                     "http://127.0.0.1:2096/clash/member-sub-id")
```

- [ ] **Step 2: Run and verify the missing implementation failure**

Run: `.venv/bin/python -m unittest tests.test_lightweight_xui -v`

- [ ] **Step 3: Implement schema introspection and bounded queries**

Open with `sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=1.0)`, execute `PRAGMA query_only=ON`, inspect `sqlite_master` and `PRAGMA table_info`, then issue one settings query and one client/traffic query. Convert all database/SQLite exceptions to `XuiCompatibilityError("3x-ui database compatibility check failed")` without the path, SQL, email, or row value.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m unittest tests.test_lightweight_xui -v`

```bash
git add clash_sub/xui.py tests/fixtures/xui-3.6.0.sql tests/test_lightweight_xui.py
git commit -m "feat: discover 3x-ui clients read only"
```

---

### Task 4: Safe Source Fetching, Snapshots, and Source Isolation

**Files:**
- Create: `clash_sub/sources.py`
- Create: `tests/test_lightweight_sources.py`

**Interfaces:**
- Consumes: validated loopback Clash URLs, one hidden-input airport HTTPS URL, and root-only home/airport YAML snapshots.
- Produces: `fetch_xui_proxies(url, max_bytes)`, `download_airport_proxies(url, max_bytes)`, `load_proxy_snapshot(path)`, `write_proxy_snapshot(path, proxies)`, `merge_proxy_sources(labeled_sources)`, `parse_subscription_userinfo(value)`.

- [ ] **Step 1: Write failing source-policy tests**

Use injected fake openers—never real sockets—to prove:

- x-ui accepts only `http://127.0.0.1:<port>/<path>` with no userinfo/query/fragment;
- airport accepts only HTTPS and every redirect remains HTTPS, with at most three redirects;
- body reads stop at `max_source_bytes + 1` and reject oversized/empty/non-mapping YAML;
- `proxies` is a non-empty list of mappings with non-empty names;
- airport URL never appears in exceptions, `repr`, snapshots, manifests, or captured output;
- `Subscription-Userinfo` accepts only non-negative integer fields and a bounded header;
- name collisions receive deterministic ` [3x-ui]`, ` [机场]`, or ` [家庭]` suffixes and all originals stay immutable.

```python
def test_airport_error_never_echoes_url(self):
    secret = "https://airport.example/private-five-minute-token"
    with self.assertRaises(SourceError) as caught:
        download_airport_proxies(secret, 1024, opener=self.failing_opener)
    self.assertNotIn(secret, str(caught.exception))
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m unittest tests.test_lightweight_sources -v`

- [ ] **Step 3: Implement with standard-library HTTPS and safe YAML only**

Use `urllib.request` with a custom `HTTPRedirectHandler`, default TLS verification, connect/read timeout 15 seconds, and `yaml.safe_load`. Private snapshot writes use canonical `{"proxies": [...]}` YAML, mode `0600`, `fsync`, and `os.replace`. Never save or return the airport URL.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m unittest tests.test_lightweight_sources -v`

```bash
git add clash_sub/sources.py tests/test_lightweight_sources.py
git commit -m "feat: load bounded private proxy sources"
```

---

### Task 5: Standard Variant, Authorized Rendering, and Mihomo Validation

**Files:**
- Create: `clash_sub/generator.py`
- Create: `clash_sub/checks.py`
- Create: `templates/variants/standard.yaml`
- Modify: `templates/clash.yaml.j2`
- Modify: `templates/variants/balanced.yaml`
- Modify: `templates/variants/privacy.yaml`
- Create: `tests/test_lightweight_generator.py`
- Create: `tests/test_lightweight_checks.py`

**Interfaces:**
- Consumes: isolated proxy tuples and `ServiceConfig.mihomo_binary`.
- Produces: `render_user_bundle(is_owner, xui, airport, home, template_root) -> Mapping[str, str]`, `validate_clash(text, forbidden_values)`, `MihomoValidator.validate(path)`.

- [ ] **Step 1: Write failing source-scope and validation tests**

Assert exact bundles:

```python
member = render_user_bundle(False, [member_node], [], [], TEMPLATE_ROOT)
self.assertEqual(tuple(member), ("standard",))
self.assertEqual(proxy_names(member["standard"]), ["Member 3x-ui"])

owner = render_user_bundle(True, [owner_node], [airport_node], [home_node], TEMPLATE_ROOT)
self.assertEqual(set(proxy_names(owner["balanced"])), {"Owner 3x-ui", "Airport", "Home"})
self.assertEqual(set(proxy_names(owner["standard"])), {"Owner 3x-ui", "Airport"})
self.assertEqual(set(proxy_names(owner["privacy"])), {"Owner 3x-ui", "Airport", "Home"})
```

Also cover strict Jinja undefined values, unique proxy/group names, valid group/rule targets, no `proxy-providers`, complete REALITY options, no forbidden source URL/token/loopback URL, and sanitized Mihomo timeout/non-zero errors. The fake Mihomo runner records exactly `(<binary>, "-t", "-f", <candidate>)` and no candidate content is captured or printed.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_lightweight_generator tests.test_lightweight_checks -v`

- [ ] **Step 3: Implement the minimal authorized generator**

Reuse only the safe Jinja/YAML concepts from the legacy renderer. Variant source selection must occur before merge/render and must not be inferred from proxy names. Copy the current `balanced-win.yaml` content to `standard.yaml` as the starting maintained variant, then remove every stale home-only target so it validates without a home source. Keep the original private reference files untouched.

`MihomoValidator` runs:

```python
subprocess.run(
    [str(binary), "-t", "-f", str(candidate)],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    timeout=30,
    check=False,
)
```

- [ ] **Step 4: Run focused tests and commit**

Run: `.venv/bin/python -m unittest tests.test_lightweight_generator tests.test_lightweight_checks -v`

```bash
git add clash_sub/generator.py clash_sub/checks.py templates/clash.yaml.j2 templates/variants/balanced.yaml templates/variants/standard.yaml templates/variants/privacy.yaml tests/test_lightweight_generator.py tests/test_lightweight_checks.py
git commit -m "feat: render isolated Clash variants"
```

---

### Task 6: Immutable Releases, Five-Version Retention, and Rollback

**Files:**
- Create: `clash_sub/release_store.py`
- Create: `tests/test_lightweight_release_store.py`

**Interfaces:**
- Consumes: validated rendered bundles and current release ID.
- Produces: `ReleaseStore.prepare(client_id, bundle, input_hashes) -> PreparedRelease | None`, `verify_release(client_id, release_id)`, `history(client_id)`, `prune(client_id, keep=5)`.

- [ ] **Step 1: Write failing immutable-release tests**

Cover private staging `0700`, files initially `0600`, immutable public release files `0640`, manifest/digest verification, sanitized metadata, no-op on identical output hashes, owner three-file completeness, rejection of unsafe IDs/symlinks/path traversal, failed write leaving the prior release untouched, hash-verified rollback lookup, and pruning only after six successful releases while preserving private references.

```python
def test_identical_bundle_returns_no_new_release(self):
    first = self.store.prepare(7, self.bundle, {"xui": "a" * 64})
    self.store.mark_current(7, first.release_id)
    second = self.store.prepare(7, self.bundle, {"xui": "a" * 64})
    self.assertIsNone(second)
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m unittest tests.test_lightweight_release_store -v`

- [ ] **Step 3: Implement immutable version directories**

Store public YAML at `public/releases/<client-id>/<release-id>/clash-<variant>.yaml` and root-only manifests at `private/releases/<client-id>/<release-id>/manifest.json`. Release IDs are UTC timestamps plus a random suffix matching `^[0-9TZ-]+-[a-f0-9]{8}$`. Do not create a public `current` symlink; Nginx routes point to immutable release paths so old workers keep serving the old bundle until reload succeeds.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m unittest tests.test_lightweight_release_store -v`

```bash
git add clash_sub/release_store.py tests/test_lightweight_release_store.py
git commit -m "feat: store immutable Clash releases"
```

---

### Task 7: Exact Nginx Routes and Transactional Activation

**Files:**
- Create: `clash_sub/nginx.py`
- Create: `deploy/nginx/routes.empty.conf`
- Create: `tests/test_lightweight_nginx.py`

**Interfaces:**
- Consumes: candidate `RuntimeState`, active `XuiClient` values, verified release paths, and current traffic.
- Produces: `render_routes(config, state, clients) -> str`, `activate_runtime(config, state, routes, runner, extra_replacements=())`.

- [ ] **Step 1: Write failing route and transaction tests**

Assert one exact location per authorized token/variant, lowercase public filename, immutable alias path, fixed title/download filename, `Subscription-Userinfo`, `access_log off`, `log_not_found off`, request rate/body limits, query rejection, and no generic filesystem mapping. Assert a member has only `standard`, owner has three, disabled/deleted clients have none, readable code alone has none, and emails never appear.

```python
def test_member_route_is_exact_and_anonymous(self):
    text = render_routes(self.config, self.state, [self.member])
    self.assertIn("location = /s/%s/clash-standard.yaml" % self.member_token, text)
    self.assertNotIn("clash-balanced.yaml", text)
    self.assertNotIn(self.member.email, text)
    self.assertNotIn("location /s/", text)
```

Use a fake command runner to prove this activation sequence:

1. write and `fsync` candidate state/routes beside their targets;
2. preserve old bytes;
3. atomically install both candidates;
4. run `<nginx_binary> -t` with all output suppressed;
5. run `<systemctl_binary> reload nginx` only after a successful test;
6. restore both old files on test failure;
7. restore both and reload the old config on reload failure.

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m unittest tests.test_lightweight_nginx -v`

- [ ] **Step 3: Implement exact generated locations and rollback**

Each generated block follows this shape with validated values only:

```nginx
location = /s/<full-token>/clash-standard.yaml {
    if ($args != "") { return 404; }
    limit_req zone=clash_subscription burst=5 nodelay;
    client_max_body_size 1k;
    limit_except GET HEAD { deny all; }
    access_log off;
    log_not_found off;
    default_type "text/yaml; charset=utf-8";
    alias /var/lib/clash-sub/public/releases/7/<release>/clash-standard.yaml;
    add_header Profile-Title "Clash Standard" always;
    add_header Content-Disposition 'attachment; filename="Clash-Standard.yaml"' always;
    add_header Subscription-Userinfo "upload=0; download=0; total=0; expire=0" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Cache-Control no-store always;
}
```

Never include subprocess stdout/stderr or a generated route line in an exception.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m unittest tests.test_lightweight_nginx -v`

```bash
git add clash_sub/nginx.py deploy/nginx/routes.empty.conf tests/test_lightweight_nginx.py
git commit -m "feat: activate static Nginx subscriptions"
```

---

### Task 8: Sync, Airport, Traffic, Rotation, and Failure Semantics

**Files:**
- Create: `clash_sub/service.py`
- Create: `tests/test_lightweight_service.py`

**Interfaces:**
- Consumes: all Tasks 1–7 interfaces through dependency-injected collaborators.
- Produces: `ClashSubService.sync_all()`, `update_airport(url)`, `traffic_update()`, `links()`, `status()`, `history(user)`, `rollback(user, release)`, `rotate_link(user)`.

- [ ] **Step 1: Write failing orchestration tests**

Cover these exact state transitions:

- first sync discovers every client, persists stable tokens, generates owner three variants/member standard, and activates all routes once;
- unchanged sync updates traffic metadata but does not create a release or call Mihomo;
- one member source failure retains that member's old release while another member updates;
- owner source/render/Mihomo failure retains the whole prior owner bundle;
- database compatibility failure changes no state, snapshot, release pointer, route, or Nginx process;
- disable/delete revokes the route at the next successful sync; re-enable same database ID restores the same token; delete/recreate gets a new token;
- airport success commits snapshot, owner three-variant release, owner traffic, state, and routes together; any failure restores all old artifacts;
- `traffic_update` changes only routes/state traffic metadata, creates no YAML/release, and preserves old headers on failure;
- rollback activates an existing verified release without fetching sources;
- rotation changes random core and readable code, keeps current release, removes the old route, and returns all new authorized URLs;
- after activation, prune to five releases; never prune a release referenced by restored old routes after failure.

```python
def test_daily_traffic_update_never_builds_yaml(self):
    before = tuple(self.release_store.history(7))
    self.service.traffic_update()
    self.assertEqual(tuple(self.release_store.history(7)), before)
    self.assertEqual(self.generator.calls, 0)
    self.assertEqual(self.mihomo.calls, 0)
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m unittest tests.test_lightweight_service -v`

- [ ] **Step 3: Implement one transactional activation boundary**

Define `ServiceError` with a stable `code` and no collaborator text. Build releases as immutable unreferenced candidates first, then construct candidate state and routes entirely in memory. Use `activate_runtime` once per operation. For airport update, pass the candidate snapshot as an `extra_replacements` entry so state, routes, and snapshot are installed and restored as one transaction around `nginx -t`/reload. Only after activation succeeds may the service prune old releases. Return sanitized result objects containing client ID/email, release ID, variant names, and stable error codes—but never token except from `links()`/successful `rotate_link()`.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m unittest tests.test_lightweight_service -v`

```bash
git add clash_sub/service.py tests/test_lightweight_service.py
git commit -m "feat: orchestrate lightweight subscription updates"
```

---

### Task 9: Four-Option Menu and Minimal Commands

**Files:**
- Create: `clash_sub/cli.py`
- Modify: `bin/clash-sub`
- Create: `tests/test_lightweight_cli.py`

**Interfaces:**
- Consumes: `ClashSubService`.
- Produces: `main(argv=None, stdin=None, stdout=None, stderr=None, service_factory=None) -> int`.

- [ ] **Step 1: Write failing CLI tests**

Assert a no-argument invocation shows exactly:

```text
1. 更新机场订阅
2. 同步所有配置
3. 查看订阅链接
4. 查看状态和历史版本
0. 退出
```

Test hidden airport input through an injected `getpass` function; the URL must not appear in stdout/stderr. `links` prints every active user in database-ID order, groups by internal email, prints `[ABC234]`, and lists all authorized full URLs. `status` never prints token/code/subId/UUID/source URL. Test the exact non-interactive commands `sync`, `traffic-update`, `status`, `links`, `history <user>`, `rollback <user> <release>`, and `rotate-link <user>`; reject `refresh`, `refresh-all`, `clashctl`, unknown options, and airport URLs passed as arguments.

```python
def test_links_lists_every_user_without_selection(self):
    result = run_cli(["links"], service=self.service)
    self.assertIn("Alice [ABC234]", result.stdout)
    self.assertIn("Bob [XYZ789]", result.stdout)
    self.assertNotIn("请选择用户", result.stdout)
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m unittest tests.test_lightweight_cli -v`

- [ ] **Step 3: Implement the CLI and switch the entry point**

Use `argparse` only for documented subcommands. The menu catches `EOFError`/`KeyboardInterrupt` and exits without modifying state. All operational errors print one stable Chinese summary plus an error code; tracebacks and collaborator exception text stay out of terminal output.

Change `bin/clash-sub` to import only `clash_sub.cli.main`.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m unittest tests.test_lightweight_cli -v`

```bash
git add clash_sub/cli.py bin/clash-sub tests/test_lightweight_cli.py
git commit -m "feat: add the clash-sub management menu"
```

---

### Task 10: Native Nginx, Daily Traffic Timer, and Manual Deployment Assets

**Files:**
- Create: `deploy/nginx/clash-sub.conf.tmpl`
- Create: `deploy/systemd/clash-sub-traffic.service`
- Create: `deploy/systemd/clash-sub-traffic.timer`
- Create: `tests/test_lightweight_deployment.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: generated routes and stable acme.sh certificate paths.
- Produces: manually installable native Nginx/systemd files with no installer or resident application service.

- [ ] **Step 1: Write failing deployment-asset tests**

Assert:

- TCP 80 serves only `/.well-known/acme-challenge/` and generic 404;
- TCP 8443 has default, `panel.<domain>`, and `sub.<domain>` TLS servers sharing one SAN certificate pair;
- panel proxies only its random base path to loopback;
- subscription server includes `routes.conf`, defines `limit_req_zone`, and returns generic 404 with access logging off for every unmatched `/s/` path;
- no `proxy_pass` exists in the subscription server;
- no TCP 443/1443/8080, UDP, stream, Docker, publisher, subconverter, Certbot, directory listing, raw `/sub/`, `/json/`, or `/clash/` exposure exists;
- timer runs `clash-sub traffic-update` once daily with `Persistent=true`, a small randomized delay, tight systemd hardening, and no config generation command;
- deployment creates `/var/lib/clash-sub/public` as `root:www-data` mode `02750` before any release is generated, and asserts that this setgid/group contract is the prerequisite for Nginx to read `0640` release YAML;
- requirements remain exactly Jinja2 and PyYAML pins.

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m unittest tests.test_lightweight_deployment -v`

- [ ] **Step 3: Implement static host assets**

The Nginx template placeholders are exactly `{{DOMAIN}}`, `{{PANEL_BASE_PATH}}`, `{{PANEL_UPSTREAM}}`, `{{FULLCHAIN_PATH}}`, `{{PRIVKEY_PATH}}`, and `{{ROUTES_INCLUDE}}`. Do not add a render/install script; the deployment guide will use explicit `sed`/editor steps and `nginx -t` before reload.

The manual deployment procedure must create `/var/lib/clash-sub/public` with owner/group `root:www-data` and mode `02750` before the first `clash-sub sync`. `ReleaseStore` inherits and verifies that numeric group for every public directory and release file; the Python runtime never calls `chown` or hardcodes a group name.

The timer invokes:

```ini
[Service]
Type=oneshot
ExecStart=/usr/local/bin/clash-sub traffic-update
User=root
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/clash-sub/private /var/lib/clash-sub/public /etc/nginx/clash-sub
```

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m unittest tests.test_lightweight_deployment -v`

```bash
git add deploy/nginx/clash-sub.conf.tmpl deploy/systemd/clash-sub-traffic.service deploy/systemd/clash-sub-traffic.timer tests/test_lightweight_deployment.py requirements.txt
git commit -m "feat: add native lightweight deployment assets"
```

---

### Task 11: Remove the Superseded Runtime Without Compatibility Aliases

**Files:**
- Delete: `Dockerfile`
- Delete: `compose.yaml`
- Delete: `config/subconverter/pref.ini`
- Delete: `config/users.example.yaml`
- Delete: `clash_sub/converter.py`
- Delete: `clash_sub/host_cli.py`
- Delete: `clash_sub/manager.py`
- Delete: `clash_sub/models.py`
- Delete: `clash_sub/publisher.py`
- Delete: `clash_sub/reference_rules.py`
- Delete: `clash_sub/releases.py`
- Delete: `clash_sub/rendering.py`
- Delete: `clash_sub/settings.py`
- Delete: `clash_sub/traffic.py`
- Delete: `clash_sub/validation.py`
- Replace: `clash_sub/__init__.py`
- Delete: `templates/variants/balanced-win.yaml`
- Delete: `scripts/check_certificate.py`
- Delete: `scripts/install-server.sh`
- Delete: `scripts/install_server.py`
- Delete: `scripts/migrate_reference_templates.py`
- Delete: `scripts/server_preflight.py`
- Delete: `deploy/nginx/00-acme-http.conf.tmpl`
- Delete: `deploy/nginx/10-clash-domain.conf.tmpl`
- Delete: `deploy/nginx/10-clash-ip.conf.tmpl`
- Delete: `deploy/systemd/clash-sub-cert-*`
- Delete: legacy `tests/test_*.py` files not named `test_lightweight_*`, `test_repository_safety.py`, `test_secret_scan.py`, or `test_reality_target.py`
- Modify: `tests/test_repository_safety.py`
- Modify: `tests/test_secret_scan.py`

**Interfaces:**
- Consumes: the fully passing replacement path from Tasks 1–10.
- Produces: one unambiguous runtime with no Docker-era import or command surface.

- [ ] **Step 1: Add failing absence assertions before deletion**

Update repository safety tests to assert the forbidden runtime paths above do not exist, `rg` finds no active reference to `publisher`, `subconverter`, `balanced-win`, `refresh`, `Certbot`, or Docker outside historical specs/plans and the dedicated legacy topology document, and `clash_sub.__all__` exposes only the new supported types/errors.

- [ ] **Step 2: Run the repository safety test and observe failure**

Run: `.venv/bin/python -m unittest tests.test_repository_safety -v`

- [ ] **Step 3: Delete only the listed superseded files and clean exports**

Replace `clash_sub/__init__.py` with imports for `ServiceConfig`, `ConfigError`, `ClashSubService`, and `ServiceError`. Do not retain shim modules, aliases, deprecated flags, or migration code. Preserve `scripts/check_reality_target.py`, `scripts/scan_tracked_secrets.py`, private ignored data, reference originals, historical specs/plans, and `pr-body.md`.

- [ ] **Step 4: Run the complete remaining unit suite and commit**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: PASS with no socket-binding tests and no Docker requirement.

```bash
git add -u Dockerfile compose.yaml config clash_sub templates scripts deploy tests
git add clash_sub/__init__.py tests/test_repository_safety.py tests/test_secret_scan.py
git commit -m "refactor: remove the Docker subscription stack"
```

Before committing, inspect `git diff --cached --name-status` and unstage anything outside the explicit Task 11 list. Never use `git add -A`.

---

### Task 12: End-to-End Failure Proof and Secret Scanning

**Files:**
- Create: `tests/test_lightweight_end_to_end.py`
- Modify: `scripts/scan_tracked_secrets.py`
- Modify: `tests/test_secret_scan.py`

**Interfaces:**
- Consumes: the final replacement runtime.
- Produces: acceptance evidence for isolation, static publication, failure preservation, and tracked/private secret checks.

- [ ] **Step 1: Write a synthetic end-to-end harness**

Compose a real temporary SQLite database, config, state store, renderer, release store, and route activator with fake HTTP/Mihomo/Nginx runners. Prove in independent tests:

1. owner three variants contain the exact source scopes and member standard contains only its own node;
2. links list all users with anonymous full URLs and unique readable suffixes;
3. a member token cannot address another variant or user because no exact Nginx location is generated;
4. database/source/render/Mihomo/Nginx failures preserve prior active bytes and metadata;
5. airport URL and credentials never enter tracked output, state, manifest, routes, exception text, or captured terminal output;
6. traffic-only update changes only the route header and invokes neither renderer nor Mihomo;
7. rollback and token rotation are exact and old routes disappear;
8. six successful content changes retain five verified releases;
9. an optional `MIHOMO_BIN` environment path runs the real binary against all three generated fixtures, otherwise the test is explicitly skipped.

- [ ] **Step 2: Run and verify at least one acceptance test fails before harness completion**

Run: `.venv/bin/python -m unittest tests.test_lightweight_end_to_end -v`

- [ ] **Step 3: Extend the scanner for the final URL/token format**

Detect concrete `/s/<43-char-core>-<six-readable-code>/clash-<variant>.yaml` paths and any tracked `vless://`, `trojan://`, airport userinfo URL, random UUID, private key, or generated runtime path. Reports contain only category and relative path, never the matching value. Documentation placeholders such as `<token>` and RFC 5737 addresses remain allowed.

- [ ] **Step 4: Run full verification and commit**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/scan_tracked_secrets.py
git diff --check
```

Expected: all tests PASS, scanner exits 0, and diff check prints nothing.

```bash
git add tests/test_lightweight_end_to_end.py scripts/scan_tracked_secrets.py tests/test_secret_scan.py
git commit -m "test: prove lightweight subscription isolation"
```

---

### Task 13: Controlled Deployment and Operations Documentation

**Files:**
- Replace: `README.md`
- Replace: `DEPLOYMENT.md`
- Replace: `docs/3x-ui-setup.md`
- Replace: `docs/operations.md`
- Replace: `docs/private-data.md`
- Create: `docs/legacy-trojan-topology.md`
- Modify: `tests/test_lightweight_deployment.py`
- Modify: `tests/test_repository_safety.py`

**Interfaces:**
- Consumes: final commands/assets and approved spec.
- Produces: command-by-command clean Debian deployment and small-screen/mobile SSH operation guidance, with no one-click installer.

- [ ] **Step 1: Add failing documentation assertions**

Assert active documentation covers:

- 512 MiB RAM / 256 MiB Swap / 10 GiB disk constraints and idle-process list;
- manual 3x-ui 3.6.0 and Xray 26.6.27 installation/verification without executing them;
- REALITY TCP 443, Nginx 80/8443, no UDP 443/public 1443;
- 3x-ui panel and subscription listeners on `127.0.0.1`, Clash output enabled, and SQLite at the configured path;
- Python venv, two pinned dependencies, pinned Mihomo checksum verification, root/public directory ownership and modes;
- acme.sh DNS/HTTP issuance for one SAN certificate, `--install-cert` stable paths, and reload command;
- explicit Nginx template editing, `nginx -t`, systemd timer install, first `clash-sub` sync, links, airport update over mobile SSH, status, rollback, rotation, and incident handling;
- 3x-ui upgrade procedure: backup, stop, inspect pinned schema in a copy, upgrade, run `clash-sub status/sync`, and rely on old YAML/routes when compatibility fails;
- domain/IP/VPS replacement checklist and the fact REALITY by IP does not use the Nginx certificate;
- no short links, no daily YAML regeneration, no live request query, no Telegram, and no requirement to remember `refresh`;
- historical Trojan topology isolated to `docs/legacy-trojan-topology.md` and explicitly not executable on the new host.

- [ ] **Step 2: Run documentation tests and observe failure**

Run: `.venv/bin/python -m unittest tests.test_lightweight_deployment tests.test_repository_safety -v`

- [ ] **Step 3: Write concise, copyable manual procedures**

Every mutating server command is a separate documented step preceded by a read-only check and an explanation of expected output. Do not provide a script that installs packages, modifies firewall/Nginx/systemd, issues certificates, or touches 3x-ui automatically. Mark all domains, paths, emails, ports, and hashes as examples/placeholders.

The legacy document records:

- Trojan owned public 443;
- non-Trojan TLS could be forwarded to `fallback_addr 127.0.0.1`, `fallback_port 1443`;
- `trojan-web` occupied public 80;
- Nginx's Debian/default HTTP listener was moved from 80 to 8080 to avoid that conflict;
- Nginx 1443 was the TLS fallback site;
- none of these ports/fallbacks are part of the new 3x-ui-only server.

- [ ] **Step 4: Run docs/full tests and commit**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/scan_tracked_secrets.py
git diff --check
```

```bash
git add README.md DEPLOYMENT.md docs/3x-ui-setup.md docs/operations.md docs/private-data.md docs/legacy-trojan-topology.md tests/test_lightweight_deployment.py tests/test_repository_safety.py
git commit -m "docs: document the lightweight Clash service"
```

---

### Task 14: Final Local Verification and Terra Handoff Record

**Files:**
- Create: `.superpowers/sdd/2026-08-23-clash-sub-lightweight/verification.md` (ignored local artifact; do not stage)
- Modify only if verification finds a defect: the smallest affected implementation/test/documentation files.

**Interfaces:**
- Consumes: all committed implementation tasks.
- Produces: local evidence ready for independent Sol review; no VPS deployment.

- [ ] **Step 1: Run the complete verification matrix**

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python -m compileall -q clash_sub scripts
git diff --check
git status --short --branch
```

Expected: tests PASS, scanner exits 0, compileall exits 0, diff check is empty, branch contains only intended commits, and the only pre-existing untracked user file remains `pr-body.md`.

- [ ] **Step 2: Inspect forbidden runtime references**

Run:

```bash
rg -n "subconverter|publisher|balanced-win|refresh-all|clashctl|certbot|docker compose" --glob '!docs/superpowers/**' --glob '!docs/legacy-trojan-topology.md' .
```

Expected: no active runtime/deployment result. Any match must be either removed or explicitly asserted as forbidden by a test.

- [ ] **Step 3: Record sanitized results locally**

Write only command names, exit codes, test counts, skipped optional Mihomo reason, and commit hashes. Do not include route text, domains, IPs, tokens, emails, source URLs, file contents, or private paths. Keep this `.superpowers` artifact ignored.

- [ ] **Step 4: Close the verification gate**

If any command in Steps 1–2 fails, Task 14 remains incomplete: return to the task that owns the failing file, add a focused regression test there, observe the failure, make the smallest correction, rerun that focused suite and this complete matrix, and commit only those named test/implementation files. If every command passes, make no verification-only commit.

---

## Execution and Review Handoff

Execute Tasks 1–14 with `superpowers:subagent-driven-development`. Every implementation subagent must explicitly use model `gpt-5.6-terra`, work only in the existing feature worktree, follow the task's named write set, and return changed paths plus focused-test evidence. The coordinating agent reviews each task before proceeding and never copies changes from `pr-body.md`.

After Terra completes and Task 14 passes, dispatch one independent `gpt-5.6-sol` agent for final review. Sol must inspect the complete diff against the 2026-08-23 spec, with special attention to read-only SQLite enforcement, cross-user source isolation, token/airport secrecy, Nginx exact-path safety, activation rollback, immutable release pruning, disabled/deleted user revocation, lack of resident services, and deployment-document accuracy. Terra fixes findings; Sol rechecks every high-risk correction. Do not connect to or modify the real VPS during either phase.
