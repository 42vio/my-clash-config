# Private Home Overlay Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all owner-only home nodes, groups, injection controls, group extensions, and rules into a private six-field overlay that `template-sync` extracts without local Mihomo, then publish it after a direct SFTP overwrite and server-side `clash-sub sync` validation.

**Architecture:** Introduce an immutable `HomeOverlay` value and strict path loader, then compose that overlay only into owner balanced/privacy renders. `template-sync` uses the current private overlay as the ownership scope while splitting the latest downloaded balanced workbench into public templates plus a new private overlay, using structural/variant/leak checks but no local Mihomo. The maintainer directly replaces `/var/lib/clash-sub/private/home.yaml` through SFTP; the existing `sync` command normalizes its mode, validates the resulting owner release with the server Mihomo, and preserves the old release on failure without rolling back the already-overwritten source file.

**Tech Stack:** Python 3.9+, PyYAML, `unittest`, SFTP as an external manual operation, server Mihomo validation, existing Nginx activation journal.

**Spec:** `docs/superpowers/specs/2026-08-28-private-home-overlay-upload-design.md`

## Global Constraints

- Read the spec completely before editing and preserve the approved six-field `HomeOverlay` schema exactly.
- At this plan revision, `DEPLOYMENT.md` has a pre-existing user change and must remain untouched. Before Task 1, record `git status --short`; if any other file in this plan is already dirty and its ownership is not established, stop and ask the user. Never reset, stash, overwrite, or absorb those changes into a task commit. If a dirty file is an uncommitted prerequisite owned by the executing Agent, finish and commit that prerequisite separately before starting this plan.
- Do not print or copy real contents from `private/`, generated subscriptions, tokens, proxy URLs, UUIDs, passwords, REALITY keys, or airport URLs into output, patches, tests, or commits.
- Python remains 3.9+ and dependencies remain `Jinja2==3.1.6` plus `PyYAML==6.0.3`; comment/format round-trip preservation is explicitly out of scope.
- `private/home.yaml` is ignored, current-user-owned `0600` on Mac, and `0600 root:root` on the server. It is never staged or committed.
- Owner balanced/privacy receive home; owner standard and member standard receive no home objects, controls, rules, or names.
- Preserve every valid public-group extension declared by the workbench, including
  `PT站加速 -> ProxyServer` when the workbench contains it.
- Preserve `inject-node-groups: [ProxyServer]` and `inject-home-node-groups: [HomeServer]`.
- Local `template-sync` must run with neither Mihomo nor `MIHOMO_BIN`; real Mihomo validation occurs only inside server `clash-sub sync`.
- The only documented transfer is SFTP from local `private/home.yaml` directly onto `/var/lib/clash-sub/private/home.yaml`, followed by server `clash-sub sync`. Do not add FTP, `scp`, an upload script, inbox, candidate path, or `home-import`.
- Direct overwrite is an accepted risk: validation failure preserves the old published release but leaves the new invalid/truncated official `home.yaml` in place until the maintainer uploads a correction or restores an external backup.
- Follow TDD for every behavior change. Run the named narrow test before and after implementation, then run each full affected module before committing.
- Use `apply_patch` for repository edits. Clean only the legacy top-level private fragments authorized in Task 7 and leave `private/reference-configs/` and unrelated private data untouched.

---

## File Map

**Modify**

- `clash_sub/domain.py` — immutable `HomeOverlay` value.
- `clash_sub/sources.py` — strict six-field path parser, serializer, digest, source alias map, and safe server mode normalization boundary.
- `clash_sub/generator.py` — owner-only overlay composition and runtime injection.
- `clash_sub/template_sync.py` — home-scope extraction and mixed-mode atomic outputs.
- `clash_sub/service.py` — home source loading during existing sync and failure isolation.
- `clash_sub/runtime.py` — inject the six-field home loader into the existing service.
- `scripts/scan_tracked_secrets.py` — include root `private/home.yaml` in private-value comparison.
- `templates/variants/manifest.yaml` — remove tracked home feature declarations.
- `README.md`, `docs/operations.md`, `docs/private-data.md`, and only genuinely affected clean recovery text — direct SFTP workflow and asymmetric source/release failure boundary; preserve dirty `DEPLOYMENT.md` untouched.
- Existing lightweight source, generator, template-sync, service, CLI, end-to-end, deployment, repository-safety, and secret-scan tests.

**Delete**

- `templates/features/home.yaml` after its approved controls have moved into the private overlay.
- After successful private migration only: ignored `private/proxies.yaml`, `private/proxy-groups.yaml`, `private/rules.yaml`, and `private/.DS_Store` when present.

---

### Task 1: Strict HomeOverlay value and source boundary

**Files:**

- Modify: `clash_sub/domain.py:1-83`
- Modify: `clash_sub/sources.py:1-347`
- Test: `tests/test_lightweight_sources.py:285-340`

**Interfaces:**

- Produces: `HomeOverlay(proxies, proxy_groups, extend_proxy_groups, inject_node_groups, inject_home_node_groups, rules)`.
- Produces: `parse_home_overlay(payload: bytes, max_bytes: int) -> HomeOverlay`.
- Produces: `load_home_overlay(path: Path, max_bytes: int) -> HomeOverlay`.
- Produces: `dump_home_overlay(home: HomeOverlay) -> bytes` and `home_overlay_digest(home: HomeOverlay) -> str`.
- Produces: `HomeSourceError.code`, containing only one approved stable `home_*` code.
- Consumed later by generator, template-sync, runtime, and service tasks.

- [ ] **Step 1: Add failing six-field parsing and immutability tests**

Add `HomeOverlaySourceTests` with a synthetic document containing two proxy groups,
sample extension/injection declarations, and the home CIDR rule. Assert exact
tuple/mapping values, defensive copies, deterministic dump/load, and digest stability.

```python
def test_six_field_home_overlay_round_trips_without_mutable_aliases(self):
    payload = home_document_bytes()
    home = parse_home_overlay(payload, len(payload))

    self.assertEqual(home.inject_node_groups, ("ProxyServer",))
    self.assertEqual(home.inject_home_node_groups, ("HomeServer",))
    self.assertEqual(
        dict(home.extend_proxy_groups),
        {"BiliBili": ("ProxyServer",), "国内流媒体": ("ProxyServer",)},
    )
    self.assertEqual(parse_home_overlay(dump_home_overlay(home), 5 * 1024 * 1024), home)
    self.assertEqual(home_overlay_digest(home), home_overlay_digest(home))
```

- [ ] **Step 2: Add failing rejection and secure-path tests**

Cover missing/unknown keys, empty proxies/groups, duplicate names, non-string rules, `MATCH`/`FINAL`, missing extension targets, injection overlap, an injection group absent from `proxy-groups`, malformed UTF-8/YAML, empty/oversized bytes, symlink, hard link, wrong owner, and mode other than `0600`. Assert `HomeSourceError.code`, never its input value.

```python
def test_injection_lists_must_be_disjoint_and_reference_private_groups(self):
    document = home_document()
    document["inject-node-groups"] = ["HomeServer"]
    document["inject-home-node-groups"] = ["HomeServer"]

    with self.assertRaises(HomeSourceError) as caught:
        parse_home_overlay(yaml.safe_dump(document).encode(), 5 * 1024 * 1024)

    self.assertEqual(caught.exception.code, "home_group_reference_invalid")
```

- [ ] **Step 3: Run the new tests and verify the intended failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_sources.HomeOverlaySourceTests -v
```

Expected: FAIL because `HomeOverlay`, `parse_home_overlay`, and related helpers do not exist.

- [ ] **Step 4: Implement the immutable model and parser minimally**

Add the exact value type and copy nested mutable input before freezing public containers.

```python
@dataclass(frozen=True)
class HomeOverlay:
    proxies: tuple[Mapping, ...]
    proxy_groups: tuple[Mapping, ...]
    extend_proxy_groups: Mapping[str, tuple[str, ...]]
    inject_node_groups: tuple[str, ...]
    inject_home_node_groups: tuple[str, ...]
    rules: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "proxies", tuple(copy.deepcopy(dict(item)) for item in self.proxies))
        object.__setattr__(self, "proxy_groups", tuple(copy.deepcopy(dict(item)) for item in self.proxy_groups))
        object.__setattr__(
            self,
            "extend_proxy_groups",
            MappingProxyType({key: tuple(value) for key, value in self.extend_proxy_groups.items()}),
        )
```

In `sources.py`, require the exact key set and use separate validation helpers so every failure maps to one approved code. `dump_home_overlay` must use `yaml.safe_dump(document, allow_unicode=True, sort_keys=False)` and end with one newline. Existing airport/xui functions keep their current generic errors.

- [ ] **Step 5: Run source tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_sources.HomeOverlaySourceTests -v
.venv/bin/python -m unittest tests.test_lightweight_sources -v
```

Expected: PASS.

- [ ] **Step 6: Commit the source boundary**

```bash
git add clash_sub/domain.py clash_sub/sources.py tests/test_lightweight_sources.py
git commit -m "feat: define private home overlay source"
```

---

### Task 2: Preserve source-specific names and compose the private overlay

**Files:**

- Modify: `clash_sub/sources.py:122-159`
- Modify: `clash_sub/generator.py:1-310`
- Modify: `templates/variants/manifest.yaml`
- Delete: `templates/features/home.yaml`
- Test: `tests/test_lightweight_sources.py`
- Test: `tests/test_lightweight_generator.py:130-470`

**Interfaces:**

- Consumes: `HomeOverlay` from Task 1.
- Produces: `merge_proxy_sources_with_aliases(labeled_sources) -> tuple[list[dict], dict[str, dict[str, str]]]`.
- Preserves: `merge_proxy_sources(labeled_sources) -> list[dict]` as a compatibility wrapper for existing callers.
- Produces: `render_user_bundle(is_owner, xui, airport, home: Optional[HomeOverlay], template_root) -> Mapping[str, str]`.
- Produces internal `_apply_home_overlay(document, home, source_aliases, provider_name)`.

- [ ] **Step 1: Add failing alias-map tests**

```python
def test_merge_returns_home_aliases_for_collision_rewrites(self):
    merged, aliases = merge_proxy_sources_with_aliases(
        (("3x-ui", [proxy("Duplicate")]), ("home", [proxy("Duplicate")]))
    )

    self.assertEqual([item["name"] for item in merged], ["Duplicate [3x-ui]", "Duplicate [home]"])
    self.assertEqual(aliases["home"], {"Duplicate": "Duplicate [home]"})
```

Run the one test and expect an import failure for the new function.

- [ ] **Step 2: Implement alias-preserving merge**

Refactor the existing merge loop once. Record each source label's original-to-final mapping after collision resolution, then keep the old function as:

```python
def merge_proxy_sources(labeled_sources):
    merged, _aliases = merge_proxy_sources_with_aliases(labeled_sources)
    return merged
```

Reject duplicate proxy names inside one home source before an ambiguous alias map can be created.

- [ ] **Step 3: Replace feature-based generator tests with HomeOverlay tests**

Create a `home_overlay()` fixture and update every render call to pass `HomeOverlay` or `None`, not a bare proxy list. Assert:

- balanced/privacy contain `HomeServer`, `ProxyServer`, home CIDR rule, home nodes,
  and every extension declared by the private overlay;
- `ProxyServer` receives owner 3x-ui + renamed home nodes and `use: [AmyTelecom]`;
- `HomeServer` receives only renamed home nodes and never provider `use`;
- both standard profiles contain no home names, rules, controls, or extensions;
- invalid/overlapping injection controls, missing extension targets, duplicate group names, and final rules fail closed.
- home-originated composition failures raise `HomeSourceError` with the exact applicable `home_group_reference_invalid`, `home_extension_invalid`, or `home_rule_invalid` code so server sync can report a useful sanitized error.

```python
def test_private_home_overlay_has_fixed_variant_authorization(self):
    bundle = render_user_bundle(True, [reality_proxy("Owner")], provider(), home_overlay(), TEMPLATE_ROOT)
    balanced = yaml.safe_load(bundle["balanced"])
    groups = {item["name"]: item for item in balanced["proxy-groups"]}

    self.assertEqual(groups["ProxyServer"]["proxies"], ["🎯 Direct", "HomeServer", "Owner", "Home"])
    self.assertEqual(groups["ProxyServer"]["use"], ["AmyTelecom"])
    self.assertEqual(groups["HomeServer"]["proxies"], ["🎯 Direct", "Home"])
    self.assertIn("ProxyServer", groups["BiliBili"]["proxies"])
```

- [ ] **Step 4: Run generator tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_generator -v
```

Expected: FAIL because generator still accepts a proxy list and reads the tracked home feature.

- [ ] **Step 5: Implement overlay composition and simplify the manifest**

Change source authorization to carry an overlay only for owner balanced/privacy. Copy private groups, rewrite explicit home proxy members through `source_aliases["home"]`, add every declared extension, prepend private rules, then apply the two injection lists. Keep public manifest injection for common groups and keep privacy DNS override; remove the `features` key and all tracked feature loading/application code.

The resulting manifest shape is exactly:

```yaml
variants:
  balanced:
    overrides: []
  standard:
    overrides: []
  privacy:
    overrides:
      - privacy-dns
inject-node-groups:
  - 加速线路
  - AI服务
```

Delete `templates/features/home.yaml` only after the new tests exercise every migrated behavior.

- [ ] **Step 6: Run affected tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_sources -v
.venv/bin/python -m unittest tests.test_lightweight_generator -v
.venv/bin/python -m unittest tests.test_lightweight_checks -v
```

Expected: PASS.

- [ ] **Step 7: Commit generator composition**

```bash
git add clash_sub/sources.py clash_sub/generator.py templates/variants/manifest.yaml tests/test_lightweight_sources.py tests/test_lightweight_generator.py tests/test_lightweight_checks.py
git add -u templates/features/home.yaml
git commit -m "feat: compose private home overlay"
```

---

### Task 3: Make template-sync emit public templates and private home together

**Files:**

- Modify: `clash_sub/template_sync.py:31-681`
- Modify: `tests/test_lightweight_template_sync.py`
- Modify: `tests/test_lightweight_cli.py`

**Interfaces:**

- Consumes: `load_home_overlay`, `dump_home_overlay`, and new generator API.
- Produces: `run_template_sync(repo_root: Path) -> {"changed": ("templates/clash.yaml", "templates/variants/manifest.yaml", "private/home.yaml")}` with no Mihomo argument or environment lookup.
- Produces internal `_split_workbench(root, workbench, home_scope) -> tuple[dict, dict, HomeOverlay]`.
- Produces internal mixed-mode writer using `0644` for tracked outputs and `0600` for `private/home.yaml`.

- [ ] **Step 1: Rewrite fixtures around an existing private scope**

Update each temporary repository fixture to write `private/home.yaml` mode `0600`. Construct the workbench by rendering a synthetic owner balanced profile with one owner xui proxy, one home proxy, and the synthetic Amy provider. Update `PUBLIC_TEMPLATE_FILES` to exclude the deleted feature and add a separate private output snapshot.

- [ ] **Step 2: Add failing extraction tests**

Cover:

- home group names come only from the existing private scope;
- home proxies are collected only from `inject-home-node-groups`, never from `ProxyServer` all-node members;
- copied private groups have runtime inline names and provider `use` removed;
- all declared extensions and both injection lists are exported;
- the home-target rule moves to private rules and stays before public rules at render time;
- missing scope, insecure scope, missing declared group, and leaked private values preserve every previous byte and mode;
- an undeclared new group is treated as public, never receives home nodes, and must still pass the public-candidate and private-leak checks;
- a write failure at each of the three targets restores all previous outputs;
- success reports the three exact paths and keeps private output `0600`.

```python
def test_template_sync_exports_home_without_xui_or_provider_members(self):
    result = run_template_sync(self.root)
    home = load_home_overlay(self.root / "private" / "home.yaml", 5 * 1024 * 1024)

    self.assertEqual(result["changed"], TEMPLATE_OUTPUT_PATHS)
    self.assertEqual([item["name"] for item in home.proxies], ["Home"])
    proxy_server = next(item for item in home.proxy_groups if item["name"] == "ProxyServer")
    self.assertEqual(proxy_server["proxies"], ["🎯 Direct", "HomeServer"])
    self.assertNotIn("use", proxy_server)
```

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_template_sync -v
```

Expected: FAIL on old feature outputs and missing private-scope extraction.

- [ ] **Step 4: Implement deterministic split and mixed-mode rollback**

Replace `TEMPLATE_RELATIVE_PATHS` with:

```python
OUTPUT_MODES = {
    "templates/clash.yaml": 0o644,
    "templates/variants/manifest.yaml": 0o644,
    "private/home.yaml": 0o600,
}
TEMPLATE_OUTPUT_PATHS = tuple(OUTPUT_MODES)
```

Use the existing home group/injection declarations as scope. Before building the private candidate, record all inline proxy names, collect only members of `inject-home-node-groups`, and strip every runtime inline member plus `use: AmyTelecom` from copied home groups. Derive and preserve extensions from remaining public groups without special-casing their names, and classify rules by parsed policy target.

Update candidate validation to render all four authorization cases with synthetic sources and pass every rendered string through `validate_clash`. Remove `_resolve_mihomo`, `MihomoValidator`, `MIHOMO_BIN`, runner plumbing used only for Mihomo, temporary Mihomo files, and `mihomo_validation_failed` from this local command. Expand forbidden-name/value checks to include the candidate home proxy/group names and complete private rules. The replacement loop snapshots bytes and modes for all three outputs and restores all attempted paths after any error.

- [ ] **Step 5: Update CLI template-sync output tests**

Assert the command succeeds when `MIHOMO_BIN` is absent, prints all three changed paths, never prints file contents, and retains the existing final prompt. Keep `template-sync` absent from the server menu.

- [ ] **Step 6: Run template and CLI tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_template_sync -v
.venv/bin/python -m unittest tests.test_lightweight_cli -v
```

Expected: PASS.

- [ ] **Step 7: Commit dual-output template synchronization**

```bash
git add clash_sub/template_sync.py tests/test_lightweight_template_sync.py tests/test_lightweight_cli.py
git commit -m "feat: export private home from template sync"
```

---

### Task 4: Load the directly overwritten server home safely during sync

**Files:**

- Modify: `clash_sub/sources.py`
- Modify: `clash_sub/service.py:69-300`
- Modify: `clash_sub/runtime.py:1-76`
- Modify: `tests/test_lightweight_service.py`
- Modify: `tests/test_lightweight_sources.py`

**Interfaces:**

- Consumes: `load_home_overlay`, `home_overlay_digest`, and the generator API.
- Produces: `normalize_server_home(path: Path, max_bytes: int) -> HomeOverlay`, which accepts only a root-owned regular single-link file under the configured root, changes a safe file to `0600`, rechecks metadata, then parses it.
- Changes: `ClashSubService._optional_home() -> Optional[HomeOverlay]` and `_owner_sources(state: RuntimeState) -> tuple[Optional[bytes], Optional[HomeOverlay], Optional[str]]` where the last item is a sanitized owner error code.
- Preserves: `ClashSubService.sync_all() -> {"updated": tuple, "errors": tuple}` and the existing release activation transaction; the official source path is deliberately not an activation artifact.

- [ ] **Step 1: Add failing server file-normalization tests**

In `HomeOverlaySourceTests`, create a root-owned synthetic file under a `0700` private root. Assert a safe `0644` regular single-link file becomes `0600` and parses. Assert symlink, hard link, non-root owner (mock metadata), directory, empty file, and oversized file fail without changing content or emitting a value.

```python
def test_server_home_normalizes_safe_sftp_mode_before_parsing(self):
    path = self.write_home(mode=0o644)

    home = normalize_server_home(path, 5 * 1024 * 1024)

    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
    self.assertEqual(home.inject_node_groups, ("ProxyServer",))
```

- [ ] **Step 2: Add failing sync success and source-persistence tests**

Replace proxy-list service fixtures with `HomeOverlay`. Bootstrap an existing owner release, overwrite the fixture's official `home.yaml` bytes exactly as SFTP would, then call `sync_all`. Assert the source bytes remain the uploaded bytes, its mode becomes `0600`, owner balanced/privacy publish the new home, and both standard profiles remain free of home names.

```python
def test_sync_publishes_directly_overwritten_home_after_server_validation(self):
    previous = self.current_owner_release()
    uploaded = home_document_bytes(node_name="Home New")
    self.home_path.write_bytes(uploaded)
    self.home_path.chmod(0o644)

    result = self.service.sync_all()

    self.assertFalse(result["errors"])
    self.assertNotEqual(self.current_owner_release(), previous)
    self.assertEqual(self.home_path.read_bytes(), uploaded)
    self.assertEqual(stat.S_IMODE(self.home_path.stat().st_mode), 0o600)
```

- [ ] **Step 3: Add failing invalid-source isolation tests**

For truncated YAML, invalid schema, unsafe metadata, and an invalid home reference, record the uploaded source bytes plus the current owner release/state/current/routes. After `sync_all`, assert the source remains the bad uploaded version while the entire active owner view stays equal. Assert enabled members can still sync and the owner error contains only the stable `home_*` code.

```python
def test_bad_overwritten_home_stays_for_debug_while_old_release_stays_live(self):
    before = self.active_owner_view()
    uploaded = b"proxy-groups: ["
    self.home_path.write_bytes(uploaded)

    result = self.service.sync_all()

    self.assertEqual(self.home_path.read_bytes(), uploaded)
    self.assertEqual(self.active_owner_view(), before)
    self.assertEqual(result["errors"], ({"client_id": 7, "code": "home_yaml_invalid"},))
```

- [ ] **Step 4: Run focused tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_sources.HomeOverlaySourceTests -v
.venv/bin/python -m unittest tests.test_lightweight_service -v
```

Expected: FAIL because the current home loader accepts only a proxy list, rejects `0644`, and collapses source errors into `owner_update_failed`.

- [ ] **Step 5: Implement the minimal direct-source boundary**

Implement `normalize_server_home` with `path.parent.lstat()`, `path.lstat()`, `os.open(path, os.O_RDONLY | os.O_NOFOLLOW)`, descriptor `fstat`, `fchmod(0o600)`, bounded descriptor read, and a final descriptor/path identity recheck before parsing. Require a `0700` expected-owner parent and a regular single-link file; close the descriptor on every path. Never rename, copy, delete, back up, or journal source bytes. Keep `load_home_overlay` strict and non-mutating for the Mac workbench boundary.

Inject `normalize_server_home` into `ClashSubService`. `_optional_home` treats genuine absence as `None`; a present bad file raises `HomeSourceError`. `_owner_sources` preserves `HomeSourceError.code`, while missing airport/current owner data stays `owner_update_failed`. In the per-client preparation loop, catch `HomeSourceError` before the generic exception branch, record its code for the owner, discard that owner's candidate, and continue members. Successful owner generation continues through `_prepare`, server `MihomoValidator`, and the existing activation transaction.

```python
def _optional_home(self):
    path = _home_path(self.config)
    if not path.exists() and not path.is_symlink():
        return None
    return self._load_home(path, self.config.max_source_bytes)

def _owner_sources(self, state):
    try:
        home = self._optional_home()
    except HomeSourceError as error:
        return None, None, error.code
    identity = state.users.get(state.owner_client_id)
    if not identity or not identity.current_release:
        return None, home, "owner_update_failed"
    airport = self._releases.read_airport_document(
        state.owner_client_id, identity.current_release
    )
    if airport is None:
        return None, home, "owner_update_failed"
    return airport, home, None
```

Update normal sync, airport update, and owner token rotation to pass `Optional[HomeOverlay]`; do not add the home path to `activation_paths` or `extra_replacements`, because SFTP already replaced it outside the transaction.

- [ ] **Step 6: Run source and service regression tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_sources -v
.venv/bin/python -m unittest tests.test_lightweight_service -v
```

Expected: PASS.

- [ ] **Step 7: Commit server source loading**

```bash
git add clash_sub/sources.py clash_sub/service.py clash_sub/runtime.py tests/test_lightweight_sources.py tests/test_lightweight_service.py
git commit -m "feat: validate directly uploaded home during sync"
```

---

### Task 5: Prove the direct-SFTP failure model end to end

**Files:**

- Modify: `tests/test_lightweight_cli.py`
- Modify: `tests/test_lightweight_end_to_end.py`
- Modify: `tests/test_repository_safety.py`
- Modify production only if a failing integration test proves Task 4 incomplete: `clash_sub/service.py`, `clash_sub/runtime.py`, `clash_sub/cli.py`

**Interfaces:**

- Consumes: existing `ClashSubService.sync_all()` and CLI `clash-sub sync`.
- Preserves: CLI exit `0` only when `result["errors"]` is empty; any owner `home_*` error produces exit `1` and the existing sanitized client-id/error-code line.
- Prohibits: `home-import`, upload-target arguments, stdin upload handling, `home_upload.py`, `upload-home.sh`, and any new menu item.

- [ ] **Step 1: Add CLI regression tests for the existing sync command**

Make `FakeService.sync_all` return one `home_yaml_invalid` owner error. Assert exit `1`, the existing partial-completion message, and one sanitized error line; assert no proxy/group/file contents appear. Also assert `home-import` remains `invalid_command` and no menu contains a home upload item.

```python
def test_sync_reports_home_error_without_source_details(self):
    self.service.sync_result = {
        "updated": (),
        "errors": ({"client_id": 7, "code": "home_yaml_invalid"},),
    }

    code, stdout, stderr = run_cli(["sync"], self.service)

    self.assertEqual(code, 1)
    self.assertEqual(stdout, "同步部分完成。\n")
    self.assertEqual(stderr, "客户端 ID 7（错误代码：home_yaml_invalid）\n")
```

- [ ] **Step 2: Add end-to-end SFTP-overwrite simulations**

Extend the existing harness to write bytes directly to `config.private_root / "home.yaml"` before invoking the real service. Cover success, malformed YAML, invalid reference, `0644` normalization, Mihomo rejection, Nginx rejection, and a truncated/empty file. On each failure compare release manifest, current symlink/marker, state, routes, and public bytes to the pre-upload snapshot while separately asserting the official home source still contains the uploaded bad bytes.

```python
def test_mihomo_rejects_uploaded_home_but_source_is_not_rolled_back(self):
    before = self.harness.active_owner_view()
    uploaded = home_document_bytes(node_name="Mihomo Reject")
    self.harness.home_path.write_bytes(uploaded)
    self.harness.runner.fail_mihomo = True

    result = self.harness.service.sync_all()

    self.assertEqual(self.harness.home_path.read_bytes(), uploaded)
    self.assertEqual(self.harness.active_owner_view(), before)
    self.assertEqual(result["errors"][0]["code"], "home_mihomo_validation_failed")
```

- [ ] **Step 3: Run the focused integration tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_cli -v
.venv/bin/python -m unittest tests.test_lightweight_end_to_end -v
.venv/bin/python -m unittest tests.test_repository_safety -v
```

Expected before completing the boundary: FAIL where home errors are generic or source bytes are incorrectly expected to roll back.

- [ ] **Step 4: Make only integration-proven corrections**

Keep CLI command registration unchanged. If Task 4 currently maps a home parse/reference failure or owner Mihomo failure to generic `owner_update_failed`, add only the narrow `HomeSourceError`/`CheckError` mapping needed to preserve the appropriate `home_*` code. Keep Nginx/routes/current switching failures on the existing global `sync_activation_failed` contract. Never include exception text. Do not add backup, rename, deletion, inbox, or source restoration logic.

- [ ] **Step 5: Run CLI, end-to-end, Nginx, release, and safety tests**

```bash
.venv/bin/python -m unittest tests.test_lightweight_cli -v
.venv/bin/python -m unittest tests.test_lightweight_end_to_end -v
.venv/bin/python -m unittest tests.test_lightweight_nginx -v
.venv/bin/python -m unittest tests.test_lightweight_release_store -v
.venv/bin/python -m unittest tests.test_repository_safety -v
```

Expected: PASS.

- [ ] **Step 6: Commit direct-overwrite integration coverage**

```bash
git add tests/test_lightweight_cli.py tests/test_lightweight_end_to_end.py tests/test_repository_safety.py
git add clash_sub/service.py clash_sub/runtime.py clash_sub/cli.py
git commit -m "test: prove direct home overwrite safety boundary"
```

Stage a production file only if Step 4 required a tested correction; verify `git diff --cached --name-only` before committing.

---

### Task 6: Extend private-value scanning without leaking home contents

**Files:**

- Modify: `scripts/scan_tracked_secrets.py:1-430`
- Modify: `tests/test_secret_scan.py`
- Modify: `tests/test_repository_safety.py`

**Interfaces:**

- Consumes: ignored `private/home.yaml` only when `--private-root private` is supplied.
- Produces: the existing scanner exit code and category/path-only output contract.

- [ ] **Step 1: Add failing home scalar leak tests**

Create a synthetic ignored root-level home file containing proxy credentials, group names, and a private rule. Put one value at a time into a tracked fixture and assert the scanner finds it without printing the matched value. Also assert malformed/missing home stays secret-safe and ordinary public extension targets do not create false positives.

```python
def test_root_home_values_are_compared_without_echoing_them(self):
    secret = "home-private-password-0123456789"
    write_home(self.private_root, password=secret)
    tracked = self.root / "tracked.txt"
    tracked.write_text(secret, encoding="utf-8")

    result = run_scanner(self.root, self.private_root)

    self.assertNotEqual(result.returncode, 0)
    self.assertIn("private-value-leak: tracked.txt", result.stdout)
    self.assertNotIn(secret, result.stdout + result.stderr)
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_secret_scan -v
```

Expected: FAIL because root `private/home.yaml` is not currently scanned.

- [ ] **Step 3: Implement a fixed root-home scan**

Keep existing private directory scans and add the exact file `private_root / "home.yaml"`. Never recurse arbitrary new roots, follow links, or emit scalar values. Reuse YAML scalar extraction and credential-key logic; include home proxy/group names and complete private rules in exact tracked-value comparison where they are not already public values.

- [ ] **Step 4: Run scanner tests and both real scans**

Run:

```bash
.venv/bin/python -m unittest tests.test_secret_scan -v
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
```

Expected: all exit 0 and print no private values.

- [ ] **Step 5: Commit scanner coverage**

```bash
git add scripts/scan_tracked_secrets.py tests/test_secret_scan.py tests/test_repository_safety.py
git commit -m "test: scan private home values for leaks"
```

---

### Task 7: Migrate the ignored private source safely

**Files:**

- Create locally but never stage: `private/home.yaml`
- Delete locally after verification: `private/proxies.yaml`, `private/proxy-groups.yaml`, `private/rules.yaml`, `private/.DS_Store` when present
- Preserve: `private/workbench/balanced.yaml`, `private/reference-configs/`, `private/config/`, and every unrelated private path

**Interfaces:**

- Consumes: the latest downloaded/tested workbench and the legacy ignored fragments.
- Produces: one real six-field `private/home.yaml`, current-user-owned mode `0600`.
- Consumed by: the real `template-sync` smoke test and later manual SFTP overwrite.

- [ ] **Step 1: Inventory names and modes without printing content**

Run only metadata checks such as `find private -maxdepth 2 -type f -print` and `stat`; do not run `cat`, YAML dumps, diffs, or commands that echo scalar values. Confirm the three authorized legacy fragments and latest workbench exist.

- [ ] **Step 2: Build the real private overlay in a secret-safe local editor**

Create the exact six keys. Copy the real home proxy objects from `private/proxies.yaml`; copy only `HomeServer` and `ProxyServer` private groups from the existing private/workbench data, removing runtime 3x-ui proxy members and `use: AmyTelecom`. Set controls exactly to:

```yaml
extend-proxy-groups:
  BiliBili:
    - ProxyServer
  国内流媒体:
    - ProxyServer
inject-node-groups:
  - ProxyServer
inject-home-node-groups:
  - HomeServer
rules:
  - IP-CIDR,192.168.2.0/24,HomeServer,no-resolve
```

Do not copy redundant public rules. Preserve any valid public-group extension present in the source workbench.

- [ ] **Step 3: Secure and validate without displaying the file**

```bash
chmod 600 private/home.yaml
env -u MIHOMO_BIN ./bin/clash-sub template-sync
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
git status --short --ignored
```

Expected: template sync lists only its three paths without locating or running Mihomo, scan exits 0, and `private/home.yaml` remains ignored.

- [ ] **Step 4: Verify the generated private file structurally without echoing values**

Use the repository loader in a command that prints only counts and booleans, never names or fields. Confirm nonzero proxy/group counts, the expected extension count from the source workbench, exactly one all-node injection, exactly one home-only injection, exactly one home rule, and mode `0600`.

- [ ] **Step 5: Remove only authorized legacy fragments**

After all Task 7 validation succeeds, remove the four authorized old paths when present. Report what was removed and that the files are not recoverable except from the retained workbench/reference copies or external backup. Do not remove sources/config/reference directories.

- [ ] **Step 6: Confirm no private data is staged**

```bash
git diff --cached --name-only
git status --short --ignored
```

Expected: no `private/` path is staged or tracked. This task has no Git commit because its outputs are intentionally ignored.

---

### Task 8: Update operational documentation and deployment assertions

**Files:**

- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/private-data.md`
- Preserve untouched: `DEPLOYMENT.md`
- Modify only if a design-required recovery sentence exists: `docs/recovery.md`
- Modify: `tests/test_lightweight_deployment.py`

**Interfaces:**

- Documents one local command, one manual SFTP overwrite, and one existing server command.
- Keeps server menu/CLI structure unchanged and explicitly excludes `home-import`, upload scripts, inbox, FTP, and `scp`.

- [ ] **Step 1: Add failing documentation assertions**

Assert current docs contain all three fixed workflow elements:

```text
./bin/clash-sub template-sync
private/home.yaml → /var/lib/clash-sub/private/home.yaml
clash-sub sync
```

Assert they describe the rolling latest balanced workbench, six private fields, owner variant isolation, server-only Mihomo validation, backup inclusion, and the asymmetric failure rule: old release stays live but overwritten source does not roll back. Assert current operational sections do not advertise local Mihomo, `MIHOMO_BIN`, FTP, `scp`, inbox, upload scripts, alternative remote paths, `home-import`, or `templates/features/home.yaml`.

- [ ] **Step 2: Run documentation tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_deployment -v
```

Expected: FAIL on the old home feature and old local workflow text.

- [ ] **Step 3: Rewrite only affected documentation sections**

Document this exact workflow:

1. Directly download the latest server `clash-balanced.yaml` and save it as `private/workbench/balanced.yaml`.
2. Modify and test that rolling local working copy.
3. Run repository-local `./bin/clash-sub template-sync` without local Mihomo.
4. Review only tracked public diffs and run tests/scans.
5. If tracked templates changed, commit/push them and run server `clash-sub update` only; do not use the combined update+sync path before the matching home is present.
6. Use SFTP to overwrite `private/home.yaml` onto `/var/lib/clash-sub/private/home.yaml`.
7. Run `clash-sub sync` on the server; this is the separate validation/publication step.

State that SFTP interruption or bad YAML can leave the formal source invalid while the old release remains online, and that recovery requires a corrected upload or external backup. Recommend SFTP, never plaintext FTP. Do not change current YAML comment/format behavior or add a preservation claim. Preserve unrelated dirty documentation text line-by-line.

- [ ] **Step 4: Run documentation and safety tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_deployment -v
.venv/bin/python -m unittest tests.test_repository_safety -v
```

Expected: PASS.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md docs/operations.md docs/private-data.md tests/test_lightweight_deployment.py
git diff --cached --check
git commit -m "docs: document private home workflow"
```

Do not edit or stage `DEPLOYMENT.md`. If a design-required sentence must change in `docs/recovery.md`, first verify that file was clean at the Task 1 boundary and stage it only after reviewing its complete diff.

---

### Task 9: Full verification and integration handoff

**Files:**

- Verify all changed production, test, template, script, and documentation files.
- Do not modify unrelated files to force a green result.

**Interfaces:**

- Produces a clean, evidence-backed implementation report and commit list.

- [ ] **Step 1: Run every focused module once more**

```bash
.venv/bin/python -m unittest tests.test_lightweight_sources -v
.venv/bin/python -m unittest tests.test_lightweight_generator -v
.venv/bin/python -m unittest tests.test_lightweight_template_sync -v
.venv/bin/python -m unittest tests.test_lightweight_service -v
.venv/bin/python -m unittest tests.test_lightweight_cli -v
.venv/bin/python -m unittest tests.test_lightweight_end_to_end -v
.venv/bin/python -m unittest tests.test_secret_scan -v
.venv/bin/python -m unittest tests.test_repository_safety -v
.venv/bin/python -m unittest tests.test_lightweight_deployment -v
```

Expected: PASS, with only existing explicitly documented skips.

- [ ] **Step 2: Run the complete suite**

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: PASS. If a failure also reproduces on the exact pre-feature baseline, report it separately with the baseline command/output and do not change unrelated code.

- [ ] **Step 3: Run both leak scans and diff checks**

```bash
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
git diff --check
git status --short
```

Expected: both scans exit 0; no private path is tracked; remaining dirty files are either this plan's intentional changes or clearly identified pre-existing user/Agent changes.

- [ ] **Step 4: Run the real local no-Mihomo template-sync smoke validation**

```bash
env -u MIHOMO_BIN ./bin/clash-sub template-sync
```

Expected: no Mihomo lookup or subprocess call; exactly `templates/clash.yaml`, `templates/variants/manifest.yaml`, and `private/home.yaml`, followed by the existing review/test reminder. Re-run relevant tests if this modifies tracked template bytes.

- [ ] **Step 5: Review commits and request code review**

```bash
git log --oneline --decorate -12
git diff 7270ad8...HEAD --stat -- . ':(exclude)docs/superpowers/specs/2026-08-28-private-home-overlay-upload-design.md' ':(exclude)docs/superpowers/plans/2026-08-28-private-home-overlay-upload.md'
git diff 7270ad8...HEAD --check -- . ':(exclude)docs/superpowers/specs/2026-08-28-private-home-overlay-upload-design.md' ':(exclude)docs/superpowers/plans/2026-08-28-private-home-overlay-upload.md'
```

Use the `superpowers:requesting-code-review` skill. The reviewer must compare the implementation against the spec, verify owner/member isolation, confirm the PT extension removal, and inspect the deliberate boundary where activation rollback protects the published release but never restores the SFTP-overwritten source.

- [ ] **Step 6: Final report**

Report exact test totals, skips, any baseline-only failures, commit hashes, and the ignored private migration result without values. State that no live SFTP overwrite or server sync was performed; do not mutate a real server unless the user separately authorizes it.
