# Task 4 Final Report

Date: 2026-08-21
Branch: `codex/clash-subscription`

## Scope

Implemented the final Task 4 migration fix in the isolated worktree:

- kept `balanced` and `privacy` strict for unresolved `proxy-groups[*].proxies[*]` targets;
- restricted `balanced-win` unresolved-target handling to the exact approved path tuples only:
  - `('proxy-groups', 5, 'proxies', 2)`
  - `('proxy-groups', 6, 'proxies', 2)`
  - `('proxy-groups', 7, 'proxies', 2)`
- made any other unresolved `balanced-win` target fail with a path-only error, without printing the unresolved scalar value;
- made compare normalization enforce the same allowlist instead of stripping arbitrary unresolved `balanced-win` targets;
- removed only the approved balanced-win unresolved targets during transformation so the rendered output has no unresolved target left;
- kept built-in target handling, provider validation, and `use` validation strict;
- preserved one tracked shared template, variant-specific ignored private proxy snapshots, the private output path guard, `0600` snapshot mode, and atomic temp-file cleanup;
- kept logs and report output path-only, with no private scalar values.

## Files Changed

- `clash_sub/reference_rules.py`
- `scripts/migrate_reference_templates.py`
- `scripts/compare_reference_configs.py`
- `tests/test_rendering.py`

## Verification

Focused rendering regression set:

- PASS: `.venv/bin/python -m unittest tests.test_rendering.RenderingTests.test_variant_specific_private_snapshots_allow_balanced_win_to_omit_home_nodes tests.test_rendering.RenderingTests.test_balanced_win_migration_drops_stale_unresolved_home_target_and_compare_stays_clean tests.test_rendering.RenderingTests.test_balanced_win_rejects_extra_unresolved_proxy_path_even_with_same_stale_value tests.test_rendering.RenderingTests.test_compare_normalize_reference_rejects_extra_balanced_win_unresolved_proxy_path tests.test_rendering.RenderingTests.test_migration_private_snapshots_use_mode_600 tests.test_rendering.RenderingTests.test_migration_rejects_private_proxy_dir_outside_primary_checkout_before_writes tests.test_rendering.RenderingTests.test_validate_reference_document_accepts_reject_drop_builtin_target tests.test_rendering.RenderingTests.test_validate_reference_document_rejects_unresolved_proxy_target_with_path_only_error tests.test_rendering.RenderingTests.test_balanced_variant_still_rejects_unknown_proxy_target tests.test_rendering.RenderingTests.test_private_proxy_dir_guard_accepts_only_primary_checkout_owner_directory tests.test_rendering.RenderingTests.test_atomic_write_text_cleans_up_temporary_file_when_replace_fails -v`

Broader suites:

- PASS: `.venv/bin/python -m unittest tests.test_rendering -v`
- PASS: `.venv/bin/python -m unittest tests.test_repository_safety -v`
- PASS: `git diff --check`

Authoritative migration:

- PASS: `.venv/bin/python scripts/migrate_reference_templates.py --reference-dir /Users/42vio/Workspace/my-mihomo-config/private/reference-configs/2026-08-21 --template-dir /Users/42vio/Workspace/my-mihomo-config/.worktrees/codex/clash-subscription/templates --private-proxy-dir /Users/42vio/Workspace/my-mihomo-config/private/sources/owner`
  - safe summary:
    - `balanced`: `private-proxies=5`, `inject-node-groups=4`
    - `balanced-win`: `private-proxies=1`, `inject-node-groups=2`, `dropped-unresolved-proxy-targets=3`
    - `privacy`: `private-proxies=5`, `inject-node-groups=4`
  - balanced-win dropped unresolved paths:
    - `proxy-groups[5].proxies[2]`
    - `proxy-groups[6].proxies[2]`
    - `proxy-groups[7].proxies[2]`
  - no extra unresolved balanced-win path was accepted, and no scalar value was printed.

Authoritative compare:

- PASS: `.venv/bin/python scripts/compare_reference_configs.py --reference-dir /Users/42vio/Workspace/my-mihomo-config/private/reference-configs/2026-08-21 --template-dir /Users/42vio/Workspace/my-mihomo-config/.worktrees/codex/clash-subscription/templates --private-proxy-dir /Users/42vio/Workspace/my-mihomo-config/private/sources/owner`

Tracked secret scan:

- NOT AVAILABLE: `scripts/scan_tracked_secrets.py` is absent in this worktree, so the existing repository safety suite was used instead.

## Commit Status

Ready to commit after staging the Task 4 diff.
