# Clash Comment-Preserving Template Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the old `balanced` / `standard` / `privacy` template system with comment-preserving `compat-office` / `compat-universal` / `balance-office` subscriptions sourced directly from ClashX iCloud files.

**Architecture:** Keep one sanitized round-trip Compat base, one complete round-trip Balance `dns` document, one composition manifest, and one ignored private home overlay. The local template updater splits full owner profiles into those artifacts, while the runtime generator injects per-user 3x-ui nodes, owner-only airport access, and office-only home content before validation and immutable publication.

**Tech Stack:** Python 3.9+, `unittest`, PyYAML 6.0.3 for plain validation, `ruamel.yaml` 0.19.1 for comment-preserving YAML 1.2 round trips, Jinja2 3.1.6, fixed-version Mihomo on the server publication boundary.

**Spec:** `docs/superpowers/specs/2026-08-29-clash-template-redesign.md`

## Global Constraints

- Implementation is performed by a `gpt-5.6-luna` subagent with `max` reasoning; the Sol root agent owns final review and verification.
- The fixed owner profiles are exactly `("compat-office", "compat-universal", "balance-office")`; the fixed member profiles are exactly `("compat-universal",)`.
- `privacy` is absent from runtime code, templates, links, tests, and user-facing documentation in this change.
- Only owner outputs may carry the `AmyTelecom` provider; only `*-office` outputs may carry `private/home.yaml` content.
- No-argument `template-sync` reads `Compat-Office.yaml` and `Balance-Office.yaml` from `Path.home() / "Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents"`.
- Passing exactly one source option updates only that source; passing both updates both atomically.
- Local template updates never require Mihomo; server `clash-sub sync` still performs fixed-version Mihomo validation before publication.
- Never print real node names, addresses, UUIDs, passwords, provider URLs, query credentials, or private rules. Public comments may appear in the change report.
- Preserve unrelated worktree changes. The pre-existing deletion of `docs/dns-design.md` is authorized for the documentation cleanup task, not for earlier commits.
- Do not connect to, modify, or reinstall the server.

## File Responsibility Map

- Create `clash_sub/yaml_rt.py`: the only round-trip YAML loader/dumper and plain-data conversion boundary.
- Create `templates/base/compat-office.yaml`: sanitized public Compat base with comments and anchors.
- Create `templates/dns/balance-office.yaml`: complete top-level `dns:` document with Balance comments.
- Create `templates/profiles.yaml`: profile recipes and public injection groups; no role authorization.
- Modify `clash_sub/domain.py`: fixed profile constants and comment-carrying `HomeOverlay` metadata.
- Modify `clash_sub/sources.py`: strict home validation while preserving its round-trip document.
- Modify `clash_sub/template_sync.py`: iCloud inputs, splitting, one-time home bootstrap, complete Balance DNS extraction, safe report, and atomic writes.
- Modify `clash_sub/generator.py`: round-trip profile composition, node/provider/home injection, and final comment-preserving dump.
- Modify `clash_sub/cli.py`: source path options and report rendering.
- Modify `clash_sub/release_store.py`, `clash_sub/service.py`, and `clash_sub/nginx.py` only where hard-coded profile expectations or link/file assertions require the new fixed set.
- Modify tests under `tests/` in the same task as their production boundary.
- Rewrite the six retained user documents; delete superseded templates, historical documents, and approved ignored private artifacts only after replacement validation succeeds.

---

### Task 1: Add the round-trip YAML and comment-carrying home boundary

**Files:**
- Modify: `requirements.txt`
- Create: `clash_sub/yaml_rt.py`
- Modify: `clash_sub/domain.py` (`HomeOverlay`)
- Modify: `clash_sub/sources.py` (`parse_home_overlay`, `_load_home_document`, `_build_home_overlay`, `dump_home_overlay`)
- Test: `tests/test_lightweight_sources.py`
- Test: `tests/test_repository_safety.py`

**Interfaces:**
- Produces: `load_round_trip(payload: str | bytes) -> CommentedMap`
- Produces: `dump_round_trip(document: Mapping) -> str`
- Produces: `clone_round_trip(value: object) -> object`
- Produces: `plain_data(value: object) -> object`
- Produces: `copy_key_comments(source: CommentedMap, source_key: str, target: CommentedMap, target_key: str) -> None`
- Produces: `HomeOverlay.document`, excluded from equality/repr but deep-copied for safe reuse.
- Consumed by: Tasks 2 and 3.

- [ ] **Step 1: Pin the official round-trip dependency**

Add the current Python 3.9-compatible release verified on the official package index:

```text
Jinja2==3.1.6
PyYAML==6.0.3
ruamel.yaml==0.19.1
```

- [ ] **Step 2: Write failing round-trip and home-comment tests**

Add tests with synthetic values only:

```python
def test_home_round_trip_preserves_comments_anchor_and_order(self):
    payload = b"""# home header
proxies:
- &home
  name: Home
  type: ss  # proxy type
  server: 192.0.2.10
  port: 443
  cipher: aes-256-gcm
  password: synthetic-password
proxy-groups:
- name: HomeServer
  type: select
  proxies: [Home]
extend-proxy-groups: {}
inject-node-groups: []
inject-home-node-groups: [HomeServer]
rules:
- IP-CIDR,192.168.2.0/24,HomeServer,no-resolve
"""
    home = parse_home_overlay(payload, 1024 * 1024)
    rendered = dump_home_overlay(home).decode("utf-8")
    self.assertIn("# home header", rendered)
    self.assertIn("# proxy type", rendered)
    self.assertLess(rendered.index("proxies:"), rendered.index("proxy-groups:"))
```

Add a repository-safety assertion that `requirements.txt` pins exactly one `ruamel.yaml` version.

- [ ] **Step 3: Run the new tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_lightweight_sources \
  tests.test_repository_safety -v
```

Expected: FAIL because comments are currently discarded and `clash_sub.yaml_rt` does not exist.

- [ ] **Step 4: Implement `clash_sub/yaml_rt.py`**

Use one configured round-trip loader per operation, not one mutable global instance:

```python
import copy
from io import StringIO
from collections.abc import Mapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


class RoundTripYamlError(ValueError):
    pass


def _yaml():
    parser = YAML(typ="rt")
    parser.allow_duplicate_keys = False
    parser.preserve_quotes = True
    parser.width = 4096
    return parser


def load_round_trip(payload):
    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        document = _yaml().load(text)
    except Exception:
        raise RoundTripYamlError("yaml round trip failed") from None
    if not isinstance(document, CommentedMap):
        raise RoundTripYamlError("yaml root must be a mapping")
    return document


def dump_round_trip(document):
    stream = StringIO()
    try:
        _yaml().dump(document, stream)
    except Exception:
        raise RoundTripYamlError("yaml round trip failed") from None
    text = stream.getvalue()
    return text if text.endswith("\n") else text + "\n"


def clone_round_trip(value):
    return copy.deepcopy(value)


def copy_key_comments(source, source_key, target, target_key):
    if source.ca.comment is not None:
        target.ca.comment = copy.deepcopy(source.ca.comment)
    if source_key in source.ca.items:
        target.ca.items[target_key] = copy.deepcopy(source.ca.items[source_key])


def plain_data(value):
    if isinstance(value, Mapping):
        return {key: plain_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, CommentedSeq)):
        return [plain_data(item) for item in value]
    return copy.deepcopy(value)
```

- [ ] **Step 5: Preserve the validated home document**

Add `document` as a seventh optional `HomeOverlay` field:

```python
from dataclasses import dataclass, field

document: Mapping | None = field(default=None, repr=False, compare=False)
```

In `HomeOverlay.__post_init__`, deep-copy mapping entries without coercing a
`CommentedMap` through `dict()`. In `_build_home_overlay`, pass the validated
round-trip root as `document=document`. In `dump_home_overlay`, clone
`home.document` when present; otherwise build the existing six-key document,
then dump through `dump_round_trip`.

Keep all six schema keys and every existing structural validation. Translate
`RoundTripYamlError` to the existing stable `home_yaml_invalid` code.

- [ ] **Step 6: Run focused and full regression tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_lightweight_sources \
  tests.test_lightweight_generator \
  tests.test_lightweight_service \
  tests.test_repository_safety -v
```

Expected: PASS; programmatically constructed `HomeOverlay` fixtures remain compatible.

- [ ] **Step 7: Commit the YAML foundation**

```bash
git add requirements.txt clash_sub/yaml_rt.py clash_sub/domain.py clash_sub/sources.py \
  tests/test_lightweight_sources.py tests/test_repository_safety.py
git commit -m "feat: preserve comments in yaml sources"
```

---

### Task 2: Replace the local template updater with Compat and Balance inputs

**Files:**
- Rewrite: `clash_sub/template_sync.py`
- Rewrite: `tests/test_lightweight_template_sync.py`

**Interfaces:**
- Consumes: Task 1 round-trip functions and the existing strict `HomeOverlay` validators.
- Produces: `default_source_paths(home: Path | None = None) -> tuple[Path, Path]`
- Produces: `initialize_home_scope(repo_root: Path, compat_office: Path, compat_universal: Path) -> Path`
- Produces: `run_template_sync(repo_root: Path, compat_office: Path | None = None, balance_office: Path | None = None) -> TemplateSyncReport`
- Produces: `TemplateSyncReport.changed: tuple[str, ...]`
- Produces: `TemplateSyncReport.lines: tuple[str, ...]`
- Consumed by: Tasks 3 and 4.

- [ ] **Step 1: Replace old workbench fixtures with full-profile fixtures**

Build synthetic `Compat-Office`, `Compat-Universal`, and `Balance-Office`
documents. Include header comments, inline DNS comments, one shared 3x-ui
node, one home node, two home groups, one home rule, and a synthetic
`AmyTelecom` provider. Use RFC 5737 IPs and repeated-digit credentials.

Define the new expected paths:

```python
PUBLIC_TEMPLATE_FILES = (
    "templates/base/compat-office.yaml",
    "templates/dns/balance-office.yaml",
    "templates/profiles.yaml",
)
HOME_SCOPE_PATH = "private/home.yaml"
```

- [ ] **Step 2: Write failing input-selection tests**

Cover these exact calls:

```python
def test_no_paths_read_both_default_icloud_sources(self):
    compat, balance = default_source_paths(Path("/Users/tester"))
    self.assertEqual(compat.name, "Compat-Office.yaml")
    self.assertEqual(balance.name, "Balance-Office.yaml")
    self.assertIn("iCloud~com~west2online~ClashX", str(compat))

def test_explicit_compat_updates_only_compat_targets(self):
    report = run_template_sync(self.root, compat_office=self.compat_path)
    self.assertNotIn("templates/dns/balance-office.yaml", report.changed)

def test_explicit_balance_updates_only_balance_target(self):
    report = run_template_sync(self.root, balance_office=self.balance_path)
    self.assertEqual(report.changed, ("templates/dns/balance-office.yaml",))
```

- [ ] **Step 3: Write failing split, comment, and Balance tests**

Assert:

```python
compat_text = (root / "templates/base/compat-office.yaml").read_text()
balance_text = (root / "templates/dns/balance-office.yaml").read_text()
self.assertIn("# shared comment", compat_text)
self.assertIn("# balance dns comment", balance_text)
self.assertEqual(set(yaml.safe_load(balance_text)), {"dns"})
self.assertEqual(
    yaml.safe_load(balance_text)["dns"],
    yaml.safe_load(self.balance_path.read_text())["dns"],
)
```

Also assert that Balance differing outside `dns` raises
`TemplateSyncError("balance_profile_mismatch")`, a new approved stable error
code; a missing or insecure home scope raises the existing home error; and
comments containing an actual synthetic credential cause candidate rejection
instead of comment removal.

- [ ] **Step 4: Write failing bootstrap and atomicity tests**

`initialize_home_scope` must derive office-only nodes, groups, public-group
extensions, private rules, and injection declarations from the Office versus
Universal pair. It must write `private/home.yaml` as mode `0600` only after
the derived Universal reproduces the supplied Universal structurally.

Patch `_os_replace` to fail at every target index and assert all original
bytes and modes are restored. Add a byte-stability test for two identical
runs.

- [ ] **Step 5: Run the rewritten test module and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_template_sync -v
```

Expected: FAIL because the old updater requires
`private/workbench/balanced.yaml` and writes old paths.

- [ ] **Step 6: Implement the new fixed paths and result type**

Use these constants:

```python
ICLOUD_RELATIVE_ROOT = Path(
    "Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents"
)
COMPAT_SOURCE_NAME = "Compat-Office.yaml"
BALANCE_SOURCE_NAME = "Balance-Office.yaml"
TEMPLATE_OUTPUT_PATHS = (
    "templates/base/compat-office.yaml",
    "templates/dns/balance-office.yaml",
    "templates/profiles.yaml",
    "private/home.yaml",
)
OUTPUT_MODES = {
    "templates/base/compat-office.yaml": 0o644,
    "templates/dns/balance-office.yaml": 0o644,
    "templates/profiles.yaml": 0o644,
    "private/home.yaml": 0o600,
}


@dataclass(frozen=True)
class TemplateSyncReport:
    changed: tuple[str, ...]
    lines: tuple[str, ...]
```

`default_source_paths()` returns the two fixed filenames below
`Path.home()` unless a test supplies another home path. In
`run_template_sync`, both `None` arguments mean both defaults; one non-None
argument means exactly one explicit source.

- [ ] **Step 7: Adapt the existing safe split to round-trip nodes**

Retain the current security rules: regular bounded UTF-8 files, full Clash
validation, no Jinja markers, no duplicate proxy names, strict provider
shape, no private values in tracked candidates, synthetic composition, and
multi-file rollback.

When Compat is present:

1. load the current strict `private/home.yaml` scope;
2. strip dynamic inline nodes and the fixed provider;
3. move declared home objects and attached comments into the home candidate;
4. write the remaining public document as the Compat candidate;
5. derive injection groups into `profiles.yaml`.

When Balance is present, perform the same private/dynamic stripping, compare
`plain_data` outside `dns` with the current or newly supplied Compat
candidate, and create a one-key round-trip document containing the complete
Balance `dns` value plus its key comments.

Keep the orchestration explicit:

```python
def run_template_sync(repo_root, compat_office=None, balance_office=None):
    root = Path(repo_root)
    if compat_office is None and balance_office is None:
        compat_office, balance_office = default_source_paths()
    scope = _load_home_scope(root)
    compat_candidate = (
        _split_compat_source(root, Path(compat_office), scope)
        if compat_office is not None
        else _load_current_compat(root)
    )
    balance_candidate = (
        _extract_balance_dns(Path(balance_office), compat_candidate, scope)
        if balance_office is not None
        else None
    )
    candidates = _selected_candidates(
        compat_office, balance_office, compat_candidate, balance_candidate
    )
    _validate_candidates(root, candidates, scope)
    report = _build_report(root, candidates)
    _atomic_replace_outputs(root, candidates)
    return report


def initialize_home_scope(repo_root, compat_office, compat_universal):
    office = _load_source(Path(compat_office))
    universal = _load_source(Path(compat_universal))
    home = _derive_home_from_pair(office, universal)
    _validate_derived_universal(office, universal, home)
    _atomic_replace_private_home(Path(repo_root), dump_home_overlay(home))
    return Path(repo_root) / "private/home.yaml"
```

- [ ] **Step 8: Implement safe change reporting**

Build deterministic lines such as:

```text
Compat 基础：已更新
  - dns.fake-ip-filter：已修改
  - rules：新增 1，删除 0，修改 1
Balance DNS：无变化
家庭覆盖层：已更新
  - 节点数量：3 → 3
写入：templates/base/compat-office.yaml
```

Public paths and public comment text are allowed. Private reports contain
counts only. Do not include object `repr()`, exception payloads, source URLs,
node names, or rule text.

- [ ] **Step 9: Run the complete updater test matrix**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_template_sync -v
.venv/bin/python scripts/scan_tracked_secrets.py
```

Expected: PASS and the scanner prints no secret values.

- [ ] **Step 10: Commit the updater rewrite**

```bash
git add clash_sub/template_sync.py tests/test_lightweight_template_sync.py
git commit -m "feat: rebuild templates from clashx profiles"
```

---

### Task 3: Cut the generator and fixed profile model over to the new layout

**Files:**
- Modify: `clash_sub/domain.py`
- Rewrite: `clash_sub/generator.py`
- Modify: `templates/profiles.yaml` after generation in Task 5; use temporary fixtures in this task.
- Rewrite: `tests/test_lightweight_generator.py`
- Modify: `tests/test_lightweight_checks.py`

**Interfaces:**
- Consumes: Task 1 round-trip YAML, Task 2 output schema, and existing `render_user_bundle(is_owner, xui, airport, home, template_root)` callers.
- Produces: the same `render_user_bundle(...) -> dict[str, str]` public function with new fixed keys.
- Produces: `_compose_variant(template_root: Path, variant: str) -> tuple[CommentedMap, dict[str, str]]`.
- Consumed by: Task 4 service and publication layers.

- [ ] **Step 1: Write the new fixed-set tests**

Replace old assertions with:

```python
self.assertEqual(
    tuple(render_user_bundle(True, owner_nodes, provider(), home, root)),
    ("compat-office", "compat-universal", "balance-office"),
)
self.assertEqual(
    tuple(render_user_bundle(False, member_nodes, None, None, root)),
    ("compat-universal",),
)
```

Assert the exact authorization matrix: owner Universal has the provider but
no home groups/rules; member Universal has neither provider nor home; both
Office outputs have home; all outputs contain only the authorized inline
3x-ui nodes.

- [ ] **Step 2: Write failing complete-DNS and comment tests**

Create a temporary template tree where the Compat and Balance DNS values and
comments differ. Assert:

```python
self.assertIn("# compat shared comment", owner["compat-office"])
self.assertIn("# compat shared comment", owner["compat-universal"])
self.assertIn("# balance dns comment", owner["balance-office"])
self.assertNotIn("compat-only-dns.example", owner["balance-office"])
self.assertEqual(
    yaml.safe_load(owner["balance-office"])["dns"],
    yaml.safe_load((root / "dns/balance-office.yaml").read_text())["dns"],
)
```

Add a home-comment assertion proving that a comment attached to a home group
appears only in Office outputs.

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_lightweight_generator \
  tests.test_lightweight_checks -v
```

Expected: FAIL because the generator still loads `templates/clash.yaml` and old variants.

- [ ] **Step 4: Replace the fixed profile constants**

In `clash_sub/domain.py`:

```python
VARIANTS = ("compat-office", "compat-universal", "balance-office")
OWNER_VARIANTS = VARIANTS
MEMBER_VARIANTS = ("compat-universal",)
```

Do not add aliases for old names.

- [ ] **Step 5: Load the new profile recipes**

`templates/profiles.yaml` has this strict shape:

```yaml
profiles:
  compat-office:
    dns: compat
    home: true
  compat-universal:
    dns: compat
    home: false
  balance-office:
    dns: balance-office
    home: true
inject-node-groups: []
```

Validate that the profile key set equals `OWNER_VARIANTS`, every recipe has
exactly `dns` and `home`, only the two approved DNS values exist, and the
fixed home booleans match the code authorization. The manifest describes
composition but cannot add node sources or widen roles.

- [ ] **Step 6: Compose and dump round-trip documents**

Start each profile from a deep clone of
`templates/base/compat-office.yaml`. For Balance, replace the complete
top-level `dns` object and its comments from
`templates/dns/balance-office.yaml`. Apply dynamic proxy/provider/home
composition to `CommentedMap` and `CommentedSeq` objects without converting
through plain dicts.

Use one strict composition entry point:

```python
def _compose_variant(template_root, variant):
    root = Path(template_root)
    document = load_round_trip((root / "base/compat-office.yaml").read_bytes())
    manifest = _load_manifest(root / "profiles.yaml")
    recipe = manifest["profiles"][variant]
    if recipe["dns"] == "balance-office":
        balance = load_round_trip((root / "dns/balance-office.yaml").read_bytes())
        document["dns"] = clone_round_trip(balance["dns"])
        copy_key_comments(balance, "dns", document, "dns")
    injections = {
        group: "all" for group in manifest["inject-node-groups"]
    }
    return document, injections
```

Change `_HOME_VARIANTS` to:

```python
_HOME_VARIANTS = frozenset({"compat-office", "balance-office"})
```

Change `_authorized_sources` to return the new order, and call
`dump_round_trip(document)` instead of `yaml.safe_dump`.

- [ ] **Step 7: Preserve home key and sequence comments during injection**

When appending home proxies, groups, and rules, clone objects from
`home.document` so item comments survive. Copy the `proxies`,
`proxy-groups`, and `rules` key-comment slots only into Office documents.
Universal must not retain orphan comments from removed home objects.

- [ ] **Step 8: Run generator and security tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_lightweight_generator \
  tests.test_lightweight_checks \
  tests.test_secret_scan -v
```

Expected: PASS with the new profile keys and preserved comments.

- [ ] **Step 9: Commit the generator cutover**

```bash
git add clash_sub/domain.py clash_sub/generator.py \
  tests/test_lightweight_generator.py tests/test_lightweight_checks.py
git commit -m "feat: generate compat and balance profiles"
```

---

### Task 4: Propagate the new profiles through CLI, service, releases, links, and routes

**Files:**
- Modify: `clash_sub/cli.py`
- Modify: `clash_sub/release_store.py`
- Modify: `clash_sub/service.py`
- Modify: `clash_sub/nginx.py` only if a fixed old name is found.
- Modify: `tests/test_lightweight_cli.py`
- Modify: `tests/test_lightweight_release_store.py`
- Modify: `tests/test_lightweight_service.py`
- Modify: `tests/test_lightweight_nginx.py`
- Modify: `tests/test_lightweight_end_to_end.py`

**Interfaces:**
- Consumes: new domain constants, `render_user_bundle`, and `TemplateSyncReport`.
- Produces: `clash-sub template-sync [--compat-office PATH] [--balance-office PATH]`.
- Produces: owner release files `clash-compat-office.yaml`, `clash-compat-universal.yaml`, `clash-balance-office.yaml`, `AmyTelecom.yaml`.
- Produces: member release file `clash-compat-universal.yaml` only.

- [ ] **Step 1: Replace release fixtures and assertions**

Use:

```python
self.member_bundle = {"compat-universal": "proxies: []\n"}
self.owner_bundle = {
    "compat-office": "proxies: [compat-office]\n",
    "compat-universal": "proxies: [compat-universal]\n",
    "balance-office": "proxies: [balance-office]\n",
}
```

Assert `_filename` produces the three new filenames and old bundle keys are
rejected as an invalid fixed set. Do not retain legacy manifest support.

- [ ] **Step 2: Replace service, link, route, and end-to-end expectations**

Every owner fake renderer returns exactly the three new keys; every member
fake renderer returns only `compat-universal`. Update URL assertions to:

```text
/s/<token>/clash-compat-office.yaml
/s/<token>/clash-compat-universal.yaml
/s/<token>/clash-balance-office.yaml
```

Assert member routes expose only the Universal URL and never expose
`AmyTelecom.yaml`.

- [ ] **Step 3: Write failing CLI argument and report tests**

Patch `template_sync.run_template_sync` and verify:

```python
code = main(["template-sync"], stdout=stdout, stderr=stderr)
mock.assert_called_once_with(default_repo_root(), None, None)

code = main([
    "template-sync", "--compat-office", "/tmp/Compat-Office.yaml"
], stdout=stdout, stderr=stderr)
mock.assert_called_once_with(
    default_repo_root(), Path("/tmp/Compat-Office.yaml"), None
)
```

The success output must equal `"\n".join(report.lines) + "\n"`; stable
`TemplateSyncError.code` remains the only failure detail printed.

- [ ] **Step 4: Run the affected tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_lightweight_cli \
  tests.test_lightweight_release_store \
  tests.test_lightweight_service \
  tests.test_lightweight_nginx \
  tests.test_lightweight_end_to_end -v
```

Expected: FAIL on old keys, filenames, and missing CLI options.

- [ ] **Step 5: Add CLI source options and report rendering**

Change the parser:

```python
template = commands.add_parser("template-sync", add_help=False)
template.add_argument("--compat-office", type=Path)
template.add_argument("--balance-office", type=Path)
```

Pass both parsed values to `_template_sync`. `_template_sync` calls:

```python
report = template_sync.run_template_sync(
    default_repo_root(), parsed.compat_office, parsed.balance_office
)
for line in report.lines:
    stdout.write("%s\n" % line)
```

Do not require root for this local command.

- [ ] **Step 6: Remove remaining hard-coded old profile assumptions**

Prefer imports from `clash_sub.domain` rather than duplicating tuples. The
release store's existing fixed-set validation and filename function should
work after fixture updates; make production changes only where a literal old
name remains. Keep owner airport raw-byte publication unchanged.

- [ ] **Step 7: Run the complete affected suite**

Run the command from Step 4 again.

Expected: PASS; URLs and immutable release manifests contain only the new set.

- [ ] **Step 8: Commit the publication cutover**

```bash
git add clash_sub/cli.py clash_sub/release_store.py clash_sub/service.py \
  clash_sub/nginx.py tests/test_lightweight_cli.py \
  tests/test_lightweight_release_store.py tests/test_lightweight_service.py \
  tests/test_lightweight_nginx.py tests/test_lightweight_end_to_end.py
git commit -m "feat: publish new clash profile set"
```

---

### Task 5: Initialize the real private home scope and replace shipped templates

**Files:**
- Create (ignored): `private/home.yaml`
- Create: `templates/base/compat-office.yaml`
- Create: `templates/dns/balance-office.yaml`
- Create: `templates/profiles.yaml`
- Delete: `templates/clash.yaml`
- Delete: `templates/variants/manifest.yaml`
- Delete: `templates/variants/privacy-dns.yaml`
- Delete after validation (ignored): `private/proxies.yaml`
- Delete after validation (ignored): `private/proxy-groups.yaml`
- Delete after validation (ignored): `private/rules.yaml`
- Delete after validation (ignored): `private/reference-configs/`
- Modify: tests that assert the shipped template paths.

**Interfaces:**
- Consumes: Tasks 1-4 and the three existing ClashX iCloud files.
- Produces: the exact tracked template tree used by runtime generation.
- Produces: the local `0600` home overlay that must later be transferred during a separate server deployment.

- [ ] **Step 1: Verify exact source targets without printing contents**

Run:

```bash
stat -f '%N mode=%Lp bytes=%z' \
  "$HOME/Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/Compat-Office.yaml" \
  "$HOME/Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/Compat-Universal.yaml" \
  "$HOME/Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/Balance-Office.yaml"
```

Expected: three regular non-empty files. Never print their contents.

- [ ] **Step 2: Bootstrap and validate `private/home.yaml`**

Call `initialize_home_scope` through a short `python -c` import using the
three explicit `Path` values; the function itself performs atomic mode-0600
write and structural reproduction checks. Then run:

```bash
stat -f '%N mode=%Lp bytes=%z' private/home.yaml
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
```

Expected: `private/home.yaml mode=600`; scanner output names categories and
paths only and succeeds.

- [ ] **Step 3: Generate the new tracked templates from both default files**

Run:

```bash
./bin/clash-sub template-sync
```

Expected: a safe summary naming the new tracked paths and private counts,
with no source URLs, node names, UUIDs, passwords, or private rule text.

- [ ] **Step 4: Prove the generated files are structurally safe**

Run:

```bash
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python -m unittest \
  tests.test_lightweight_generator \
  tests.test_lightweight_template_sync \
  tests.test_secret_scan -v
git diff --check
```

Expected: PASS. Do not proceed to deletion if any command fails.

- [ ] **Step 5: Delete only the approved old tracked templates**

Use `apply_patch` for the three tracked YAML deletions. Verify
`templates/variants/` is empty before removing that directory. Do not touch
`templates/nginx/`.

- [ ] **Step 6: Delete only the approved ignored private artifacts**

Resolve each exact path under the repository, confirm `private/home.yaml`
still exists and parses, then remove only:

```text
private/proxies.yaml
private/proxy-groups.yaml
private/rules.yaml
private/reference-configs/
```

These ignored files are not recoverable from Git. Report their deletion and
that recovery requires the user's own backup; do not remove `private/home.yaml`.

- [ ] **Step 7: Update shipped-path and repository-safety tests**

Assert the new three tracked template files exist; the three old template
paths do not exist; no tracked template contains a non-empty `proxies` list,
an `AmyTelecom` URL, or a private home group/rule.

- [ ] **Step 8: Run the shipped-template regression suite**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_lightweight_generator \
  tests.test_lightweight_template_sync \
  tests.test_repository_safety \
  tests.test_secret_scan -v
```

Expected: PASS against the actual generated templates.

- [ ] **Step 9: Commit only tracked replacements**

```bash
git add templates tests/test_lightweight_generator.py \
  tests/test_lightweight_template_sync.py tests/test_repository_safety.py \
  tests/test_secret_scan.py
git commit -m "feat: replace shipped clash templates"
```

Confirm `git diff --cached --name-only` never lists `private/home.yaml` or any source profile.

---

### Task 6: Replace the verbose documentation and run final implementation verification

**Files:**
- Rewrite: `README.md`
- Rewrite: `DEPLOYMENT.md`
- Rewrite: `docs/3x-ui-setup.md`
- Rewrite: `docs/operations.md`
- Rewrite: `docs/private-data.md`
- Rewrite: `docs/recovery.md`
- Delete: `docs/dns-design.md`
- Delete: `docs/legacy-trojan-topology.md`
- Delete: `docs/superpowers/plans/2026-08-21-clash-subscription-publication.md`
- Delete: `docs/superpowers/plans/2026-08-23-clash-sub-lightweight.md`
- Delete: `docs/superpowers/plans/2026-08-25-clash-sub-integration.md`
- Delete: `docs/superpowers/plans/2026-08-28-private-home-overlay-upload.md`
- Delete: `docs/superpowers/specs/2026-08-21-clash-subscription-publication-design.md`
- Delete: `docs/superpowers/specs/2026-08-23-clash-sub-lightweight-redesign.md`
- Delete: `docs/superpowers/specs/2026-08-25-clash-sub-integration-design.md`
- Delete: `docs/superpowers/specs/2026-08-27-local-template-workbench-design.md`
- Delete: `docs/superpowers/specs/2026-08-28-private-home-overlay-upload-design.md`
- Delete: `docs/superpowers/specs/2026-08-28-stable-amytelecom-provider-design.md`
- Keep: this plan and `docs/superpowers/specs/2026-08-29-clash-template-redesign.md`
- Modify: `tests/test_lightweight_deployment.py`
- Modify: `tests/test_repository_safety.py`

**Interfaces:**
- Consumes: the finished command names, paths, profile matrix, and deployment boundary.
- Produces: one short source of truth per user workflow and no historical compatibility guidance.

- [ ] **Step 1: Write failing documentation-contract tests**

Assert README contains the four-row authorization matrix, the three final
filenames, and the no-argument `template-sync` command. Assert operations
documents the default iCloud path, single-source examples, safe report, and
the separate future server upload boundary. Assert no retained user document
mentions `clash-balanced.yaml`, `clash-standard.yaml`, or
`clash-privacy.yaml`.

- [ ] **Step 2: Run documentation tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_lightweight_deployment \
  tests.test_repository_safety -v
```

Expected: FAIL because current documents describe the old variants and workbench.

- [ ] **Step 3: Rewrite the six retained documents concisely**

Use these single responsibilities:

```text
README.md             purpose, trust matrix, data flow, daily commands, doc links
DEPLOYMENT.md         prerequisites, clean install, verification checklist
docs/3x-ui-setup.md   only the panel/inbound/client fields this project needs
docs/operations.md    airport, template update, sync, links, rollback, troubleshooting
docs/private-data.md  ignored paths, modes, backup and transfer boundary
docs/recovery.md      clean rebuild and restore order
```

Explain each concept once and link to it elsewhere. Do not reproduce menu
screens, historical rationale, implementation internals, or old migration
steps. Keep commands copyable and use short tables only for exact mappings.

- [ ] **Step 4: Delete only the approved historical documents**

Use `apply_patch` for tracked deletions. Preserve the new 2026-08-29 spec and
this plan. The already deleted `docs/dns-design.md` is included in this
authorized cleanup commit.

- [ ] **Step 5: Run documentation and stale-name checks**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_lightweight_deployment \
  tests.test_repository_safety -v
rg -n 'clash-(balanced|standard|privacy)\.yaml|private/workbench/balanced\.yaml' \
  README.md DEPLOYMENT.md docs/3x-ui-setup.md docs/operations.md \
  docs/private-data.md docs/recovery.md clash_sub templates tests
```

Expected: tests PASS and `rg` returns no matches.

- [ ] **Step 6: Commit the documentation replacement**

```bash
git add README.md DEPLOYMENT.md docs tests/test_lightweight_deployment.py \
  tests/test_repository_safety.py
git commit -m "docs: simplify the new clash workflow"
```

- [ ] **Step 7: Run the complete implementation verification**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
.venv/bin/python -m compileall -q clash_sub tests
git diff --check
git status --short
```

Expected:

- every unit/integration test passes;
- both secret scans succeed without printing secret values;
- compileall and diff checks succeed;
- only intentional implementation changes remain;
- no server command or external deployment has run.

- [ ] **Step 8: Hand control back to the Sol root reviewer**

Report commits, changed paths, test counts, template-sync summary categories,
and any intentionally uncommitted ignored file changes. Do not claim final
completion; the Sol root agent performs the independent final diff review,
reruns verification, and requests fixes if needed.
