# Task 6 Report

## Summary

Implemented the Task 6 review fixes on top of the atomic per-user release pipeline in the isolated worktree.

- Added immutable `Candidate` and `Release` models.
- Added `clash_sub.releases.ReleaseBuilder` with dependency injection for settings, converter, traffic, local snapshot loading, rendering, validation, template location, and clock.
- Added strict ASCII slug validation for path-sensitive release identifiers before any path join.
- Added canonical-root cleanup guards so candidate cleanup and retention pruning cannot delete outside trusted private roots.
- Added atomic publish, history, and rollback support with relative symlink switching, five-release retention, and history ordering from trusted filesystem state instead of manifest timestamps.
- Added manifest integrity verification with a sanitized `manifest.sha256` sidecar, plus strict manifest schema and hash/count shape validation.
- Tightened candidate publish identity checks so `candidate.path` must resolve exactly to `private_root/staging/<operation_id>/<user_id>`, `candidate.manifest_path` must resolve to that directory's `manifest.json`, and safe-slug validation still gates every path-sensitive identifier.
- Hardened publish against staging symlink forgery by requiring every managed path component from `private_root/staging` through `<operation_id>/<user_id>` to be a real directory, not a symlink, before any rename or switch. `candidate.path` itself must also be a real directory, so a forged staging symlink can no longer be published into releases.
- Changed manifest and release validation to honor each candidate's declared variant subset as a non-empty subset of supported variants, while still requiring exact YAML, sidecar, and output-hash key matching. Owner candidates continue to publish all three variants atomically.
- Added focused synthetic tests for member isolation, one-variant member publish, nested staging forgery rejection, staging symlink forgery rejection, owner-only snapshots, traversal rejection, sanitized manifests and sidecars, file permissions, retention, tamper detection, and rollback safety.

## Verification

Executed:

```bash
.venv/bin/python -m unittest tests.test_releases tests.test_validation tests.test_rendering tests.test_converter tests.test_repository_safety tests.test_traffic -v
```

Result: all 118 tests passed.

## Concerns

No unavoidable concerns were identified within Task 6 scope.
