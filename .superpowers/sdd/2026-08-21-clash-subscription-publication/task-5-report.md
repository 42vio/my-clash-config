Task 5 report

Red evidence
- `2026-08-21`: `.venv/bin/python -m unittest tests.test_validation -v`
- Result: failed in the new synthetic regressions because rule target extraction treated `no-resolve` as the target for `IP-ASN,13335,no-resolve,Selector`, VLESS `ws+tls+servername` was misclassified as REALITY, recursive proxy-group references were not rejected, and whitespace-only proxy/group names were only caught later as unresolved references.

Green evidence
- `2026-08-21`: `.venv/bin/python -m unittest tests.test_validation -v`
- Result: `Ran 33 tests ... OK`
- `2026-08-21`: `.venv/bin/python -m unittest tests.test_rendering -v`
- Result: `Ran 21 tests ... OK`
- `2026-08-21`: `.venv/bin/python -m unittest tests.test_converter -v`
- Result: `Ran 26 tests ... OK`
- `2026-08-21`: `.venv/bin/python -m unittest tests.test_repository_safety -v`
- Result: `Ran 2 tests ... OK`
- `2026-08-21`: `git diff --check`
- Result: clean

Commit
- Planned: `fix: close task 5 validation review gaps`

Remaining concerns
- None within Task 5 scope.
