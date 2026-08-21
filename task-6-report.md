# Task 6 Report

## Summary

Implemented atomic per-user release building and publication in the isolated worktree.

- Added immutable `Candidate` and `Release` models.
- Added `clash_sub.releases.ReleaseBuilder` with dependency injection for settings, converter, traffic, local snapshot loading, rendering, validation, template location, and clock.
- Added atomic publish, history, and rollback support with relative symlink switching and five-release retention.
- Added focused synthetic tests for member isolation, owner-only snapshots, sanitized manifests and sidecars, file permissions, retention, tamper detection, and rollback safety.

## Verification

Executed:

```bash
.venv/bin/python -m unittest tests.test_releases -v
.venv/bin/python -m unittest tests.test_releases tests.test_validation tests.test_rendering tests.test_converter tests.test_repository_safety tests.test_traffic -v
```

Result: all tests passed.

## Concerns

No unavoidable concerns were identified within Task 6 scope.
