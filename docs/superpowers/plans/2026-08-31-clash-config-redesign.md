# Clash Config Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the old office/universal/privacy model with exact `Clash-Compat` and `Clash-Balance` outputs, a client-only Home overlay, and an independently updated owner-only `AmyTelecom.yaml`.

**Architecture:** Keep the existing state, release, Nginx activation, installer, and user-management framework. Centralize the two-profile contract, simplify round-trip template composition, move the airport artifact into a dedicated stable store, and remove all server-side Home behavior and legacy routes.

**Tech Stack:** Python 3.9+, `unittest`, ruamel.yaml round-trip YAML, PyYAML validation, Jinja2 Nginx templates, Mihomo CLI validation, atomic POSIX file replacement.

**Spec:** `docs/superpowers/specs/2026-08-31-clash-config-redesign-design.md`

## Global Constraints

- Exact profile filenames and titles are `Clash-Compat.yaml` / `Clash-Compat` and `Clash-Balance.yaml` / `Clash-Balance`.
- Owner receives `compat` and `balance`; a member receives only `compat`.
- Only owner configurations contain the `AmyTelecom` proxy provider.
- The provider file and URL are exactly `AmyTelecom.yaml`; the provider key/title remains `AmyTelecom`.
- The provider cache path is `./proxy_providers/AmyTelecom.yaml` and `interval` is exactly `604800`.
- Compat common comments and Balance DNS-specific comments must survive template synchronization and final rendering.
- Airport update must not generate or activate a main configuration release.
- Server-side `home.yaml`, office variants, privacy, legacy filenames, redirects, and compatibility routes are removed.
- `private/clash-verge-home.js` is the only Home data source and applies only to titles `Clash-Compat` and `Clash-Balance`.
- Project-use documentation remains the four Chinese files `README.md`, `DEPLOYMENT.md`, `docs/template-design.md`, and `docs/operations.md`; development specs and plans under `docs/superpowers/` are excluded from that count.
- Never print or commit iCloud source secrets, airport source URLs, subscription tokens, UUIDs, or private Home values.

## File Responsibility Map

- `clash_sub/domain.py`: canonical variants, filenames, titles, and shared immutable domain values.
- `clash_sub/generator.py`: compose Compat/Balance round-trip documents and inject x-ui/provider references.
- `clash_sub/template_sync.py`: read one or both iCloud sources, sanitize Compat, extract Balance DNS/comments, report ignored differences, and atomically update tracked templates.
- `clash_sub/airport_store.py`: validate ownership/mode, stage, validate, atomically replace, and read the single stable airport provider.
- `clash_sub/release_store.py`: immutable main-profile releases only; no airport bytes.
- `clash_sub/nginx.py`: exact owner/member subscription routes and stable owner-only provider route.
- `clash_sub/service.py`: orchestrate sync, provider update, rotation, rollback, and authorization without server Home state.
- `clash_sub/runtime.py`: wire the service to the airport store and remove Home dependencies.
- `clash_sub/checks.py`: validate the fixed provider URL/path/interval contract.
- `clash_sub/installer.py`: create the stable provider directory with the required group ownership and mode.
- `clash_sub/manage.py`: create the four-file rebuild backup.
- `scripts/scan_tracked_secrets.py`: retain tracked-secret protection without expecting `private/home.yaml`.
- `private/clash-verge-home.js`: local-only Home injection for the two exact new titles.

---

### Task 1: Establish the two-profile contract and simplify rendering

**Files:**
- Modify: `clash_sub/domain.py`
- Modify: `clash_sub/generator.py`
- Modify: `clash_sub/checks.py`
- Modify: `templates/profiles.yaml`
- Modify: `tests/test_lightweight_generator.py`
- Modify: `tests/test_lightweight_checks.py`

**Interfaces:**
- Produces: `VARIANTS = ("compat", "balance")`, `OWNER_VARIANTS`, `MEMBER_VARIANTS`, `PROFILE_FILENAMES`, `PROFILE_TITLES`, and `AIRPORT_FILENAME`.
- Produces: `AirportProvider(url: str)` with no digest field.
- Produces: `render_user_bundle(is_owner: bool, xui: list[Mapping], airport: AirportProvider | None, template_root: Path) -> dict[str, str]`.
- Consumes later: release storage, Nginx routing, service URL generation, and template validation import these exact constants/signature.

- [ ] **Step 1: Replace old generator tests with failing authorization, DNS, comment, and provider tests**

```python
def test_fixed_owner_and_member_profile_sets(self):
    owner = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
    member = render_user_bundle(False, [reality_proxy("Member")], None, self.templates)
    self.assertEqual(tuple(owner), ("compat", "balance"))
    self.assertEqual(tuple(member), ("compat",))

def test_balance_replaces_only_dns_and_preserves_comments(self):
    rendered = render_user_bundle(True, [reality_proxy("Owner")], provider(), self.templates)
    self.assertIn("# compat shared comment", rendered["balance"])
    self.assertIn("# balance dns comment", rendered["balance"])
    self.assertNotIn("compat-only-dns", rendered["balance"])

def test_member_has_no_airport_provider(self):
    rendered = yaml.safe_load(
        render_user_bundle(False, [reality_proxy("Member")], None, self.templates)["compat"]
    )
    self.assertNotIn("proxy-providers", rendered)
```

- [ ] **Step 2: Add failing provider validation tests for the fixed contract**

```python
def test_owner_provider_requires_stable_filename_and_weekly_interval(self):
    document = valid_document()
    document["proxy-providers"] = {
        "AmyTelecom": {
            "type": "http",
            "url": PROVIDER_URL,
            "path": "./proxy_providers/AmyTelecom.yaml",
            "interval": 604800,
        }
    }
    validate_clash(yaml.safe_dump(document), (), PROVIDER_URL)
```

Also add one rejection each for the old digest path, `interval: 0`, an extra provider, and a member document containing `AmyTelecom`.

- [ ] **Step 3: Run the focused tests and confirm the old model fails**

Run: `.venv/bin/python -m unittest tests.test_lightweight_generator tests.test_lightweight_checks -v`

Expected: failures mention old variants, the five-argument Home renderer, digest paths, or interval `0`.

- [ ] **Step 4: Implement canonical metadata and the reduced renderer**

Use one source of truth in `domain.py`:

```python
VARIANTS = ("compat", "balance")
OWNER_VARIANTS = VARIANTS
MEMBER_VARIANTS = ("compat",)
PROFILE_FILENAMES = MappingProxyType({
    "compat": "Clash-Compat.yaml",
    "balance": "Clash-Balance.yaml",
})
PROFILE_TITLES = MappingProxyType({
    "compat": "Clash-Compat",
    "balance": "Clash-Balance",
})
AIRPORT_FILENAME = "AmyTelecom.yaml"

@dataclass(frozen=True)
class AirportProvider:
    url: str
```

Make the renderer use only the x-ui inline source. Owner renders both variants and receives the provider; member renders Compat only and rejects any provider argument. Remove every `HomeOverlay`, `_HOME_VARIANTS`, Home merge, Home rule, and Home comment branch from `generator.py`.

Render the provider exactly as:

```python
providers[PROVIDER_NAME] = CommentedMap({
    "type": "http",
    "url": airport.url,
    "interval": 604800,
    "path": "./proxy_providers/AmyTelecom.yaml",
})
```

Update `templates/profiles.yaml` to contain only `compat`/`balance` DNS recipes and the existing node/provider injection group declarations, with no `home` key.

- [ ] **Step 5: Run focused tests**

Run: `.venv/bin/python -m unittest tests.test_lightweight_generator tests.test_lightweight_checks -v`

Expected: PASS.

- [ ] **Step 6: Commit the profile contract**

```bash
git add clash_sub/domain.py clash_sub/generator.py clash_sub/checks.py templates/profiles.yaml tests/test_lightweight_generator.py tests/test_lightweight_checks.py
git commit -m "refactor: replace legacy profile model"
```

### Task 2: Rewrite iCloud template synchronization around Compat plus Balance DNS

**Files:**
- Modify: `clash_sub/template_sync.py`
- Modify: `clash_sub/cli.py`
- Delete: `templates/base/compat-office.yaml`
- Delete: `templates/dns/balance-office.yaml`
- Create: `templates/base/Clash-Compat.yaml`
- Create: `templates/dns/Clash-Balance.yaml`
- Modify: `tests/test_lightweight_template_sync.py`
- Modify: `tests/test_lightweight_cli.py`

**Interfaces:**
- Produces: `default_source_paths(home: Path | None = None) -> tuple[Path, Path]` ending in `Clash-Compat.yaml` and `Clash-Balance.yaml`.
- Produces: `run_template_sync(repo_root: Path, compat: Path | None = None, balance: Path | None = None) -> TemplateSyncReport`.
- Produces: `TemplateSyncReport(changed: tuple[str, ...], lines: tuple[str, ...], ignored_balance_paths: tuple[str, ...])`.
- Consumes: Task 1 renderer and profile metadata for synthetic validation.

- [ ] **Step 1: Write failing tests for default names, one-file selection, comments, ignored differences, and rollback**

```python
def test_default_sources_use_new_case_sensitive_names(self):
    compat, balance = default_source_paths(Path("/Users/test"))
    self.assertEqual(compat.name, "Clash-Compat.yaml")
    self.assertEqual(balance.name, "Clash-Balance.yaml")

def test_balance_sync_writes_dns_with_comments_and_reports_other_paths(self):
    report = run_template_sync(self.root, balance=self.balance_source)
    text = (self.root / "templates/dns/Clash-Balance.yaml").read_text()
    self.assertIn("# balance dns comment", text)
    self.assertIn("proxy-groups", report.ignored_balance_paths)
    self.assertNotIn("private.example", "\n".join(report.lines))

def test_single_compat_input_does_not_touch_balance(self):
    before = self.balance_template.read_bytes()
    run_template_sync(self.root, compat=self.compat_source)
    self.assertEqual(self.balance_template.read_bytes(), before)
```

Retain atomic replacement tests: if serialization, validation, secret scanning, or the second `os.replace` fails, every selected output is restored byte-for-byte with its prior mode.

- [ ] **Step 2: Update CLI tests for `--compat` and `--balance`**

```python
parsed = _parser().parse_args(["template-sync", "--compat", "/tmp/Clash-Compat.yaml"])
self.assertEqual(parsed.compat, Path("/tmp/Clash-Compat.yaml"))
self.assertIsNone(parsed.balance)
```

Verify no-argument mode selects both defaults, while either single flag updates only its own output.

- [ ] **Step 3: Run template/CLI tests and verify they fail on old filenames and Home extraction**

Run: `.venv/bin/python -m unittest tests.test_lightweight_template_sync tests.test_lightweight_cli.TemplateSyncCommandTests -v`

Expected: FAIL on old `Compat-Office.yaml`, `Balance-Office.yaml`, old flags, and `private/home.yaml` output.

- [ ] **Step 4: Reduce `template_sync.py` to the new pipeline**

Set the public paths and modes exactly:

```python
COMPAT_SOURCE_NAME = "Clash-Compat.yaml"
BALANCE_SOURCE_NAME = "Clash-Balance.yaml"
PUBLIC_TEMPLATE_FILES = (
    "templates/base/Clash-Compat.yaml",
    "templates/dns/Clash-Balance.yaml",
    "templates/profiles.yaml",
)
OUTPUT_MODES = {relative: 0o644 for relative in PUBLIC_TEMPLATE_FILES}
```

Keep the proven round-trip, secret scan, snapshot, atomic replacement, and rollback helpers. Remove Home scope bootstrap/derivation and office/universal pairing. Sanitize Compat by removing inline dynamic proxies and the `AmyTelecom` provider while recording injection groups. Extract Balance as a `CommentedMap({"dns": clone_isolated_round_trip(source["dns"])})`, copying the root/key comment slots that belong to `dns`.

Compare sanitized Balance and Compat documents with `dns` removed. Report differing top-level YAML paths only; do not include scalar values. Populate `ignored_balance_paths` and lines such as `Balance 非 DNS 差异（未合并）：proxy-groups, rules`.

- [ ] **Step 5: Run the focused tests**

Run: `.venv/bin/python -m unittest tests.test_lightweight_template_sync tests.test_lightweight_cli.TemplateSyncCommandTests -v`

Expected: PASS.

- [ ] **Step 6: Run template sync against the real iCloud files and inspect only the sanitized diff/report**

Run: `.venv/bin/python bin/clash-sub template-sync`

Expected: report names Compat changes, Balance DNS changes, ignored non-DNS paths, and comment preservation; it never prints source values.

Inspect: `git diff -- templates/base/Clash-Compat.yaml templates/dns/Clash-Balance.yaml templates/profiles.yaml`

Verify the tracked templates contain no private Home node, airport node, owner token, UUID, or provider URL.

- [ ] **Step 7: Commit the template pipeline and new tracked templates**

```bash
git add clash_sub/template_sync.py clash_sub/cli.py templates tests/test_lightweight_template_sync.py tests/test_lightweight_cli.py
git commit -m "feat: sync compat and balance templates"
```

### Task 3: Add the stable atomic airport provider store

**Files:**
- Create: `clash_sub/airport_store.py`
- Create: `tests/test_lightweight_airport_store.py`
- Modify: `clash_sub/sources.py`
- Modify: `tests/test_lightweight_sources.py`

**Interfaces:**
- Produces: `AirportStore(public_root: Path, *, expected_uid: int | None = None, expected_public_gid: int | None = None)`.
- Produces: `AirportStoreError(code: str)` with sanitized stable error codes.
- Produces: `AirportStore.path -> Path`, always `public_root / "provider" / "AmyTelecom.yaml"`.
- Produces: `AirportStore.read() -> bytes` and `AirportStore.replace(document: bytes, validator: Callable[[Path], None]) -> Path`.
- Consumes later: service update/sync and Nginx route generation use the secure stable path.

- [ ] **Step 1: Write failing store tests**

```python
def test_replace_validates_temporary_file_then_atomically_publishes(self):
    seen = []
    path = self.store.replace(PROVIDER_BYTES, lambda candidate: seen.append(candidate.read_bytes()))
    self.assertEqual(seen, [PROVIDER_BYTES])
    self.assertEqual(path.name, "AmyTelecom.yaml")
    self.assertEqual(path.read_bytes(), PROVIDER_BYTES)
    self.assertEqual(path.stat().st_mode & 0o777, 0o640)

def test_validator_failure_keeps_previous_provider(self):
    self.store.replace(OLD_BYTES, lambda _: None)
    with self.assertRaises(AirportStoreError):
        self.store.replace(NEW_BYTES, lambda _: (_ for _ in ()).throw(ValueError()))
    self.assertEqual(self.store.read(), OLD_BYTES)
```

Add cases for symlink target/directory, empty or oversized input, wrong owner/group/mode, hard links, failed `os.replace`, and temporary-file cleanup.

- [ ] **Step 2: Run the new tests and confirm the module is absent**

Run: `.venv/bin/python -m unittest tests.test_lightweight_airport_store -v`

Expected: import failure for `clash_sub.airport_store`.

- [ ] **Step 3: Implement the store with same-directory staging**

The replacement sequence must be:

```python
descriptor, temporary_name = tempfile.mkstemp(
    prefix=".AmyTelecom.", dir=provider_directory
)
os.fchmod(descriptor, 0o640)
os.fchown(descriptor, expected_uid, expected_public_gid)
write_all_and_fsync(descriptor, document)
validator(Path(temporary_name))
os.replace(temporary_name, self.path)
fsync_directory(provider_directory)
```

Wrap all failures in the stable `AirportStoreError("airport_provider_invalid")` or `AirportStoreError("airport_provider_write_failed")` without including paths or document values. Preserve the existing HTTPS/redirect/size checks in `download_airport_document`.

- [ ] **Step 4: Run store and source tests**

Run: `.venv/bin/python -m unittest tests.test_lightweight_airport_store tests.test_lightweight_sources.SourceFetchingTests -v`

Expected: PASS.

- [ ] **Step 5: Commit the store**

```bash
git add clash_sub/airport_store.py clash_sub/sources.py tests/test_lightweight_airport_store.py tests/test_lightweight_sources.py
git commit -m "feat: add atomic airport provider store"
```

### Task 4: Remove airport artifacts from immutable profile releases

**Files:**
- Modify: `clash_sub/domain.py`
- Modify: `clash_sub/release_store.py`
- Modify: `tests/test_lightweight_release_store.py`

**Interfaces:**
- Changes: `PreparedRelease(release_id, public_paths, manifest_path)` has no `airport_path`.
- Changes: `ReleaseStore.prepare(client_id, bundle, input_hashes) -> PreparedRelease | None` has no `airport_document` argument.
- Removes: `AIRPORT_FILENAME` from release store, manifest `airport`, and `read_airport_document()`.
- Consumes: Task 1 `PROFILE_FILENAMES` determines exact artifact names.

- [ ] **Step 1: Replace airport-release tests with exact filename and independence tests**

```python
def test_owner_release_contains_only_two_main_profiles(self):
    release = self.store.prepare(1, OWNER_BUNDLE, {"xui": "1" * 64})
    self.assertEqual(
        {path.name for path in release.public_paths.values()},
        {"Clash-Compat.yaml", "Clash-Balance.yaml"},
    )
    self.assertNotIn("airport", json.loads(release.manifest_path.read_text()))

def test_airport_change_cannot_create_a_main_release(self):
    first = self.store.prepare(1, OWNER_BUNDLE, {"xui": "1" * 64})
    self.store.mark_current(1, first.release_id)
    self.assertIsNone(self.store.prepare(1, OWNER_BUNDLE, {"xui": "1" * 64}))
```

- [ ] **Step 2: Run release tests and verify failures reference legacy embedded airport data**

Run: `.venv/bin/python -m unittest tests.test_lightweight_release_store -v`

Expected: FAIL until the optional airport manifest/file path is removed.

- [ ] **Step 3: Simplify release preparation and verification**

Use `PROFILE_FILENAMES[variant]` in `_filename()`. Delete airport staging, hashes, manifest optional fields, verification, disk-space accounting, and history reads. Keep all existing ownership, hard-link, immutable-directory, hash, activation, pruning, and rollback safeguards for main profiles.

- [ ] **Step 4: Run release tests**

Run: `.venv/bin/python -m unittest tests.test_lightweight_release_store -v`

Expected: PASS.

- [ ] **Step 5: Commit release storage changes**

```bash
git add clash_sub/domain.py clash_sub/release_store.py tests/test_lightweight_release_store.py
git commit -m "refactor: keep airport outside profile releases"
```

### Task 5: Render exact main-profile and owner-only provider routes

**Files:**
- Modify: `clash_sub/nginx.py`
- Modify: `tests/test_lightweight_nginx.py`

**Interfaces:**
- Consumes: `PROFILE_FILENAMES`, `PROFILE_TITLES`, and `AIRPORT_FILENAME` from `domain.py`.
- Produces: owner routes for `Clash-Compat.yaml`, `Clash-Balance.yaml`, and `AmyTelecom.yaml`; member route only for `Clash-Compat.yaml`.
- Preserves: `render_routes(config, state, clients) -> str` and atomic runtime activation interface.

- [ ] **Step 1: Write failing exact-route and authorization tests**

```python
def test_owner_routes_use_exact_case_and_stable_provider(self):
    routes = render_routes(self.config, self.owner_state, self.clients)
    self.assertIn("/s/%s/Clash-Compat.yaml" % OWNER_TOKEN, routes)
    self.assertIn("/s/%s/Clash-Balance.yaml" % OWNER_TOKEN, routes)
    self.assertIn("/s/%s/AmyTelecom.yaml" % OWNER_TOKEN, routes)
    self.assertNotIn("clash-compat-office.yaml", routes)

def test_member_has_no_balance_or_provider_route(self):
    routes = render_routes(self.config, self.member_state, self.clients)
    member_block = routes_for_token(routes, MEMBER_TOKEN)
    self.assertIn("Clash-Compat.yaml", member_block)
    self.assertNotIn("Clash-Balance.yaml", member_block)
    self.assertNotIn("AmyTelecom.yaml", member_block)
```

Create the stable provider fixture at `self.config.public_root / "provider" / "AmyTelecom.yaml"` with `0640`, expected uid/gid, and one link.

- [ ] **Step 2: Run Nginx tests and confirm legacy routes fail**

Run: `.venv/bin/python -m unittest tests.test_lightweight_nginx -v`

Expected: FAIL on lowercase legacy locations and release-local `AmyTelecom.yaml`.

- [ ] **Step 3: Implement stable route resolution**

Build locations using the exact filename rather than the old `"clash-%s.yaml" % variant` pattern:

```python
location = "/s/%s/%s" % (token, PROFILE_FILENAMES[variant])
title = PROFILE_TITLES[variant]
```

Resolve the provider from `public_root / "provider" / "AmyTelecom.yaml"`, requiring its directory/file owner, group, mode `0640`, regular-file status, and link count. Add the provider block only for the current owner. Set `Profile-Title: AmyTelecom` and `Content-Disposition: attachment; filename=AmyTelecom.yaml`.

- [ ] **Step 4: Run Nginx tests**

Run: `.venv/bin/python -m unittest tests.test_lightweight_nginx -v`

Expected: PASS.

- [ ] **Step 5: Commit routes**

```bash
git add clash_sub/nginx.py tests/test_lightweight_nginx.py
git commit -m "feat: publish exact profile and provider routes"
```

### Task 6: Decouple provider update from sync and remove server Home orchestration

**Files:**
- Modify: `clash_sub/service.py`
- Modify: `clash_sub/runtime.py`
- Modify: `clash_sub/cli.py`
- Modify: `tests/test_lightweight_service.py`
- Modify: `tests/test_lightweight_cli.py`
- Modify: `tests/test_lightweight_end_to_end.py`

**Interfaces:**
- Changes constructor: replace `load_home_overlay` with `airport_store`.
- Produces: `ClashSubService.update_airport(url: str) -> dict` that only downloads, validates, and replaces the provider.
- Preserves: `sync_all()`, `rollback()`, `rotate_link()`, `links()`, and state activation semantics.
- Consumes: Tasks 1, 3, 4, and 5 interfaces.

- [ ] **Step 1: Write failing service tests for independent airport update**

```python
def test_update_airport_does_not_read_xui_prepare_release_or_activate(self):
    result = self.service.update_airport("https://airport.example/sub")
    self.assertEqual(result, {"updated": True})
    self.airport_store.replace.assert_called_once()
    self.read_snapshot.assert_not_called()
    self.release_store.prepare.assert_not_called()
    self.activate_runtime.assert_not_called()

def test_sync_requires_current_provider_but_member_output_has_none(self):
    self.airport_store.read.side_effect = AirportStoreError("airport_provider_invalid")
    with self.assertRaisesRegex(ServiceError, "airport_provider_required"):
        self.service.sync_all()
```

Add tests proving provider update failure keeps the old file and emits only `airport_update_failed`; owner rotation regenerates both main profiles with the new token URL; member rotation changes routes without touching provider bytes; rollback never changes provider; and `sync_all` has all-or-nothing activation when any user fails.

- [ ] **Step 2: Update end-to-end and CLI expectations**

Expected links:

```python
owner_urls = (
    "https://sub.example.test:443/s/%s/Clash-Compat.yaml" % OWNER_TOKEN,
    "https://sub.example.test:443/s/%s/Clash-Balance.yaml" % OWNER_TOKEN,
)
member_urls = (
    "https://sub.example.test:443/s/%s/Clash-Compat.yaml" % MEMBER_TOKEN,
)
```

Assert “更新机场订阅” succeeds without changing `state.json`, current release IDs, routes, or release history. Remove all direct Home overwrite acceptance tests.

- [ ] **Step 3: Run service/CLI/end-to-end tests and confirm legacy coupling fails**

Run: `.venv/bin/python -m unittest tests.test_lightweight_service tests.test_lightweight_cli tests.test_lightweight_end_to_end -v`

Expected: FAIL because the old service reads `home.yaml`, embeds airport bytes, and activates an owner release during airport update.

- [ ] **Step 4: Implement the decoupled service**

Use the exact provider URL:

```python
def _provider_url(config, token):
    return "https://%s/s/%s/AmyTelecom.yaml" % (
        config.subscription_authority,
        token,
    )
```

`update_airport()` acquires the existing operation lock, downloads exact bytes, and calls `airport_store.replace(document)`. The store publishes non-empty bounded bytes verbatim without any content validation; it must not reconcile state, fetch x-ui, render profiles, validate with Mihomo, prepare releases, or activate Nginx.

`sync_all()` securely reads the stable provider (existence, safe ownership, non-empty) before preparing any user; its content is never parsed. Owner render receives `AirportProvider(_provider_url(...))`; member render receives `None`. Release input hashes contain `xui` only, so provider-byte changes cannot create a new main release.

Delete `_home_path`, `_optional_home`, `_home_digest`, `_owner_sources`, Home exceptions, Home constructor arguments, and Home-specific Mihomo error mapping. Owner and member main profiles alike are validated by Mihomo exactly as published; the local airport provider file is never handed to Mihomo.

- [ ] **Step 5: Wire `AirportStore` in `runtime.py` and update CLI messages**

Instantiate `AirportStore(config.public_root)` once in `build_service`. Keep the airport menu accepting the source URL interactively, but never echo it. Change owner reinitialization success text to instruct `sync`; do not claim a new airport import is necessary when the stable provider already exists.

- [ ] **Step 6: Run service, CLI, and end-to-end tests**

Run: `.venv/bin/python -m unittest tests.test_lightweight_service tests.test_lightweight_cli tests.test_lightweight_end_to_end -v`

Expected: PASS.

- [ ] **Step 7: Commit orchestration changes**

```bash
git add clash_sub/service.py clash_sub/runtime.py clash_sub/cli.py tests/test_lightweight_service.py tests/test_lightweight_cli.py tests/test_lightweight_end_to_end.py
git commit -m "feat: decouple airport updates from profile sync"
```

### Task 7: Create provider storage during install and reduce backups to rebuild essentials

**Files:**
- Modify: `clash_sub/installer.py`
- Modify: `clash_sub/manage.py`
- Modify: `tests/test_lightweight_installer.py`
- Modify: `tests/test_lightweight_manage.py`
- Modify: `tests/test_lightweight_deployment.py`

**Interfaces:**
- Installer produces `paths.public_root / "provider"` owned `root:www-data`, mode `02750`.
- `create_backup(repo_root, runner) -> Path` archives exactly the x-ui database, two Nginx configs, and runtime `state.json`.

- [ ] **Step 1: Write failing installer and backup tests**

```python
def test_runtime_layout_creates_provider_directory(self):
    self.installer._prepare_runtime_directories()
    provider = self.paths.public_root / "provider"
    self.assertTrue(provider.is_dir())
    self.assertEqual(provider.stat().st_mode & 0o7777, 0o2750)

def test_backup_contains_only_four_rebuild_files(self):
    archive = create_backup(self.repo, self.runner)
    with tarfile.open(archive) as handle:
        self.assertEqual(set(handle.getnames()), {
            "etc/x-ui/x-ui.db",
            "etc/nginx/stream-conf.d/clash-sub.conf",
            "etc/nginx/conf.d/clash-sub.conf",
            "var/lib/clash-sub/private/state.json",
        })
```

Also verify backup fails with a stable `backup_incomplete` error when any required file is missing, rather than silently producing an insufficient rebuild backup.

- [ ] **Step 2: Run installer/manage tests and confirm over-broad backup behavior fails**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer tests.test_lightweight_manage tests.test_lightweight_deployment -v`

Expected: FAIL because routes, private config, runtime releases, status, and a versions manifest are currently archived.

- [ ] **Step 3: Implement the provider directory and exact backup manifest**

Create the provider directory alongside `public_root`, inheriting the resolved `www-data` gid. In `manage.py`, replace recursive discovery with these exact sources:

```python
required = (
    Path("/etc/x-ui/x-ui.db"),
    Path("/etc/nginx/stream-conf.d/clash-sub.conf"),
    Path("/etc/nginx/conf.d/clash-sub.conf"),
    config.private_root / "state.json",
)
```

Do not include `routes.conf`, versions metadata, service config, install state, certificates, provider, releases, status, journals, or locks. Preserve atomic archive creation and final mode `0600`.

- [ ] **Step 4: Run installer/manage/deployment tests**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer tests.test_lightweight_manage tests.test_lightweight_deployment -v`

Expected: PASS.

- [ ] **Step 5: Commit install and backup changes**

```bash
git add clash_sub/installer.py clash_sub/manage.py tests/test_lightweight_installer.py tests/test_lightweight_manage.py tests/test_lightweight_deployment.py
git commit -m "feat: prepare provider storage and minimal backups"
```

### Task 8: Make the local Home script the only Home boundary and update safety checks

**Files:**
- Modify: `private/clash-verge-home.js` (ignored, local-only)
- Modify: `scripts/scan_tracked_secrets.py`
- Modify: `tests/test_repository_safety.py`
- Modify: `tests/test_secret_scan.py`
- Delete locally: `private/home.yaml`
- Delete locally if present: `private/sources/owner/balanced.yaml`
- Delete locally if present: `private/sources/owner/balanced-win.yaml`
- Delete locally if present: `private/sources/owner/privacy.yaml`
- Delete locally: `.DS_Store`, `deploy/.DS_Store`, `docs/.DS_Store`, `generated/.DS_Store`, `private/.DS_Store`, and `templates/.DS_Store`

**Interfaces:**
- Home script `main(config, profileName)` mutates only exact titles `Clash-Compat` and `Clash-Balance`.
- Secret scanner no longer treats `private/home.yaml` as a required or supported boundary.

- [ ] **Step 1: Write failing repository safety tests**

```python
def test_home_script_targets_only_new_titles(self):
    source = (ROOT / "private/clash-verge-home.js").read_text(encoding="utf-8")
    self.assertIn('"Clash-Compat"', source)
    self.assertIn('"Clash-Balance"', source)
    self.assertNotIn("Clash Compat Universal", source)
    self.assertNotIn("Clash Balance Universal", source)

def test_server_home_yaml_is_not_a_runtime_contract(self):
    tracked = "\n".join(path.read_text(errors="ignore") for path in runtime_sources())
    self.assertNotIn("private/home.yaml", tracked)
    self.assertNotIn("HomeOverlay", tracked)
```

Keep a synthetic secret-scan baseline that proves tracked files are scanned, while removing tests that create or parse server `home.yaml`.

- [ ] **Step 2: Run safety tests and confirm old Home references fail**

Run: `.venv/bin/python -m unittest tests.test_repository_safety tests.test_secret_scan -v`

Expected: FAIL on server Home parsing/scanning and old profile titles.

- [ ] **Step 3: Update the local script without exposing its values**

Change only the target-title set to `Clash-Compat` and `Clash-Balance`. Preserve the already user-tested behavior that inserts `HomeServer` and `ProxyServer` immediately after “自动选择”, extends the intended groups, and prepends the Home rules. Validate syntax with `node --check private/clash-verge-home.js`; never print the file.

- [ ] **Step 4: Remove server Home parsing and update scanner expectations**

Delete Home-only definitions/functions/imports from `clash_sub/sources.py` that became unused after Task 6. Remove scanner logic and help text that expect `private/home.yaml`; retain scanning of tracked templates, config examples, fixtures, routes, and tracked source code. Assert `private/**` remains ignored and the local JS is not tracked.

- [ ] **Step 5: Delete the explicitly approved obsolete local files**

Resolve each exact path first, reject symlinks/unexpected directories, then delete only the approved files listed in this task. Remove empty `private/sources/owner/` and `private/sources/` directories after their known files are gone. Do not delete `private/clash-verge-home.js` or `private/config/`.

- [ ] **Step 6: Run syntax and safety tests**

Run: `node --check private/clash-verge-home.js`

Run: `.venv/bin/python -m unittest tests.test_repository_safety tests.test_secret_scan tests.test_lightweight_sources -v`

Expected: PASS, with no private values printed.

- [ ] **Step 7: Commit tracked Home-boundary cleanup**

```bash
git add clash_sub/sources.py scripts/scan_tracked_secrets.py tests/test_repository_safety.py tests/test_secret_scan.py
git commit -m "refactor: move home configuration fully client-side"
```

The ignored local script and deleted ignored private files are intentionally absent from the commit.

### Task 9: Rewrite the four Chinese project manuals and remove legacy project artifacts

**Files:**
- Modify: `README.md`
- Modify: `DEPLOYMENT.md`
- Modify: `docs/template-design.md`
- Modify: `docs/operations.md`
- Delete: `plans/2026-08-30-install-progress-display.md`
- Delete: `plans/` after it becomes empty
- Modify: any remaining active test or example containing a legacy business name

**Interfaces:**
- Project-use docs remain exactly the four files above.
- Development docs remain under `docs/superpowers/specs/` and `docs/superpowers/plans/`.

- [ ] **Step 1: Add failing documentation/residue assertions**

In `tests/test_repository_safety.py`, explicitly define the four project-use docs and separately allow `docs/superpowers/**/*.md`. Search active runtime, templates, tests, and project-use docs for forbidden legacy terms and paths:

```python
LEGACY_BUSINESS_REFERENCES = (
    "compat-office",
    "compat-universal",
    "balance-office",
    "Clash-Compat-Office.yaml",
    "Clash-Compat-Universal.yaml",
    "Clash-Balance-Office.yaml",
    "AmyTelecom.yaml",
    "private/home.yaml",
)
```

Permit those strings only in the historical design/plan documents where removal requirements are being described.

- [ ] **Step 2: Run repository safety tests and capture the legacy file list**

Run: `.venv/bin/python -m unittest tests.test_repository_safety -v`

Expected: FAIL with active legacy references that still need conversion.

- [ ] **Step 3: Rewrite the four manuals in Chinese**

`README.md`: final outputs, owner/member matrix, four-manual navigation, common commands, and incompatible URL cutover.

`DEPLOYMENT.md`: personal deployment prerequisites, Reality public listen before install, loopback closure after successful install prompt, directory permissions, initial airport import, first sync, exact four-file backup, and rebuild recovery order.

`docs/template-design.md`: Compat base, Balance DNS/comments, iCloud one-or-two-file update, ignored-difference report, exact export matrix, stable provider, and full `clash-verge-home.js` maintenance explanation.

`docs/operations.md`: template sync, airport-only update, manual provider refresh, user operations, sync, rollback boundary, Home script editing/test, backup/recovery, and concise troubleshooting.

Do not add tutorials for generic Clash, YAML, Linux, Nginx, or first-time users.

- [ ] **Step 4: Remove the old plan directory and convert all remaining active references**

Delete the superseded `plans/2026-08-30-install-progress-display.md`. Update fixtures, examples, error text, generated URL assertions, and comments to the new exact names. Do not alter the approved design spec or this plan merely because they describe removed names historically.

- [ ] **Step 5: Run repository and documentation tests**

Run: `.venv/bin/python -m unittest tests.test_repository_safety tests.test_lightweight_deployment -v`

Expected: PASS.

- [ ] **Step 6: Commit docs and residue cleanup**

```bash
git add README.md DEPLOYMENT.md docs clash_sub tests config deploy templates plans
git commit -m "docs: document the new subscription workflow"
```

### Task 10: Full verification and final review

**Files:**
- Modify only files required to fix failures caused by Tasks 1–9.

**Interfaces:**
- Verifies every requirement in the spec without adding compatibility behavior.

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test*.py'`

Expected: all tests PASS.

- [ ] **Step 2: Run both secret scans**

Run: `.venv/bin/python scripts/scan_tracked_secrets.py`

Run: `.venv/bin/python scripts/scan_tracked_secrets.py --compare-private private`

Expected: both exit `0` without printing private values.

- [ ] **Step 3: Run syntax and repository hygiene checks**

Run: `.venv/bin/python -m compileall -q clash_sub scripts tests`

Run: `node --check private/clash-verge-home.js`

Run: `git diff --check`

Run: `git status --short`

Expected: no syntax errors, whitespace errors, accidental tracked private files, or unrelated changes.

- [ ] **Step 4: Search for forbidden active legacy references**

Run a scoped `rg` across `clash_sub`, `templates`, `config`, `deploy`, `scripts`, `tests`, and the four project-use docs. Exclude `docs/superpowers/` because the design and plan intentionally record migration history.

Expected: no active office/privacy/Home-server filenames, no retired airport provider filename, and no lowercase legacy subscription URLs.

- [ ] **Step 5: Inspect generated fixtures and routing matrix**

Generate one owner and one member bundle using synthetic tests. Confirm owner has exactly two filenames and provider route, member has exactly one filename and no provider route, Balance differs from Compat only in DNS, and required comments exist in serialized output.

- [ ] **Step 6: Request code review and fix only in-scope findings**

Use `superpowers:requesting-code-review` against the complete diff. Resolve correctness, security, comment-preservation, authorization, and documentation findings; do not add redirects or compatibility aliases.

- [ ] **Step 7: Re-run Steps 1–4 and commit verification fixes if needed**

```bash
git add clash_sub config deploy scripts templates tests README.md DEPLOYMENT.md docs/operations.md docs/template-design.md
git commit -m "fix: complete clash redesign verification"
```

If review produces no changes, do not create an empty commit.
