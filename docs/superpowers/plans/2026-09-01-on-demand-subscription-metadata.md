# 按需订阅流量元数据与机场链接持久化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 3x-ui 主订阅在客户端刷新时按需更新流量信息，并让机场链接、机场文件和上游流量信息安全持久化及复用。

**Architecture:** Nginx 继续负责公网精确路由、授权和静态 YAML 发送；一个仅通过 systemd Unix Socket 激活的本机元数据服务负责五分钟 3x-ui 流量缓存和已保存机场流量响应头。机场正文与私密来源记录组成可恢复的双文件事务，机场更新继续与主配置发布完全解耦。

**Tech Stack:** Python 3.9+ 标准库、SQLite、Nginx、systemd、unittest、现有原子文件与运行时恢复框架。

**Spec:** 本计划内的“全局契约”即已确认设计；执行者不得用实现便利改变契约。

## Global Constraints

- 从干净的 `main` 开始；编写计划时基线为 `7be4095`，执行前先记录实际基线，不得覆盖用户后续提交。
- 创建独立的 `codex/` 分支或 worktree；不要直接在 `main` 实施。
- 每个任务先写失败测试并确认 RED，再写最小实现并确认 GREEN，最后独立提交。
- 不推送、不合并；测试服务器发生写入、清理或全新部署前，必须得到用户明确授权。
- 不访问或修改正式服务器。
- 不打印、记录、提交机场 URL、订阅 token、节点、YAML 正文、Cookie 或其他凭据。
- 机场正文仍不解析、不转换、不校验；只保留既有 HTTPS、重定向、超时、大小、非空和文件安全边界。
- 流量变化不得生成 release、修改主 YAML、切换发布或重载 Nginx。
- owner/member 文件矩阵、名称大小写、Home 客户端脚本和模板注释契约保持不变。
- 项目尚未正式上线，测试服务器最终采用全新部署；不写旧流量定时机制的迁移或兼容代码。
- 历史设计/计划文档允许记录旧机制；旧机制清零守卫只检查生产代码、部署文件、当前用户文档和运行时测试。

---

## 文件结构

建议新增：

- `clash_sub/airport_source.py`：机场来源记录的数据模型、序列化、安全读取与校验。
- `clash_sub/metadata.py`：3x-ui 五分钟缓存、机场流量读取、响应头规范化和元数据请求处理。
- `clash_sub/metadata_server.py`：使用 systemd 传入 Unix Socket 的本机 HTTP 服务。
- `deploy/systemd/clash-sub-metadata.socket`：Socket 激活与权限。
- `deploy/systemd/clash-sub-metadata.service`：最小权限元数据进程。
- `tests/test_lightweight_airport_source.py`：机场来源记录和双文件恢复事务。
- `tests/test_lightweight_metadata.py`：缓存、并发、降级及内部请求边界。

需要修改：

- `clash_sub/domain.py`
- `clash_sub/sources.py`
- `clash_sub/airport_store.py`
- `clash_sub/service.py`
- `clash_sub/runtime.py`
- `clash_sub/nginx.py`
- `clash_sub/cli.py`
- `clash_sub/manage.py`
- `clash_sub/installer.py`
- `deploy/systemd/`
- 对应现有测试和安全守卫
- `README.md`
- `DEPLOYMENT.md`
- `docs/operations.md`
- `docs/template-design.md`

不要借本任务重构无关代码。若现有文件边界与建议文件名冲突，先在进度报告说明原因；接口与行为契约不得漂移。

## 已确认的产品契约

### 3x-ui 主订阅流量

- 请求 `Clash-Compat.yaml` 或 `Clash-Balance.yaml` 时查询流量元数据。
- 缓存固定有效 300 秒。
- 任意用户触发缓存刷新时，一次读取全部 3x-ui 用户。
- 同一 owner 的 Compat 与 Balance 共用同一客户端缓存。
- 并发缓存失效请求只允许一个执行者读取数据库。
- 数据库失败且有旧缓存时返回旧缓存；无缓存时仍返回 YAML，但省略 `Subscription-Userinfo`。
- 主订阅继续返回 `Profile-Update-Interval: 24`。
- 客户端自动刷新与手动刷新走同一按需链路。

### 机场操作

机场子菜单固定为：

```text
机场订阅

1. 更换机场订阅链接
2. 刷新机场订阅
3. 查看机场状态
0. 返回
```

“更换机场订阅链接”：

- URL 使用可见交互输入，清理首尾空白。
- 不接受 URL 命令行参数，避免进入 shell history。
- 下载成功后才保存新链接。
- 同时更新 `AmyTelecom.yaml`、机场流量和最近成功时间。
- 任一步失败都完整保留旧链接、旧文件和旧流量。

“刷新机场订阅”：

- 直接读取此前保存的 URL，不重新输入或显示。
- 更新机场正文、流量和最近成功时间。
- 不读取 x-ui、不渲染配置、不调用 Mihomo、不操作 release、不执行 `sync`、不激活 Nginx。

“查看机场状态”只显示：

- 是否保存链接
- 来源域名
- 总量、已用量、剩余量
- 到期时间
- 最近成功时间
- `AmyTelecom.yaml` 是否存在

不得显示 URL 路径、查询参数或 token。

### 机场正文与流量

- 下载正文只执行 HTTPS、最多三次 HTTPS 重定向、15 秒超时、5 MB、非空与既有文件安全检查。
- 正文原始字节、顺序和注释原样保存。
- 流量读取最终响应的 `Subscription-Userinfo`。
- 只接受非负整数 `upload`、`download`、`total`、`expire`，保存规范化字段而非原始响应头。
- 流量头缺失或非法时，机场正文仍可成功更新；新记录的 `traffic` 必须为 `null`，不得沿用旧流量。
- 请求服务器 `AmyTelecom.yaml` 时只返回最近保存的机场流量，不访问机场上游。
- 将服务器链接直接导入 Clash Verge 时，应能显示最近保存的流量与到期信息。

### 删除旧流量机制

- 删除旧定时流量 service/timer 文件。
- 删除旧流量更新 CLI 命令和 `ClashSubService` 方法。
- 删除安装器、状态、文档和测试中的当前运行时引用。
- 不增加旧单元清理、兼容入口或 post-update 迁移。
- 测试服务器通过全新部署消除遗留单元。

---

### Task 1: 捕获机场下载流量元数据

**Files:**

- Modify: `clash_sub/domain.py`
- Modify: `clash_sub/sources.py`
- Test: `tests/test_lightweight_sources.py`

**Interfaces:**

- Produces: `AirportDownload(document: bytes, traffic: Traffic | None)`。
- Produces: `download_airport_document(url: str, max_bytes: int, opener=None) -> AirportDownload`。
- Consumes: 既有 `Traffic` 与 `parse_subscription_userinfo()`。

- [ ] **Step 1: 写失败测试**

覆盖正文原字节、最终响应头、缺失/非法流量头、重定向、空正文、超限与安全错误码。非法流量头必须包含重复字段、负数、额外字段、非数字与换行注入样例。

- [ ] **Step 2: 运行聚焦测试并确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_sources -v
```

失败原因必须是返回类型和流量捕获尚未实现，而不是 fixture 或环境错误。

- [ ] **Step 3: 实现最小下载结果模型**

建议接口：

```python
@dataclass(frozen=True)
class AirportDownload:
    document: bytes
    traffic: Traffic | None
```

读取最终响应的单个 `Subscription-Userinfo`；解析失败只令 `traffic=None`，不得拒绝正文。不要捕获或保存其他上游响应头。

- [ ] **Step 4: 运行聚焦测试并确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_sources -v
```

- [ ] **Step 5: 提交**

```bash
git add clash_sub/domain.py clash_sub/sources.py tests/test_lightweight_sources.py
git commit -m "feat: capture airport traffic metadata"
```

### Task 2: 机场来源记录与可恢复双文件事务

**Files:**

- Create: `clash_sub/airport_source.py`
- Modify: `clash_sub/airport_store.py`
- Create: `tests/test_lightweight_airport_source.py`
- Modify: `tests/test_lightweight_airport_store.py`

**Interfaces:**

- Produces: `AirportSource(source_url: str, traffic: Traffic | None, last_success: int)`。
- Produces: `AirportStore.read_source() -> AirportSource`。
- Produces: `AirportStore.replace(document: bytes, source: AirportSource)`，逻辑上同时切换正文与来源记录。
- Produces: 幂等的机场事务恢复入口，由所有机场读写操作在持锁后先调用。

- [ ] **Step 1: 写来源文件安全测试**

固定路径为 `/var/lib/clash-sub/private/airport-source.json`，要求 `0600 root:root`、常规文件、非 symlink、硬链接数 1，父目录满足既有 root-only 契约。测试 schema 版本、URL 类型、流量字段与时间类型。

- [ ] **Step 2: 写事务故障注入测试**

覆盖候选写入、文件 fsync、第一次替换、第二次替换、目录 fsync、事务日志清理和恢复重入。任何中断恢复后只能得到完整旧组合或完整新组合。

- [ ] **Step 3: 运行测试并确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_airport_source tests.test_lightweight_airport_store -v
```

- [ ] **Step 4: 实现来源模型和事务**

JSON schema 固定为：

```json
{
  "schema_version": 1,
  "source_url": "https://example.invalid/subscription",
  "traffic": {
    "upload": 1,
    "download": 2,
    "total": 3,
    "expire": 4
  },
  "last_success": 1788192000
}
```

`traffic` 可为 `null`。候选必须在目标文件系统内创建、设置最终权限、flush/fsync 后才进入替换事务。错误不得包含路径、URL、正文或流量值。

稳定错误码：

- `airport_source_missing`
- `airport_source_invalid`
- `airport_provider_write_failed`

- [ ] **Step 5: 运行测试并确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_airport_source tests.test_lightweight_airport_store -v
```

- [ ] **Step 6: 提交**

```bash
git add clash_sub/airport_source.py clash_sub/airport_store.py tests/test_lightweight_airport_source.py tests/test_lightweight_airport_store.py
git commit -m "feat: persist airport source transactionally"
```

### Task 3: 拆分机场服务接口、菜单与状态

**Files:**

- Modify: `clash_sub/service.py`
- Modify: `clash_sub/runtime.py`
- Modify: `clash_sub/cli.py`
- Test: `tests/test_lightweight_service.py`
- Test: `tests/test_lightweight_cli.py`

**Interfaces:**

- Produces: `replace_airport_source(url: str) -> dict`。
- Produces: `refresh_airport() -> dict`。
- Produces: `airport_status() -> dict`。
- Removes: 旧单一机场更新接口与相关 CLI 契约。

- [ ] **Step 1: 写三个服务接口的失败测试**

证明新链接只有在成功下载后保存；保存链接刷新不读取输入；任何失败保留旧三件套；流量缺失仍成功但结果明确标记；两个写操作都不触碰 x-ui、render、Mihomo、release、Nginx 激活或状态发布。

- [ ] **Step 2: 写菜单和脱敏失败测试**

证明主菜单选项 1 进入机场子菜单；新链接输入可见；空输入在构造 service 前拒绝；状态只显示域名和公开摘要；URL 查询串及 token 不出现在 stdout/stderr/异常。

- [ ] **Step 3: 运行测试并确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_service tests.test_lightweight_cli -v
```

- [ ] **Step 4: 实现最小服务与 CLI 调整**

没有合法流量头时使用成功提示“机场订阅已更新，未获取到流量信息”。`refresh_airport()` 缺少来源记录时返回 `airport_source_missing`。

- [ ] **Step 5: 运行测试并确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_service tests.test_lightweight_cli -v
```

- [ ] **Step 6: 提交**

```bash
git add clash_sub/service.py clash_sub/runtime.py clash_sub/cli.py tests/test_lightweight_service.py tests/test_lightweight_cli.py
git commit -m "feat: split airport replace and refresh flows"
```

### Task 4: 五分钟 3x-ui 流量缓存

**Files:**

- Create: `clash_sub/metadata.py`
- Create: `tests/test_lightweight_metadata.py`
- Modify: `clash_sub/domain.py` only if a small shared immutable response type is required

**Interfaces:**

- Produces: 一个以客户端 ID 查询 `Traffic | None` 的缓存接口。
- Consumes: `read_xui_snapshot(config.xui_database)`。
- Produces: `render_subscription_userinfo(traffic: Traffic) -> str`，仅输出四个规范字段。
- Produces: airport 类型读取已保存 `AirportSource.traffic`，不调用 downloader。

- [ ] **Step 1: 写缓存语义失败测试**

覆盖首次读取、300 秒内命中、300 秒后刷新、一次刷新全部客户端、Compat/Balance 共用、并发 single-flight、数据库失败使用旧缓存、无缓存时返回 `None`。

- [ ] **Step 2: 写缓存文件安全测试**

固定路径 `/var/lib/clash-sub/private/traffic-cache.json`，要求 `0600 root:root`、原子更新、不含 token/sub_id/URL/节点。损坏、symlink、错误模式或 schema 错误必须忽略并重建，不得阻断查询。

- [ ] **Step 3: 运行测试并确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_metadata -v
```

- [ ] **Step 4: 实现最小缓存与 single-flight**

时间和数据库读取均可注入，以便测试不依赖 sleep。一次成功 snapshot 写入全部客户端；失败不得覆盖旧缓存。规范响应头为：

```text
upload=1; download=2; total=3; expire=4
```

示例数字在实际响应中替换为对应 `Traffic` 的非负整数值。

- [ ] **Step 5: 运行测试并确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_metadata -v
```

- [ ] **Step 6: 提交**

```bash
git add clash_sub/metadata.py clash_sub/domain.py tests/test_lightweight_metadata.py
git commit -m "feat: add on-demand traffic metadata cache"
```

### Task 5: systemd Socket 激活的元数据服务

**Files:**

- Create: `clash_sub/metadata_server.py`
- Modify: `clash_sub/cli.py`
- Create: `deploy/systemd/clash-sub-metadata.socket`
- Create: `deploy/systemd/clash-sub-metadata.service`
- Modify: `tests/test_lightweight_metadata.py`
- Modify: `tests/test_lightweight_cli.py`
- Modify: `tests/test_lightweight_deployment.py`

**Interfaces:**

- Produces: 内部 `clash-sub metadata-serve` 入口，仅供 systemd 使用，不出现在菜单。
- Consumes: systemd 传入的 AF_UNIX 监听文件描述符。
- Produces: 规范 `Subscription-Userinfo` 与安全的 `X-Accel-Redirect`；无流量时只返回内部跳转。

- [ ] **Step 1: 写内部协议失败测试**

只接受 Nginx 固定发送的类型、整数客户端 ID 和受限内部静态标识。拒绝公网 URI、token、查询参数、换行和任意文件路径；错误响应不回显输入。

- [ ] **Step 2: 写 Socket 与 unit 失败测试**

Socket 固定为 `/run/clash-sub/metadata.sock`，父目录 `0750 root:www-data`，Socket `0660 root:www-data`，`Accept=no`。Service 不监听 TCP、不访问公网，仅允许 Unix Socket 和最小文件访问。

- [ ] **Step 3: 运行测试并确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_metadata tests.test_lightweight_cli tests.test_lightweight_deployment -v
```

- [ ] **Step 4: 实现最小 Socket 服务与 hardening**

unit 至少包含：

- `User=root`
- `Group=root`
- `NoNewPrivileges=true`
- `PrivateNetwork=true`
- `ProtectSystem=strict`
- `ProtectHome=true`
- `PrivateDevices=true`
- `RestrictAddressFamilies=AF_UNIX`
- `UMask=0077`
- 最小 `ReadOnlyPaths` 和仅覆盖缓存目录的 `ReadWritePaths`

请求日志不得含原始 URI、token、URL、正文或流量字段。

- [ ] **Step 5: 运行测试并确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_metadata tests.test_lightweight_cli tests.test_lightweight_deployment -v
```

- [ ] **Step 6: 提交**

```bash
git add clash_sub/metadata_server.py clash_sub/cli.py deploy/systemd/clash-sub-metadata.socket deploy/systemd/clash-sub-metadata.service tests/test_lightweight_metadata.py tests/test_lightweight_cli.py tests/test_lightweight_deployment.py
git commit -m "feat: serve metadata over systemd socket"
```

### Task 6: Nginx 动态响应头与静态文件降级

**Files:**

- Modify: `clash_sub/nginx.py`
- Modify: `tests/test_lightweight_nginx.py`
- Modify: `tests/test_lightweight_end_to_end.py`

**Interfaces:**

- Consumes: 元数据 Socket 内部协议。
- Produces: 公网精确路由先查询元数据，再通过 `X-Accel-Redirect` 由 Nginx 发送静态文件。
- Produces: 元数据服务连接失败、超时或 5xx 时，内部回退到同一静态文件。

- [ ] **Step 1: 写 Nginx 渲染失败测试**

要求 token、大小写文件名、GET/HEAD、空查询参数、限流和 owner/member 授权保持不变。公网请求头不得整体传入后端，内部位置必须为 `internal`。

- [ ] **Step 2: 写真实 Nginx 降级测试**

在可用环境验证正常请求包含 YAML 与流量头；Socket 不存在、服务超时或返回 5xx 时仍为 200 且正文相同，只省略流量头；内部静态 URI 公网访问为 404；伪造内部头无效。

- [ ] **Step 3: 运行测试并确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_nginx tests.test_lightweight_end_to_end -v
```

- [ ] **Step 4: 实现标准 proxy/X-Accel 链路**

不要使用 `auth_request`。使用标准 `proxy_pass`、`proxy_pass_request_headers off`、最多 1 秒连接/读取超时、`proxy_intercept_errors` 与内部错误页降级。Python 不读取或复制 YAML 正文。

主订阅保留 24 小时更新提示；机场 provider 已确认的七天刷新间隔不变。正常与降级响应均保留标题、Content-Disposition、安全头和 `Cache-Control: no-store`。

参考：<https://nginx.org/en/docs/http/ngx_http_proxy_module.html>

- [ ] **Step 5: 运行测试并确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_nginx tests.test_lightweight_end_to_end -v
```

- [ ] **Step 6: 提交**

```bash
git add clash_sub/nginx.py tests/test_lightweight_nginx.py tests/test_lightweight_end_to_end.py
git commit -m "feat: attach cached traffic to subscriptions"
```

### Task 7: 安装器、备份与旧机制移除

**Files:**

- Modify: `clash_sub/manage.py`
- Modify: `clash_sub/installer.py`
- Modify: `clash_sub/service.py`
- Delete: old scheduled-traffic service/timer files under `deploy/systemd/`
- Modify: `tests/test_lightweight_manage.py`
- Modify: `tests/test_lightweight_installer.py`
- Modify: `tests/test_lightweight_deployment.py`
- Modify: `tests/test_repository_safety.py`

**Interfaces:**

- Installs/enables: metadata socket。
- Removes from product: 旧定时流量单元、CLI 命令和 service 方法。
- Changes backup: 从四个重建文件增加为五个。

- [ ] **Step 1: 写安装和移除契约失败测试**

全新安装只安装并启用 metadata socket；不长期直接启用 metadata service；健康摘要检查 socket。安装回滚/卸载只处理新单元，不包含旧机制迁移。

- [ ] **Step 2: 写精确备份失败测试**

备份必须精确包含：

- 3x-ui 数据库
- 两份 Nginx 配置
- `/var/lib/clash-sub/private/state.json`
- `/var/lib/clash-sub/private/airport-source.json`

不得包含 `AmyTelecom.yaml`、`traffic-cache.json`、release、证书、systemd、日志或锁。任一必需文件缺失时返回 `backup_incomplete`。

- [ ] **Step 3: 写当前产品树清零守卫**

检查 `clash_sub/`、`deploy/`、`README.md`、`DEPLOYMENT.md`、`docs/operations.md`、`docs/template-design.md` 和运行时测试中不再存在旧定时流量单元名称或旧 CLI 命令。排除历史 `docs/superpowers/specs/` 与 `docs/superpowers/plans/`，不要删除历史设计记录。

- [ ] **Step 4: 运行测试并确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_manage tests.test_lightweight_installer tests.test_lightweight_deployment tests.test_repository_safety -v
```

- [ ] **Step 5: 删除旧机制并实现新安装/备份契约**

不保留兼容命令、旧单元清理或 post-update 迁移分支。测试服务器后续全新部署。

- [ ] **Step 6: 运行测试并确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_manage tests.test_lightweight_installer tests.test_lightweight_deployment tests.test_repository_safety -v
```

- [ ] **Step 7: 提交**

```bash
git add clash_sub/manage.py clash_sub/installer.py clash_sub/service.py deploy/systemd tests/test_lightweight_manage.py tests/test_lightweight_installer.py tests/test_lightweight_deployment.py tests/test_repository_safety.py
git commit -m "refactor: replace scheduled traffic refresh"
```

### Task 8: 中文文档与本地完整验证

**Files:**

- Modify: `README.md`
- Modify: `DEPLOYMENT.md`
- Modify: `docs/operations.md`
- Modify: `docs/template-design.md`
- Modify: 其他受本设计直接影响的测试

- [ ] **Step 1: 更新中文文档**

文档面向项目所有者本人，保持直接但完整。写清五分钟按需缓存、故障降级、机场两个更新操作、来源文件权限与备份、机场流量刷新边界、metadata Socket、全新部署和恢复步骤。

- [ ] **Step 2: 运行全部测试与扫描**

```bash
.venv/bin/python -m unittest discover
.venv/bin/python -m compileall -q clash_sub scripts tests
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
node --check private/clash-verge-home.js
git diff --check
git status --short
```

只允许已有、理由明确的可选 Mihomo 真机测试 skip。任何真实失败必须修复后重新完整运行。

- [ ] **Step 3: 审查当前产品树的旧机制残留**

使用 `rg` 搜索生产代码、部署文件、当前用户文档和运行时测试；历史设计/计划目录不在清零范围。不得为了通过扫描删除历史设计文档。

- [ ] **Step 4: 提交文档与最终本地修正**

```bash
git add README.md DEPLOYMENT.md docs/operations.md docs/template-design.md clash_sub tests deploy
git commit -m "docs: describe on-demand subscription metadata"
```

### Task 9: 测试服务器全新部署验收

**Files:** None in the first step; deployment findings may require a separate tested code fix commit.

**Authorization:** 执行任何服务器写入、清理或全新部署前，先向用户说明将改变的状态并取得明确许可。服务器连接资料由用户在会话中提供，不得写入仓库文件。

- [ ] **Step 1: 取得测试服务器全新部署授权**

没有授权时停止本任务，并在报告明确标记“服务器验收未执行”；不得把本地模拟写成真实验收。

- [ ] **Step 2: 全新部署并检查 systemd/Nginx**

确认系统不存在旧流量定时单元，只存在 metadata socket/service；Socket 能按请求激活；Nginx 配置检查通过。

- [ ] **Step 3: 验证 3x-ui 缓存链路**

首次请求触发一次数据库读取；五分钟内连续刷新 Compat/Balance 不重复读取；五分钟后刷新显示新流量；不得输出真实 token 或 URL。

- [ ] **Step 4: 验证静态降级**

停止或隔离 metadata 服务后，现有主 YAML 仍可下载；恢复服务后流量头重新出现。降级测试不得修改发布文件。

- [ ] **Step 5: 验证机场链路**

更换链接成功保存来源、正文和流量；保存链接刷新不要求再次输入；直接导入服务器 `AmyTelecom.yaml` 能显示最近保存流量；proxy-provider 请求不访问机场上游。

- [ ] **Step 6: 验证授权矩阵**

owner 两个主配置与机场路由有效；普通用户只有 Compat，不能访问 Balance 或机场；文件名和标题大小写严格不变。

- [ ] **Step 7: 对服务器发现执行 TDD 修复**

若发现真实缺陷，先新增本地回归测试、确认 RED，再修复、运行聚焦与全量测试并单独提交。禁止在服务器上直接热改后不回写仓库。

### Task 10: 完整 diff 自审与交付

**Files:** Review only unless a concrete defect requires a tested fix.

- [ ] **Step 1: 审查基线到最终 HEAD**

```bash
git diff --stat 7be4095...HEAD
git diff 7be4095...HEAD
git log --oneline 7be4095..HEAD
```

逐项确认：

- 没有重新引入机场内容解析或校验。
- 没有把机场 URL、token 或正文写入输出、日志、缓存或提交。
- metadata 服务不是 YAML 可用性的硬依赖。
- 流量变化不会生成 release 或重载 Nginx。
- 当前产品树无旧定时流量机制和旧 CLI 命令。
- member 无 Balance/机场权限。
- 模板通用注释没有丢失。
- 文件名、标题与大小写未改变。
- 没有无关重构或兼容分支。

- [ ] **Step 2: 再次运行完整验证**

提交任何审查修复后，重新运行 Task 8 的完整测试、扫描、compileall、Node 检查和 `git diff --check`。

- [ ] **Step 3: 确认工作区状态**

`git status --short` 必须为空；如存在用户原有文件或有意保留的未跟踪产物，逐项说明，不能笼统宣称干净。

- [ ] **Step 4: 提交最终报告，不推送或合并**

报告必须包含：

1. 分支名、实际基线和最终提交号。
2. 每个提交的摘要。
3. 新增、修改、删除文件清单。
4. 3x-ui 请求缓存链路说明。
5. 机场链接与流量事务说明。
6. Nginx 正常路径和故障降级说明。
7. 聚焦测试、全量测试、秘密扫描与静态检查结果。
8. 测试服务器真实验收结果；未授权则明确写未执行。
9. 完整 diff 自审发现及修复。
10. 尚存风险和无法验证事项。
11. `git status --short` 结果。

将最终提交号和报告交回用户，由 Sol 做最终独立审查。
