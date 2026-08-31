# Install Progress Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `bash install.sh` show the current installation step, truthful step-based progress, resume state, elapsed time, and actionable failure output.

**Architecture:** Keep the existing durable `InstallState.phases_done` journal as the source of truth. The POSIX shell bootstrap reports steps 1–3, then passes a display-only offset to the Python installer, which reports its existing nine phases as steps 4–12. Output is append-only text with no ANSI cursor control or third-party progress library, so it remains readable in terminals, redirected logs, and CI.

**Tech Stack:** POSIX `sh`, Python 3.9+, standard library (`time`), `unittest`/`unittest.mock`.

**Spec:** `plans/2026-08-30-install-progress-display.md#design-contract` (this self-contained handoff document)

## Global Constraints

- Address the user as “Sir” in all agent responses, per repository instructions.
- Do not change installation phase behavior, phase order, journal schema, rollback ownership, or error codes.
- Do not print the Cloudflare token, owner email, panel base path, subscription URLs, or other secrets during progress reporting.
- Progress means completed steps, not estimated elapsed time; never show an ETA.
- Use no new dependency and no ANSI cursor movement, spinner thread, or terminal-width detection.
- Preserve Python 3.9 compatibility and POSIX `/bin/sh` compatibility.
- A direct `bin/clash-sub install` invocation reports `0/9` through `9/9`; `bash install.sh` reports the unified `0/12` through `12/12` flow.
- Existing completed Python phases must be displayed as `沿用记录` and must not execute again.
- On failure, preserve the original `InstallerError` and exit status after emitting phase context and saved progress.
- Touch only `install.sh`, `clash_sub/installer.py`, `clash_sub/cli.py`, `tests/test_lightweight_deployment.py`, `tests/test_lightweight_installer.py`, `tests/test_lightweight_cli.py`, and the installation paragraph in `DEPLOYMENT.md`.
- At handoff time, `DEPLOYMENT.md` and `tests/test_lightweight_sources.py` already contain unrelated user changes. Preserve them exactly; append the progress paragraph surgically and never stage `tests/test_lightweight_sources.py` for this feature.

---

## Design Contract

### Output states

- `▶` means the named step has started.
- `✓` means the step completed during this run.
- `↷` means a shell bootstrap step was already satisfied and skipped.
- `✓ … 沿用记录` means a Python phase is already present in the durable journal.
- `✗` means the named step raised an error.
- `!` is reserved for the existing post-install manual Reality-listen instruction.

Every Python phase uses the fixed position implied by the existing order:

| Position with bootstrap | Phase key | User-facing label |
|---:|---|---|
| 4 | `preflight` | 检查服务器环境 |
| 5 | `low_memory` | 优化低内存配置 |
| 6 | `nginx_packages` | 安装并配置 Nginx |
| 7 | `mihomo` | 安装 Mihomo 核心 |
| 8 | `certificate` | 申请 TLS 证书 |
| 9 | `nginx_activation` | 激活访问路由 |
| 10 | `systemd_harden` | 配置系统服务 |
| 11 | `subscription_init` | 初始化订阅服务 |
| 12 | `report` | 完成安装检查 |

### Required examples

Fresh phase start and completion:

```text
[█████░░░░░░░░░░░░░░░] 3/12 · 25%
▶ [4/12] 检查服务器环境
✓ [4/12] 检查服务器环境                 已完成  1.2s
```

Resume:

```text
检测到未完成的安装记录：已完成 7/12，准备继续。
✓ [4/12] 检查服务器环境                 沿用记录
▶ [8/12] 申请 TLS 证书
```

Failure:

```text
✗ [8/12] 申请 TLS 证书：失败             31.6s
已保存安装进度：7/12
操作失败（错误代码：certificate_issue_failed）
修正问题后重新执行：bash install.sh
```

The progress bar is exactly 20 cells. Filled cells are calculated with integer floor division: `completed * 20 // total`. Percentage is also floored: `completed * 100 // total`.

---

### Task 1: Python phase progress and resume reporting

**Files:**
- Modify: `clash_sub/installer.py:1-40,241-246,847-893`
- Test: `tests/test_lightweight_installer.py:1360-1440`

**Interfaces:**
- Consumes: existing `InstallState.phases_done`, `Installer.print_fn`, and the existing nine phase actions.
- Produces: `Installer(..., progress_offset=0, clock=None)`, `_progress_line(completed: int, total: int) -> str`, and stable progress messages sent through `print_fn`.

- [ ] **Step 1: Add failing unit tests for progress formatting**

Import `_progress_line` from `clash_sub.installer` and add these tests near `InstallOrchestrationTests`:

```python
class InstallProgressFormattingTests(unittest.TestCase):
    def test_progress_line_uses_twenty_cells_and_floor_percentage(self):
        self.assertEqual(
            _progress_line(3, 12),
            "[█████░░░░░░░░░░░░░░░] 3/12 · 25%",
        )

    def test_progress_line_handles_empty_and_complete(self):
        self.assertEqual(_progress_line(0, 9), "[░░░░░░░░░░░░░░░░░░░░] 0/9 · 0%")
        self.assertEqual(_progress_line(9, 9), "[████████████████████] 9/9 · 100%")
```

- [ ] **Step 2: Run the formatting tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_installer.InstallProgressFormattingTests -v
```

Expected: import failure because `_progress_line` does not exist.

- [ ] **Step 3: Implement the formatter and phase metadata**

Add `import time`, `_PROGRESS_WIDTH = 20`, and an ordered label mapping next to `_INSTALL_PHASES`. Keep `_INSTALL_PHASES` unchanged for journal validation.

```python
_INSTALL_PHASE_LABELS = {
    "preflight": "检查服务器环境",
    "low_memory": "优化低内存配置",
    "nginx_packages": "安装并配置 Nginx",
    "mihomo": "安装 Mihomo 核心",
    "certificate": "申请 TLS 证书",
    "nginx_activation": "激活访问路由",
    "systemd_harden": "配置系统服务",
    "subscription_init": "初始化订阅服务",
    "report": "完成安装检查",
}


def _progress_line(completed, total):
    filled = completed * _PROGRESS_WIDTH // total
    percent = completed * 100 // total
    return "[%s%s] %d/%d · %d%%" % (
        "█" * filled,
        "░" * (_PROGRESS_WIDTH - filled),
        completed,
        total,
        percent,
    )
```

Change the constructor without changing existing defaults:

```python
def __init__(
    self, repo_root, *, paths=None, runner=None, print_fn=None,
    progress_offset=0, clock=None
):
    self.repo_root = Path(repo_root)
    self.paths = paths or InstallPaths()
    self.runner = runner or subprocess.run
    self.print_fn = print_fn or (lambda message: None)
    self.progress_offset = progress_offset
    self.clock = clock or time.monotonic
    self._state_path = self.repo_root / "private" / "install-state.json"
```

Reject a boolean, negative, or non-integer offset with `ValueError("invalid progress offset")`. This is display-only validation and must not introduce a new `InstallerError` code.

- [ ] **Step 4: Run formatting tests and verify pass**

Run the command from Step 2. Expected: 2 tests pass.

- [ ] **Step 5: Add failing orchestration tests for fresh, resumed, and failed phases**

Update the existing `test_install_skips_completed_phases_and_persists_domain`: replace the assertion that no preflight message exists with assertions that `检查服务器环境` and `沿用记录` both exist while `preflight` and `low_memory` actions remain uncalled.

Add a constructor validation test:

```python
def test_progress_offset_rejects_invalid_values(self):
    for value in (-1, True, "3"):
        with self.subTest(value=value):
            with self.assertRaisesRegex(ValueError, "invalid progress offset"):
                Installer(self.root, progress_offset=value)
```

Add a failure test that starts after two journaled phases. Patch `install_nginx_packages` to raise `InstallerError("command_failed")`, patch the x-ui snapshot to return the owner, construct the installer with `progress_offset=3`, `print_fn=printed.append`, and a deterministic clock returning `10.0` then `12.5`. Assert:

```python
self.assertIn("✓ [4/12] 检查服务器环境", printed)
self.assertIn("✓ [5/12] 优化低内存配置", printed)
self.assertIn("▶ [6/12] 安装并配置 Nginx", printed)
self.assertIn("✗ [6/12] 安装并配置 Nginx：失败", printed)
self.assertIn("已保存安装进度：5/12", printed)
self.assertFalse(any("安装 Mihomo 核心" in line for line in printed))
```

The test must also assert that the raised exception is still `InstallerError("command_failed")`.

- [ ] **Step 6: Run orchestration tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_installer.InstallOrchestrationTests -v
```

Expected: failures because resumed and failed progress messages are not implemented.

- [ ] **Step 7: Replace the orchestration print with start/success/resume/failure messages**

Keep the existing `phases` tuple and phase order. Compute `total = self.progress_offset + len(phases)`. Before the loop, if `done` is non-empty, emit exactly one resume summary using the number of completed journaled phases plus the offset.

For each phase at zero-based index `index`:

```python
position = self.progress_offset + index + 1
label = _INSTALL_PHASE_LABELS[name]
```

For a completed phase, emit `✓ [%d/%d] %s                 沿用记录` and continue without invoking its action. For a pending phase:

1. Emit `_progress_line(self.progress_offset + completed_count, total)`.
2. Emit `▶ [position/total] label`.
3. Capture `started = self.clock()`.
4. Invoke the existing action.
5. On `Exception`, emit `✗ [position/total] label：失败  elapsed`, then `已保存安装进度：completed/total`, and re-raise the same exception. Do not catch `BaseException`; `KeyboardInterrupt` and `SystemExit` retain their current behavior.
6. On success, increment the local completed count and emit `✓ [position/total] label  已完成  elapsed`.

Use `%.1fs` for elapsed time. Do not rely on fixed padding in tests; assert meaningful substrings. After the loop, emit `_progress_line(total, total)`. Delete the old `phase %s: done` output.

- [ ] **Step 8: Run installer tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_installer -v
```

Expected: all installer tests pass and completed actions are still skipped.

- [ ] **Step 9: Commit Task 1**

```bash
git add clash_sub/installer.py tests/test_lightweight_installer.py
git commit -m "feat: report installer phase progress"
```

---

### Task 2: CLI heading, unified offset, and actionable errors

**Files:**
- Modify: `clash_sub/cli.py:725-774`
- Test: `tests/test_lightweight_cli.py:1130-1310`

**Interfaces:**
- Consumes: internal environment variable `CLASH_SUB_PROGRESS_OFFSET`, set by `install.sh`.
- Produces: `Installer(progress_offset=<non-negative integer>)`, a non-secret install heading, and retry guidance on `InstallerError`.

- [ ] **Step 1: Add failing CLI tests**

Update the four local `FakeInstaller.__init__` methods in install tests to accept and record `progress_offset=0` in addition to `root` and `print_fn`.

Add a test that sets `CLASH_SUB_DOMAIN`, `CLASH_SUB_OWNER_EMAIL`, and `CLASH_SUB_PROGRESS_OFFSET="3"`, patches `getpass`, `Installer`, and `os.geteuid`, then asserts:

```python
self.assertEqual(captured["progress_offset"], 3)
self.assertNotIn("owner@x", stdout.getvalue())
self.assertNotIn("tok", stdout.getvalue())
```

Add a second case with `CLASH_SUB_PROGRESS_OFFSET="not-a-number"`; assert the installer receives `0` and stdout contains `clash-sub 安装程序`, because an inherited or manually supplied malformed display hint must not block installation. Do not print the raw domain in the Python heading: domain validation happens later in the existing phase flow, so echoing it here would allow terminal control characters from an untrusted environment value.

Add a failure case whose fake installer raises `InstallerError("certificate_issue_failed")`. Assert stderr contains both the existing stable error and `修正问题后重新执行：bash install.sh`.

- [ ] **Step 2: Run the CLI install tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_cli.InstallCommandTests -v
```

Expected: progress offset, heading, and retry assertions fail.

- [ ] **Step 3: Implement safe offset parsing and heading output**

Inside `_install`, after the owner is known and before creating `Installer`, parse the internal hint:

```python
try:
    progress_offset = int(os.environ.get("CLASH_SUB_PROGRESS_OFFSET", "0"))
except ValueError:
    progress_offset = 0
if progress_offset < 0:
    progress_offset = 0

if progress_offset == 0:
    stdout.write("\nclash-sub 安装程序\n")
```

Pass `progress_offset=progress_offset` to `Installer`. The shell bootstrap already prints the heading when the offset is 3, so the conditional prevents a duplicate heading; direct CLI invocation still gets one. Do not print domain, owner email, or token. In the `except InstallerError` branch, write the retry instruction to stderr before returning through `_error`; keep `_error` and every status code unchanged.

- [ ] **Step 4: Run CLI tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_cli -v
```

Expected: all CLI tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add clash_sub/cli.py tests/test_lightweight_cli.py
git commit -m "feat: explain install progress failures"
```

---

### Task 3: Shell bootstrap steps and operator documentation

**Files:**
- Modify: `install.sh:1-20`
- Modify: `DEPLOYMENT.md:29-43`
- Test: `tests/test_lightweight_deployment.py:140-155`

**Interfaces:**
- Consumes: existing tool, virtualenv, and requirements checks.
- Produces: shell steps 1–3 and `CLASH_SUB_PROGRESS_OFFSET=3` for the Python installer.

- [ ] **Step 1: Add failing deployment contract tests**

Extend `test_install_sh_bootstraps_venv_and_executes_install` with exact semantic fragments:

```python
self.assertIn("▶ [1/12] 检查基础工具", text)
self.assertIn("▶ [2/12] 创建 Python 环境", text)
self.assertIn("▶ [3/12] 安装项目依赖", text)
self.assertIn("CLASH_SUB_PROGRESS_OFFSET=3", text)
```

Add a syntax test:

```python
def test_install_sh_has_valid_posix_shell_syntax(self):
    result = subprocess.run(
        ["sh", "-n", str(INSTALL_SH)], capture_output=True, text=True
    )
    self.assertEqual(result.returncode, 0, result.stderr)
```

- [ ] **Step 2: Run deployment tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_deployment.LightweightDeploymentTests.test_install_sh_bootstraps_venv_and_executes_install tests.test_lightweight_deployment.LightweightDeploymentTests.test_install_sh_has_valid_posix_shell_syntax -v
```

Expected: progress-fragment assertions fail; syntax test passes.

- [ ] **Step 3: Add the three shell progress steps**

Keep `set -eu`, root validation, and command behavior intact. Emit the following markers around existing operations:

```sh
echo "clash-sub 安装程序"
echo "▶ [1/12] 检查基础工具"
```

If all required commands already exist, emit `↷ [1/12] 检查基础工具                 已存在`; otherwise run the existing apt commands and emit `✓ [1/12] 检查基础工具                 已完成` only after success.

Apply the same rule to `.venv/bin/python` for step 2. Step 3 always emits start and success around the existing quiet pip install. Immediately before `exec`, set the offset for that one process without changing caller state:

```sh
CLASH_SUB_PROGRESS_OFFSET=3 exec bin/clash-sub install
```

Do not suppress apt output: package-manager diagnostics remain necessary when installation fails.

- [ ] **Step 4: Document the progress and resume behavior**

Append one paragraph after the existing installation-input paragraph in `DEPLOYMENT.md`:

```markdown
安装过程按 12 个步骤显示当前操作和已完成进度；百分比表示完成的步骤比例，不是剩余时间估算。若 Python 安装阶段失败，修正错误后重新执行 `bash install.sh`，安装器会读取安装记录并沿用已经完成的阶段。
```

- [ ] **Step 5: Run deployment and documentation contract tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_deployment -v
```

Expected: all tests pass, including the repository rule that permits only the existing four maintenance documents under the root and `docs/` tree.

- [ ] **Step 6: Commit Task 3**

```bash
git add install.sh DEPLOYMENT.md tests/test_lightweight_deployment.py
git commit -m "feat: show bootstrap install progress"
```

---

### Task 4: End-to-end verification and output audit

**Files:**
- Verify only; make fixes solely in files already listed under Global Constraints.

**Interfaces:**
- Consumes: all changes from Tasks 1–3.
- Produces: evidence that behavior, tests, shell syntax, secret safety, and repository documentation constraints remain intact.

- [ ] **Step 1: Run the focused install suites**

```bash
.venv/bin/python -m unittest \
  tests.test_lightweight_installer \
  tests.test_lightweight_cli \
  tests.test_lightweight_deployment -v
```

Expected: all tests pass.

- [ ] **Step 2: Run the complete lightweight suite**

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 3: Verify shell syntax and whitespace**

```bash
sh -n install.sh
git diff --check
```

Expected: both commands exit 0 with no output.

- [ ] **Step 4: Audit displayed strings for secret leakage and stale output**

```bash
rg -n "phase %s: done|Cloudflare.*%|owner.*%|cf_token.*print|token.*print" \
  install.sh clash_sub/installer.py clash_sub/cli.py
```

Expected: no stale phase output and no interpolation of sensitive values. Prompts such as `请输入 Cloudflare API Token` are allowed because they do not echo the token.

- [ ] **Step 5: Review the final diff for scope**

```bash
git diff --stat
git diff -- install.sh clash_sub/installer.py clash_sub/cli.py DEPLOYMENT.md \
  tests/test_lightweight_deployment.py tests/test_lightweight_installer.py \
  tests/test_lightweight_cli.py
```

Every changed line must implement progress display, tests, or its deployment documentation. Do not include unrelated refactors or formatting.

- [ ] **Step 6: Commit verification fixes only if needed**

If verification required a code or test correction, commit only that correction:

```bash
git add install.sh clash_sub/installer.py clash_sub/cli.py DEPLOYMENT.md \
  tests/test_lightweight_deployment.py tests/test_lightweight_installer.py \
  tests/test_lightweight_cli.py
git commit -m "fix: align install progress output"
```

If no correction was needed, do not create an empty commit.

## Final Acceptance Checklist

- A fresh `bash install.sh` visibly advances from step 1 to step 12.
- A direct `bin/clash-sub install` visibly advances through its nine Python phases.
- A slow phase announces its name before executing.
- Completed phases are not executed again and are labeled `沿用记录`.
- A failure identifies the failed step, retains the stable error code, reports saved progress, and gives the retry command.
- The success output retains the panel URL and manual Reality-listen instruction.
- No secret is added to stdout, stderr, tests, fixtures, documentation, or commits.
- Full tests pass, `sh -n install.sh` passes, and `git diff --check` is clean.
