# Subscription Email Token Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every published `clash-sub` URL identify its 3x-ui client by email while retaining a 128-bit random capability.

**Architecture:** Introduce a v2 state/token format and a sync-only upgrade candidate. Build releases and exact Nginx routes from that candidate, then publish routes, releases and state with the existing activation transaction; read-only commands never advertise a candidate URL. Keep `/s/` and the 3x-ui loopback source unchanged.

**Tech Stack:** Python 3.9+, `unittest`, Nginx exact locations, existing SQLite snapshot and runtime activation code.

**Spec:** `docs/superpowers/specs/2026-10-16-subscription-email-token-design.md`

## Global Constraints

- URL token: 16 random bytes encoded as 22 unpadded URL-safe Base64 characters, then `-` and the unchanged client email.
- Email length: 1–254 ASCII characters; allowed characters are letters, digits, `.`, `_`, `@`, `+`, `-`.
- Owner airport-only capability derives a distinct 22-character core and uses the same email suffix.
- `/s/`, 3x-ui loopback `sub_id` source, profile filenames, response headers and refresh periods do not change.
- Successful first `sync` invalidates v1 links; v1 state remains readable solely for migration and failed-attempt recovery.
- Follow the user's AGENTS.md: do not commit, push or deploy without separate authorization. The skill's usual per-task commit step is replaced by a review checkpoint.

## Review Focus

- Hyphenated or mixed-case email: parser must keep the entire suffix unchanged; Task 1 tests it.
- Space, slash, percent sign, Unicode, control character or 255-character email: fail before creating a route; Task 1 tests it.
- First migration with one user preparation failure: leave v1 state and every old route active; Task 2 tests it.
- Owner email rename or rollback: provider URLs and the independent airport capability update together, while old-provider releases are rejected; Task 2 tests both.
- `links` called before `sync` after an email rename or code upgrade: must not print a URL that is not routable yet; Task 3 tests it.

---

## File map

- `clash_sub/state.py`, `clash_sub/domain.py`: v1 reader, v2 writer/validator, token codec and sync candidate.
- `clash_sub/service.py`: sync migration and rename orchestration; links and rotation behavior.
- `clash_sub/nginx.py`: validate and render exact v2 routes.
- `clash_sub/cli.py`: show email, client ID, migration or rename prompt, and airport mirror label.
- `tests/test_lightweight_state.py`, `tests/test_lightweight_service.py`, `tests/test_lightweight_nginx.py`, `tests/test_lightweight_cli.py`, `tests/test_lightweight_end_to_end.py`: unit and transactional acceptance checks.
- `docs/operations.md`, `docs/template-design.md`: upgrade and link-format instructions.

### Task 1: v2 token and state codec

**Files:** Modify `clash_sub/state.py`, `clash_sub/domain.py`; test `tests/test_lightweight_state.py`.

**Interfaces:**
- Produce `validate_email(email: str) -> str`: return unchanged valid email or raise `StateError("invalid client email")`.
- Produce `generate_token(email: str, *, random_bytes=secrets.token_bytes) -> str`: 22-character core plus `-` and email.
- Produce `airport_only_token(owner_token: str) -> str`: distinct 22-character core plus identical email suffix.
- Produce `prepare_link_update(state: RuntimeState, clients: tuple[XuiClient, ...]) -> RuntimeState`: v1 → v2 reissue for retained clients; v2 email change → same core/new suffix. Return a candidate, never persist it.
- `load_state(path)` accepts valid v1 and v2; `save_state(path, state)` preserves the supplied schema until successful sync migrates it to v2. `UserState.readable_code` remains an optional in-memory legacy field for v1, omitted from v2 JSON.

- [x] **Step 1: Write failing codec tests.** Update `test_token_has_random_core_and_readable_suffix`, `test_airport_only_capability_is_distinct_and_has_token_shape`, and add `test_email_suffix_keeps_hyphens_and_case` and `test_invalid_email_characters_and_length_fail_before_generation`. Assert 16 random bytes yield a 22-character core, the exact email suffix, a distinct owner airport core, and rejection of space, slash, percent, Unicode, control and 255-character input.
- [x] **Step 2: Run the state test file.** Run `.venv/bin/python -m unittest tests.test_lightweight_state -q`; expect the new tests to fail against the old codec.
- [x] **Step 3: Implement the token codec.** Use fixed core length to split the suffix; validate email before token construction. Keep v1 parsing, serialization and routes for pre-migration recovery and management; select airport derivation by explicit schema version.
- [x] **Step 4: Write failing state tests.** Add `test_v1_state_loads_for_migration_and_v2_round_trips`, `test_prepare_link_update_reissues_v1_and_renames_v2_without_persisting`, and `test_v2_rejects_mismatched_token_email`; assert schema 2 omits `readable_code`, v1 input remains unchanged, and invalid v2 state cannot save.
- [x] **Step 5: Implement v2 state serialization and candidate creation.** Retain `reconcile_state(previous, clients, owner_email)` for discovery, but preserve the stored email/token for existing users until `prepare_link_update` is invoked by `sync`.
- [x] **Step 6: Run state tests.** Run `.venv/bin/python -m unittest tests.test_lightweight_state -q`; expect `OK`. Review the Task 1 diff; do not commit.

### Task 2: Atomic migration and email rename in sync

**Files:** Modify `clash_sub/service.py`, `clash_sub/nginx.py`; test `tests/test_lightweight_service.py`, `tests/test_lightweight_nginx.py`, `tests/test_lightweight_end_to_end.py`.

**Interfaces:** Consume Task 1's `prepare_link_update`, `generate_token`, `airport_only_token` and v2 state validation. `sync_all()` prepares and activates a v2 candidate; `rotate_link(client_id)` preserves the email and replaces only the random core.

- [x] **Step 1: Write failing migration acceptance tests.** Add `test_first_sync_migrates_v1_links_atomically` and `test_failed_first_migration_keeps_v1_state_and_routes` to `tests/test_lightweight_end_to_end.py`. Assert v2 URLs match `^https://[^/]+/s/[A-Za-z0-9_-]{22}-[^/]+/` and old routes disappear only on success.
- [x] **Step 2: Run the new acceptance tests.** Run `.venv/bin/python -m unittest tests.test_lightweight_end_to_end -q` in a setgid-capable environment; expect the new assertions to fail.
- [x] **Step 3: Make first `sync` migration all-or-nothing.** Build the v2 candidate, force preparation of every enabled user, and abort before `_activate` if any candidate fails. Keep the existing activation transaction for state, release and Nginx publication. Disabled/deleted users follow current cleanup rules.
- [x] **Step 4: Write failing rename, rotation and rollback tests.** Add `test_email_rename_changes_link_on_sync_only`, `test_owner_rename_updates_profile_and_airport_provider_urls`, `test_rotate_v2_keeps_email_and_changes_random_core`, `test_owner_rollback_rejects_old_provider_url`, and `test_rejected_email_never_reaches_nginx` in service/nginx tests.
- [x] **Step 5: Implement v2 route and service changes.** Nginx exact locations accept complete validated tokens for the published schema; successful migration emits only v2 routes. For normal sync, keep existing per-user failure reporting: a failed renamed user retains their published identity and route while other users may update. Owner rename or rotation regenerates its profile bundle and derived airport route. Owner rollback verifies the candidate's provider URL equals the currently published owner provider URL before activation.
- [x] **Step 6: Run task tests.** Run `.venv/bin/python -m unittest tests.test_lightweight_service tests.test_lightweight_nginx tests.test_lightweight_end_to_end -q` in a setgid-capable environment; expect `OK`. Review the Task 2 diff; do not commit.

### Task 3: Only display published links

**Files:** Modify `clash_sub/service.py`, `clash_sub/cli.py`; test `tests/test_lightweight_service.py`, `tests/test_lightweight_cli.py`.

**Interfaces:** `Service.links()` returns published URLs plus `client_id`, `email` and a `sync_required` flag when v1 migration or an email rename is pending. `rotate-link` returns only the new full URLs after activation.

- [x] **Step 1: Write failing display tests.** Add `test_links_before_migration_or_rename_show_only_published_urls_and_sync_prompt`, `test_links_after_sync_show_email_and_client_id`, and `test_rotate_output_keeps_airport_mirror_label`. Assert no candidate token appears before activation.
- [x] **Step 2: Run display tests.** Run `.venv/bin/python -m unittest tests.test_lightweight_service tests.test_lightweight_cli -q`; expect the new tests to fail.
- [x] **Step 3: Implement published-state reads and CLI display.** For `links`, read saved state for URL construction and compare it with the 3x-ui snapshot only to set `sync_required`; do not call candidate generation. Print a stable prompt to run `sync`, plus email and client ID on each user heading. Keep the “机场镜像链接” label.
- [x] **Step 4: Run display tests.** Run `.venv/bin/python -m unittest tests.test_lightweight_service tests.test_lightweight_cli -q`; expect `OK`. Review the Task 3 diff; do not commit.

### Task 4: Documentation and final verification

**Files:** Modify `docs/operations.md`, `docs/template-design.md`; test/update affected assertions across the five test files above.

**Interfaces:** No new runtime API. Document `update` then `sync`, the v1 rollback boundary, URL privacy, allowed email characters, and the fact that 3x-ui's local Clash subscription remains required.

- [x] **Step 1: Update operations documentation.** Describe the first-sync migration, failed-migration retry, email rename taking effect on next successful sync, and old links expiring after success.
- [x] **Step 2: Update template/link documentation.** Show a placeholder URL only; document 22-character random core, email suffix, independent owner airport token and unchanged `/s/` path.
- [x] **Step 3: Run full verification.** Run `.venv/bin/python -m unittest discover -s tests -p 'test*.py' -q` in a setgid-capable environment, `.venv/bin/python scripts/scan_tracked_secrets.py`, and `git diff --check`; expect all tests to pass, a clean scan and no whitespace errors.
- [x] **Step 4: Review final diff and deployment impact.** Verify no unrelated files changed, no real token or email is in documentation, and no server write occurred. Report the changed URL contract and required server `update`/`sync`. Do not commit or push without separate authorization.
