# Private Home Overlay Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all owner-only home nodes, groups, injection controls, group extensions, and rules into a private six-field overlay that `template-sync` extracts locally and one upload script validates and activates transactionally on the server.

**Architecture:** Introduce an immutable `HomeOverlay` value and strict path/bytes loaders, then compose that overlay only into owner balanced/privacy renders. `template-sync` uses the current private overlay as the ownership scope while splitting the latest downloaded balanced workbench into public templates plus a new private overlay. A local wrapper streams the overlay to a root-only `home-import` command; the existing activation journal switches the source file and owner release together.

**Tech Stack:** Python 3.9+, PyYAML, `unittest`, POSIX shell, OpenSSH, Mihomo validation, existing Nginx activation journal.

**Spec:** `docs/superpowers/specs/2026-08-28-private-home-overlay-upload-design.md`

## Global Constraints

- Read the spec completely before editing and preserve the approved six-field `HomeOverlay` schema exactly.
- This work starts on a dirty tree containing overlapping AmyTelecom changes. Before Task 1, record `git status --short`; if any file in this plan is already dirty and its ownership is not established, stop and ask the user. Never reset, stash, overwrite, or absorb those changes into a task commit. If they are an uncommitted prerequisite owned by the executing Agent, finish and commit that prerequisite separately before starting this plan.
- Do not print or copy real contents from `private/`, generated subscriptions, tokens, proxy URLs, UUIDs, passwords, REALITY keys, or airport URLs into output, patches, tests, or commits.
- Python remains 3.9+ and dependencies remain `Jinja2==3.1.6` plus `PyYAML==6.0.3`; comment/format round-trip preservation is explicitly out of scope.
- `private/home.yaml` is ignored, current-user-owned `0600` on Mac, and `0600 root:root` on the server. It is never staged or committed.
- Owner balanced/privacy receive home; owner standard and member standard receive no home objects, controls, rules, or names.
- Preserve `BiliBili -> ProxyServer` and `国内流媒体 -> ProxyServer`; deliberately remove `PT站加速 -> ProxyServer`.
- Preserve `inject-node-groups: [ProxyServer]` and `inject-home-node-groups: [HomeServer]`.
- The only documented upload command is `./scripts/upload-home.sh root@<server>`; do not document SFTP, FTP, `scp`, inbox, or a server directory.
- Follow TDD for every behavior change. Run the named narrow test before and after implementation, then run each full affected module before committing.
- Use `apply_patch` for repository edits. Clean only the legacy top-level private fragments authorized in Task 7 and leave `private/reference-configs/` and unrelated private data untouched.

---

## File Map

**Create**

- `clash_sub/home_upload.py` — local SSH upload orchestration without shell interpolation.
- `scripts/upload-home.sh` — executable repository-relative entry point.
- `tests/test_home_upload.py` — local target/file/SSH boundary tests.

**Modify**

- `clash_sub/domain.py` — immutable `HomeOverlay` value.
- `clash_sub/sources.py` — strict six-field path/bytes parser, serializer, digest, and source alias map.
- `clash_sub/generator.py` — owner-only overlay composition and runtime injection.
- `clash_sub/template_sync.py` — home-scope extraction and mixed-mode atomic outputs.
- `clash_sub/service.py` — home source loading and transactional `import_home(payload)`.
- `clash_sub/runtime.py` — inject home parser and reserve home activation path.
- `clash_sub/cli.py` — non-menu root-only `home-import` stdin command.
- `scripts/scan_tracked_secrets.py` — include root `private/home.yaml` in private-value comparison.
- `templates/variants/manifest.yaml` — remove tracked home feature declarations.
- `README.md`, `docs/operations.md`, `docs/private-data.md`, and only genuinely affected deployment/recovery text — final workflow and safety boundary.
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

Add `HomeOverlaySourceTests` with a synthetic document containing two proxy groups, the two approved extensions/injection lists, and the home CIDR rule. Assert exact tuple/mapping values, defensive copies, deterministic dump/load, and digest stability.

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

In `sources.py`, require the exact key set and use separate validation helpers so every failure maps to one approved code. `dump_home_overlay` must use `yaml.safe_dump(..., allow_unicode=True, sort_keys=False)` and end with one newline. Existing airport/xui functions keep their current generic errors.

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
- Produces: `render_user_bundle(is_owner, xui, airport, home: HomeOverlay | None, template_root) -> Mapping[str, str]`.
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

- balanced/privacy contain `HomeServer`, `ProxyServer`, home CIDR rule, home nodes, and both approved extensions;
- `PT站加速` does not contain `ProxyServer`;
- `ProxyServer` receives owner 3x-ui + renamed home nodes and `use: [AmyTelecom]`;
- `HomeServer` receives only renamed home nodes and never provider `use`;
- both standard profiles contain no home names, rules, controls, or extensions;
- invalid/overlapping injection controls, missing extension targets, duplicate group names, and final rules fail closed.

```python
def test_private_home_overlay_has_fixed_variant_authorization(self):
    bundle = render_user_bundle(True, [reality_proxy("Owner")], provider(), home_overlay(), TEMPLATE_ROOT)
    balanced = yaml.safe_load(bundle["balanced"])
    groups = {item["name"]: item for item in balanced["proxy-groups"]}

    self.assertEqual(groups["ProxyServer"]["proxies"], ["🎯 Direct", "HomeServer", "Owner", "Home"])
    self.assertEqual(groups["ProxyServer"]["use"], ["AmyTelecom"])
    self.assertEqual(groups["HomeServer"]["proxies"], ["🎯 Direct", "Home"])
    self.assertNotIn("ProxyServer", groups["PT站加速"]["proxies"])
```

- [ ] **Step 4: Run generator tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_generator -v
```

Expected: FAIL because generator still accepts a proxy list and reads the tracked home feature.

- [ ] **Step 5: Implement overlay composition and simplify the manifest**

Change source authorization to carry an overlay only for owner balanced/privacy. Copy private groups, rewrite explicit home proxy members through `source_aliases["home"]`, add the two extensions, prepend private rules, then apply the two injection lists. Keep public manifest injection for common groups and keep privacy DNS override; remove the `features` key and all tracked feature loading/application code.

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
- Produces: `run_template_sync(...) -> {"changed": ("templates/clash.yaml", "templates/variants/manifest.yaml", "private/home.yaml")}`.
- Produces internal `_split_workbench(root, workbench, home_scope) -> tuple[dict, dict, HomeOverlay]`.
- Produces internal mixed-mode writer using `0644` for tracked outputs and `0600` for `private/home.yaml`.

- [ ] **Step 1: Rewrite fixtures around an existing private scope**

Update each temporary repository fixture to write `private/home.yaml` mode `0600`. Construct the workbench by rendering a synthetic owner balanced profile with one owner xui proxy, one home proxy, and the synthetic Amy provider. Update `PUBLIC_TEMPLATE_FILES` to exclude the deleted feature and add a separate private output snapshot.

- [ ] **Step 2: Add failing extraction tests**

Cover:

- home group names come only from the existing private scope;
- home proxies are collected only from `inject-home-node-groups`, never from `ProxyServer` all-node members;
- copied private groups have runtime inline names and provider `use` removed;
- exactly two extensions and both injection lists are exported;
- the home-target rule moves to private rules and stays before public rules at render time;
- missing scope, insecure scope, missing declared group, and leaked private values preserve every previous byte and mode;
- an undeclared new group is treated as public, never receives home nodes, and must still pass the public-candidate and private-leak checks;
- a write failure at each of the three targets restores all previous outputs;
- success reports the three exact paths and keeps private output `0600`.

```python
def test_template_sync_exports_home_without_xui_or_provider_members(self):
    result = run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)
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

Use the existing home group/injection declarations as scope. Before building the private candidate, record all inline proxy names, collect only members of `inject-home-node-groups`, and strip every runtime inline member plus `use: AmyTelecom` from copied home groups. Derive extensions from remaining public groups, explicitly reject `PT站加速 -> ProxyServer`, and classify rules by parsed policy target.

Update candidate validation to render all four authorization cases with synthetic sources. Expand forbidden-name/value checks to include the candidate home proxy/group names and complete private rules. The replacement loop snapshots bytes and modes for all three outputs and restores all attempted paths after any error.

- [ ] **Step 5: Update CLI template-sync output tests**

Assert the command prints all three changed paths, never prints file contents, and retains the existing final prompt. Keep `template-sync` absent from the server menu.

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

### Task 4: Add transactional server-side home import

**Files:**

- Modify: `clash_sub/service.py:69-300`
- Modify: `clash_sub/runtime.py:1-76`
- Modify: `tests/test_lightweight_service.py`
- Modify: `tests/test_lightweight_end_to_end.py`
- Modify only if a failing regression proves necessary: `clash_sub/nginx.py`, `tests/test_lightweight_nginx.py`, `clash_sub/release_store.py`, `tests/test_lightweight_release_store.py`

**Interfaces:**

- Consumes: `parse_home_overlay`, `load_home_overlay`, `home_overlay_digest`, and generator API.
- Produces: `ClashSubService.import_home(payload: bytes) -> dict`.
- Changes: `_optional_home() -> HomeOverlay | None`.
- Uses existing `activate_runtime(..., extra_replacements=((home_path, payload, 0o600), ...))` so the home file participates in the durable activation journal.

- [ ] **Step 1: Update service fixtures to inject the new parser and overlay**

Replace proxy-list home fixtures with `HomeOverlay`. Add a `parse_home_overlay` dependency to `ClashSubService.__init__`, and make harnesses use the real parser while unit tests can inject a deterministic fake.

- [ ] **Step 2: Add failing import success and readiness tests**

```python
def test_import_home_switches_source_and_owner_release_together(self):
    self.bootstrap()
    old_release = self.state.users[7].current_release
    payload = home_document_bytes(node_name="Home New")

    result = self.service.import_home(payload)

    self.assertEqual((self.private_root / "home.yaml").read_bytes(), payload)
    self.assertNotEqual(result["release_id"], old_release)
    self.assertIn("Home New", self.owner_balanced_text())
```

Also assert missing/disabled owner, missing current owner release, or missing verified airport artifact returns `home_owner_not_ready` without creating `home.yaml`.

- [ ] **Step 3: Add failing rollback tests at every boundary**

Snapshot live home bytes, state, current marker, routes, current release, and status timestamp. Inject parser, xui fetch, render, Mihomo, release prepare, Nginx test/reload, `os.replace`, and journal failures. Every case must keep the snapshot equal, discard unreferenced release candidates, and journal only a safe `home_*` error.

```python
def test_home_import_nginx_failure_restores_source_and_release(self):
    before = self.active_view_with_home()
    self.runner.fail_nginx_test = True

    with self.assertRaisesRegex(ServiceError, "home_activation_failed"):
        self.service.import_home(home_document_bytes(node_name="Rejected"))

    self.assertEqual(self.active_view_with_home(), before)
    self.assert_candidate_cleanup(self)
```

- [ ] **Step 4: Run focused tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_service -v
.venv/bin/python -m unittest tests.test_lightweight_end_to_end -v
```

Expected: FAIL because service still loads a proxy list and has no import transaction.

- [ ] **Step 5: Implement import_home using the existing activation journal**

Within the operation lock: recover, parse bytes at `config.max_source_bytes`, reconcile, require an active/current owner and verified airport bytes, prepare only the owner with the candidate overlay, then activate with the home artifact included in `extra_replacements`. Map validation and activation failures to the approved `home_*` codes, discard candidate releases, and update the sanitized status journal.

```python
def import_home(self, payload):
    with self._lock():
        self._recover()
        home = self._parse_home(payload, self.config.max_source_bytes)
        snapshot, state = self._reconciled()
        owner = _client(snapshot.clients, state.owner_client_id)
        identity = _identity(state, owner.client_id)
        if not owner.enabled or not identity.current_release:
            raise ServiceError("home_owner_not_ready")
        airport = self._releases.read_airport_document(owner.client_id, identity.current_release)
        if airport is None:
            raise ServiceError("home_owner_not_ready")
        # prepare owner, then activate home bytes + current marker atomically
```

Change normal `sync_all`, airport update, and owner rotation to pass `HomeOverlay | None`. A malformed installed home must fail owner operations with a safe home-specific code while preserving the existing release.

In `runtime.build_service`, pass the real home parser/loader and include `config.private_root / "home.yaml"` in `ReleaseStore(..., activation_paths=...)` for space preflight. Existing Nginx extra-replacement support should need no production change; modify it only when a new regression test proves a missing guarantee.

- [ ] **Step 6: Run runtime regression tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_service -v
.venv/bin/python -m unittest tests.test_lightweight_end_to_end -v
.venv/bin/python -m unittest tests.test_lightweight_nginx -v
.venv/bin/python -m unittest tests.test_lightweight_release_store -v
```

Expected: PASS.

- [ ] **Step 7: Commit transactional import**

Stage only files actually required by the tests:

```bash
git add clash_sub/service.py clash_sub/runtime.py tests/test_lightweight_service.py tests/test_lightweight_end_to_end.py
git add clash_sub/nginx.py tests/test_lightweight_nginx.py clash_sub/release_store.py tests/test_lightweight_release_store.py
git commit -m "feat: activate uploaded home atomically"
```

Before committing, use `git diff --cached --name-only` and unstage any optional file with no necessary diff.

---

### Task 5: Expose the internal stdin command and one public upload script

**Files:**

- Create: `clash_sub/home_upload.py`
- Create: `scripts/upload-home.sh`
- Create: `tests/test_home_upload.py`
- Modify: `clash_sub/cli.py:176-620`
- Modify: `tests/test_lightweight_cli.py`
- Modify: `tests/test_repository_safety.py`

**Interfaces:**

- Consumes: `ClashSubService.import_home(payload)`.
- Produces internal server command: `/usr/local/bin/clash-sub home-import` reading stdin only.
- Produces local orchestration: `upload_home(repo_root: Path, target: str, runner=subprocess.run) -> int`.
- Produces local Python entry: `clash_sub.home_upload.main(argv=None) -> int`.
- Produces public user entry: `./scripts/upload-home.sh root@<server>`.

- [ ] **Step 1: Add failing CLI stdin tests**

Add `FakeService.import_home`, then assert root-only authorization, bounded binary/text stdin handling, empty/TTY rejection, no extra args, exact success output, stable error propagation, and absence from every menu rendering.

```python
def test_home_import_reads_stdin_and_never_enters_the_menu(self):
    payload = b"proxies: []\n"
    code, stdout, stderr = run_cli(["home-import"], self.service, stdin=io.BytesIO(payload))

    self.assertEqual(code, 0)
    self.assertEqual(self.service.calls, [("import_home", (payload,))])
    self.assertEqual(stdout, "家庭配置已上传并同步。\n")
    self.assertEqual(stderr, "")
```

Patch `os.geteuid` to root for success tests and assert non-root yields `not_root` before reading stdin.

- [ ] **Step 2: Implement the non-menu command**

Register `home-import` only in `_parser`. Add `_read_bounded_stdin(stdin, maximum)` that prefers `stdin.buffer` for real streams, accepts bytes in tests, encodes test strings as UTF-8, and reads at most `maximum + 1`. Call `factory().import_home(payload)` and print no paths or contents.

- [ ] **Step 3: Add failing local uploader tests**

Test a strict target validator, exactly one argument, fixed source path, symlink/hard-link/wrong-mode/oversize rejection, argv-list SSH invocation without `shell=True`, byte-identical stdin, SSH exit-code propagation, and secret-free stdout/stderr.

```python
def test_upload_streams_fixed_home_to_absolute_remote_command(self):
    completed = upload_home(self.root, "root@server.example", runner=self.runner)

    self.assertEqual(completed, 0)
    self.assertEqual(
        self.runner.argv,
        ["ssh", "--", "root@server.example", "/usr/local/bin/clash-sub", "home-import"],
    )
    self.assertEqual(self.runner.stdin_bytes, self.home_path.read_bytes())
```

- [ ] **Step 4: Implement Python orchestration and thin shell wrapper**

`home_upload.py` validates `root@` plus a hostname/IPv4/bracketed-IPv6 destination, uses `os.lstat` for regular/symlink/link-count/owner/mode/size checks, opens the fixed file as binary stdin, and calls `subprocess.run` with an argv list and no captured secret content.

The shell wrapper contains only repository resolution and venv execution:

```sh
#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
exec "$repo_dir/.venv/bin/python" -m clash_sub.home_upload "$@"
```

Set the tracked executable bit. Do not add a manual fallback.

- [ ] **Step 5: Run CLI, uploader, and safety tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_cli -v
.venv/bin/python -m unittest tests.test_home_upload -v
.venv/bin/python -m unittest tests.test_repository_safety -v
```

Expected: PASS.

- [ ] **Step 6: Commit the upload entry**

```bash
git add clash_sub/cli.py clash_sub/home_upload.py scripts/upload-home.sh tests/test_lightweight_cli.py tests/test_home_upload.py tests/test_repository_safety.py
git commit -m "feat: add private home upload command"
```

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
- Consumed by: real `template-sync` smoke test and later upload.

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

Do not copy redundant public rules and do not add `ProxyServer` to `PT站加速`.

- [ ] **Step 3: Secure and validate without displaying the file**

```bash
chmod 600 private/home.yaml
MIHOMO_BIN="/absolute/path/to/the-maintainer-selected-mihomo" ./bin/clash-sub template-sync
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
git status --short --ignored
```

Expected: template sync lists only its three paths, scan exits 0, and `private/home.yaml` remains ignored. If the Mac Mihomo path is not known, stop and ask the user for its absolute path; do not download or guess a binary.

- [ ] **Step 4: Verify the generated private file structurally without echoing values**

Use the repository loader in a command that prints only counts and booleans, never names or fields. Confirm nonzero proxy/group counts, exactly two extensions, exactly one all-node injection, exactly one home-only injection, exactly one home rule, and mode `0600`.

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
- Modify only where relevant and preserve existing user edits: `DEPLOYMENT.md`, `docs/recovery.md`
- Modify: `tests/test_lightweight_deployment.py`

**Interfaces:**

- Documents the two-command local workflow and no manual alternative.
- Keeps server menu structure unchanged and `home-import` undocumented as a direct user command.

- [ ] **Step 1: Add failing documentation assertions**

Assert current docs contain:

```text
MIHOMO_BIN="<absolute-path>" ./bin/clash-sub template-sync
./scripts/upload-home.sh root@<server>
```

Assert they describe the rolling latest balanced workbench, six private fields, owner variant isolation, automatic validation/activation, backup inclusion, and failure rollback. Assert current operational sections do not advertise SFTP, FTP, `scp`, inbox, direct private-root upload, or `templates/features/home.yaml`.

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
3. Run repository-local `template-sync` with an explicit Mihomo binary.
4. Review only tracked public diffs and run tests/scans.
5. Run `./scripts/upload-home.sh root@<server>`; success already validates and publishes, so no second server-menu refresh is needed.

Do not change current YAML comment/format behavior or add a preservation claim. Keep the root-only server home path in private-data/backup internals, but never present it as an upload destination. Preserve unrelated dirty documentation text line-by-line.

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

If a design-required sentence must change in `DEPLOYMENT.md` or `docs/recovery.md`, first verify that the file was clean at the Task 1 boundary. Otherwise leave it untouched and report the pre-existing overlap; never broadly stage either file.

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
.venv/bin/python -m unittest tests.test_home_upload -v
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

- [ ] **Step 4: Run real local template-sync smoke validation**

With the user-supplied absolute Mihomo path:

```bash
MIHOMO_BIN="/absolute/path/to/the-maintainer-selected-mihomo" ./bin/clash-sub template-sync
```

Expected: exactly `templates/clash.yaml`, `templates/variants/manifest.yaml`, and `private/home.yaml`, followed by the existing review/test reminder. Re-run relevant tests if this modifies tracked template bytes.

- [ ] **Step 5: Review commits and request code review**

```bash
git log --oneline --decorate -12
git diff b5af0a9...HEAD --stat
git diff b5af0a9...HEAD --check
```

Use the `superpowers:requesting-code-review` skill. The reviewer must compare the implementation against the spec, verify owner/member isolation, confirm the PT extension removal, and inspect activation rollback and secret-output boundaries.

- [ ] **Step 6: Final report**

Report exact test totals, skips, any baseline-only failures, commit hashes, the ignored private migration result without values, and whether a live server upload was intentionally not performed. Do not upload to a real server unless the user separately authorizes that external mutation.
