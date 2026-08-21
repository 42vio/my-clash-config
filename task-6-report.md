# Task 6 Report

## Summary

Implemented the Task 6 review fixes on top of the atomic per-user release pipeline in the isolated worktree.

- Added immutable `Candidate` and `Release` models.
- Added `clash_sub.releases.ReleaseBuilder` with dependency injection for settings, converter, traffic, local snapshot loading, rendering, validation, template location, and clock.
- Added strict ASCII slug validation for path-sensitive release identifiers before any path join.
- Added canonical-root cleanup guards so candidate cleanup and retention pruning cannot delete outside trusted private roots.
- Added atomic publish, history, and rollback support with relative symlink switching, five-release retention, and history ordering from trusted filesystem state instead of manifest timestamps.
- Added manifest integrity verification with a sanitized `manifest.sha256` sidecar, plus strict manifest schema and hash/count shape validation.
- Added focused synthetic tests for member isolation, owner-only snapshots, traversal rejection, sanitized manifests and sidecars, file permissions, retention, tamper detection, and rollback safety.

## Verification

Executed:

```bash
.venv/bin/python -m unittest tests.test_releases tests.test_validation tests.test_rendering tests.test_converter tests.test_repository_safety tests.test_traffic -v
```

Result: all 115 tests passed.

## Concerns

No unavoidable concerns were identified within Task 6 scope.
