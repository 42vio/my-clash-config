# 机场订阅页面自动刷新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Debian 服务器上加入纯 HTTP 的机场订阅开关页面自动化，优先复用已保存订阅链接，失败时生成新链接，并保留人工兜底与现有双文件恢复事务。

**Architecture:** 新建 `AirportPortalClient`，只负责开启页面和生成真实订阅链接；`ClashSubService` 负责菜单状态机，并复用现有下载器和 `AirportStore`。来源记录升级为 Schema v2，新增每 7 天执行的 systemd timer；客户端 provider 每 24 小时从自有订阅服务器刷新。

**Tech Stack:** Python 3 标准库（`urllib`、`html.parser`、`json`）、现有 `unittest` 测试体系、systemd、现有原子文件事务。

**Spec:** `docs/superpowers/specs/2026-09-01-airport-portal-refresh-design.md`

## Global Constraints

- 不使用浏览器、无头浏览器、JavaScript 执行器或新增第三方运行依赖。
- 页面 URL 与真实订阅 URL 只接受 HTTPS，禁止 URL 用户名、密码和 fragment。
- 页面最多 1 MiB，机场正文最多 5 MiB，单次网络请求超时 15 秒，最多 3 次 HTTPS 重定向。
- 机场正文不做 YAML 解析；只拒绝空响应、超限响应、HTML MIME 和明显 HTML 开头。
- 下载正文按原始字节保存，不改变注释、顺序、缩进或换行。
- URL 只能从可见交互输入；不得增加携带 URL 的命令行参数。
- 菜单名称与顺序固定为：设置订阅开关页面、自动开启订阅并刷新、手动开启订阅后刷新、使用新订阅链接更新、查看机场状态、返回。
- 不记录开关持续时间；每次自动刷新只访问一次开关页面。
- 来源记录固定为 `/var/lib/clash-sub/private/airport-source.json`，`0600 root:root`；provider 固定为 `/var/lib/clash-sub/public/provider/AmyTelecom.yaml`，`0640 root:www-data`。
- `public/provider` 必须精确为 `02750 root:www-data`。
- 双文件正式切换顺序保持 provider 后 source；允许两次替换之间的一次并发请求短暂取得新正文与旧流量头。
- owner/member 发布矩阵、Home 脚本、模板注释、Nginx 路由和备份文件数量不变。
- 主配置刷新头保持 24 小时；`AmyTelecom` provider 间隔改为 `86400` 秒；服务器自动刷新每 7 天一次，随机延迟 0–6 小时。
- 自动刷新不执行 `sync`、不读取 3x-ui、不运行 Mihomo、不创建 release、不重载 Nginx。
- 所有错误、测试 fixture 和命令输出不得包含真实 URL、Token、页面 HTML、接口响应或机场正文。
- 项目按全新部署处理，不实现旧流量定时单元或 Schema v1 的迁移兼容。

## File Structure

### 新建文件

- `clash_sub/airport_portal.py`：页面开启、HTML 解析、生成任务轮询、页面错误码与单次 Cookie 会话。
- `tests/test_lightweight_airport_portal.py`：页面协议、同源约束、边界限制和脱敏测试。
- `deploy/systemd/clash-sub-airport-refresh.service`：无交互自动刷新 oneshot 服务。
- `deploy/systemd/clash-sub-airport-refresh.timer`：7 天周期与 0–6 小时随机延迟。

### 修改文件

- `clash_sub/airport_source.py`：Schema v2 与 `activation_url`。
- `clash_sub/sources.py`：拒绝明显 HTML，继续保留原始字节。
- `clash_sub/airport_store.py`：写入前严格检查 provider 目录模式。
- `clash_sub/service.py`：四种刷新路径、自动降级状态机与机场状态摘要。
- `clash_sub/runtime.py`：注入 `AirportPortalClient`。
- `clash_sub/cli.py`：五项机场菜单及隐藏的定时入口。
- `clash_sub/generator.py`、`clash_sub/checks.py`：provider 间隔统一为 86400。
- `clash_sub/installer.py`、`clash_sub/manage.py`：安装、post-update、回滚和卸载管理新增 systemd 单元。
- `clash_sub/metadata.py`：确认 Schema v2 读取仍只暴露流量。
- `README.md`、`DEPLOYMENT.md`、`docs/operations.md`、`docs/template-design.md`：更新当前操作口径。
- 现有对应测试文件：更新模型构造、CLI fixture、安装事务、周期、端到端和安全守卫。

---

### Task 1: 来源记录升级为 Schema v2

**Files:**
- Modify: `clash_sub/airport_source.py`
- Modify: `clash_sub/metadata.py`
- Test: `tests/test_lightweight_airport_source.py`
- Test: `tests/test_lightweight_metadata.py`
- Test: `tests/test_lightweight_airport_store.py`
- Test: `tests/test_lightweight_service.py`
- Test: `tests/test_lightweight_end_to_end.py`

**Interfaces:**
- Produces: `AirportSource(source_url: str, traffic: Traffic | None, last_success: int, activation_url: str | None)`。
- Produces: JSON 精确键集合 `activation_url,last_success,schema_version,source_url,traffic`，`schema_version == 2`。
- Consumes: 现有 `Traffic` 与 `AirportStore.replace(document, source)`。

- [ ] **Step 1: 写 Schema v2 失败测试**

在 `tests/test_lightweight_airport_source.py` 把统一 fixture 改为关键字构造，并断言 v2：

```python
ACTIVATION_URL = "https://example.invalid/Subscription/index?sid=placeholder&token=placeholder"

def sample_source():
    return AirportSource(
        source_url=SOURCE_URL,
        traffic=SOURCE_TRAFFIC,
        last_success=LAST_SUCCESS,
        activation_url=ACTIVATION_URL,
    )

def test_serialize_uses_the_exact_fixed_schema(self):
    self.assertEqual(json.loads(serialize_source(sample_source())), {
        "schema_version": 2,
        "activation_url": ACTIVATION_URL,
        "source_url": SOURCE_URL,
        "traffic": {"upload": 1, "download": 2, "total": 3, "expire": 4},
        "last_success": LAST_SUCCESS,
    })
```

增加 `activation_url=None` 往返测试、非空非字符串拒绝测试、缺键/多键拒绝测试，并把 Schema v1 放入明确拒绝列表。同步修改所有测试里的 `AirportSource(...)`，统一使用关键字参数，避免四个字段的顺序错误。

- [ ] **Step 2: 运行模型和依赖测试确认 RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_lightweight_airport_source tests.test_lightweight_airport_store tests.test_lightweight_metadata tests.test_lightweight_service tests.test_lightweight_end_to_end
```

Expected: FAIL，首个失败应来自缺少 `activation_url` 或 schema 仍为 1，而不是 fixture 语法错误。

- [ ] **Step 3: 实现精确 Schema v2**

在 `clash_sub/airport_source.py` 使用以下模型与键集合：

```python
SCHEMA_VERSION = 2
_RECORD_KEYS = frozenset({
    "activation_url", "last_success", "schema_version", "source_url", "traffic"
})

@dataclass(frozen=True)
class AirportSource:
    source_url: str
    traffic: Traffic | None
    last_success: int
    activation_url: str | None
```

`serialize_source()` 和 `parse_source()` 必须接受 `activation_url is None`，否则要求非空字符串；继续严格拒绝未知键、Schema v1、布尔整数、负流量和非法时间。`metadata.py::airport_traffic()` 不改变返回接口，只通过新版 `read_source_file()` 读取 `traffic`。

- [ ] **Step 4: 运行聚焦测试确认 GREEN**

Run: 与 Step 2 相同。

Expected: 全部 PASS，且 `metadata` 测试证明 `activation_url` 不进入 HTTP 响应或公开状态。

- [ ] **Step 5: 提交**

```bash
git add clash_sub/airport_source.py clash_sub/metadata.py tests/test_lightweight_airport_source.py tests/test_lightweight_airport_store.py tests/test_lightweight_metadata.py tests/test_lightweight_service.py tests/test_lightweight_end_to_end.py
git commit -m "refactor: upgrade airport source record"
```

---

### Task 2: 增加基础机场响应检查

**Files:**
- Modify: `clash_sub/sources.py`
- Test: `tests/test_lightweight_sources.py`

**Interfaces:**
- Consumes: `download_airport_document(url, max_bytes, opener=None)`。
- Produces: HTML 响应抛出 `SourceError("airport_response_invalid")`；其他非空非超限字节继续原样返回 `AirportDownload`。

- [ ] **Step 1: 写 HTML 拒绝与原字节保留测试**

新增参数化子测试覆盖 MIME 与正文特征：

```python
def test_airport_download_rejects_obvious_html_without_echoing_the_body(self):
    cases = (
        (b"proxies: []\n", "text/html"),
        (b"\xef\xbb\xbf  <!DOCTYPE html><title>login</title>", "text/plain"),
        (b"\n<HTML><body>expired</body></HTML>", None),
        (b" <head><title>error</title></head>", "application/octet-stream"),
        (b"<body>not enabled</body>", "text/plain"),
    )
    for body, content_type in cases:
        response = FakeResponse(body, "https://airport.example/final")
        if content_type is not None:
            response.headers["Content-Type"] = content_type
        with self.assertRaises(SourceError) as caught:
            download_airport_document(
                "https://airport.example/private-token", 1024,
                opener=self.opener_for(response),
            )
        self.assertEqual(str(caught.exception), "airport_response_invalid")
        self.assertNotIn("expired", str(caught.exception))
```

保留并强化现有“非 YAML 字节可接受”测试，例如 `b"not yaml but upstream-owned\n"` 必须逐字节返回。

- [ ] **Step 2: 运行下载测试确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_sources
```

Expected: 新 HTML 用例 FAIL，现有传输、重定向、大小和流量头用例保持 PASS。

- [ ] **Step 3: 实现最小响应分类**

在读取最终响应时只读取最终 `Content-Type`，在正文完整读入且通过大小/非空检查后调用私有函数：

```python
_HTML_PREFIXES = (b"<!doctype html", b"<html", b"<head", b"<body")

def _reject_obvious_html(body, content_type):
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    prefix = body.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    if media_type in {"text/html", "application/xhtml+xml"}:
        raise SourceError("airport_response_invalid")
    if prefix.startswith(_HTML_PREFIXES):
        raise SourceError("airport_response_invalid")
```

不得调用 `yaml.safe_load`、不得检查文件扩展名、不得要求 `application/yaml`。返回值继续携带原 `body` 对象。

- [ ] **Step 4: 运行下载测试确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_sources
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add clash_sub/sources.py tests/test_lightweight_sources.py
git commit -m "feat: reject obvious airport html responses"
```

---

### Task 3: 收紧 provider 目录安全检查

**Files:**
- Modify: `clash_sub/airport_store.py`
- Test: `tests/test_lightweight_airport_store.py`

**Interfaces:**
- Produces: `AirportStore.replace/read/recover` 在 provider 目录模式不是 `02750` 时抛出 `AirportStoreError("airport_provider_invalid")`。

- [ ] **Step 1: 写模式篡改失败测试**

```python
def test_provider_directory_requires_exact_setgid_mode(self):
    for mode in (0o750, 0o775, 0o2770, 0o2775):
        with self.subTest(mode=oct(mode)):
            os.chmod(self.provider_directory, mode)
            with self.assertRaises(AirportStoreError) as caught:
                self.store.replace(PROVIDER_DOCUMENT, sample_source())
            self.assertEqual(caught.exception.code, "airport_provider_invalid")
            os.chmod(self.provider_directory, 0o2750)
```

同时断言现有文件未覆盖、未留下 candidate/backup/journal。

- [ ] **Step 2: 运行存储测试确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_airport_store
```

Expected: 新增的 `0750`/可组写目录用例 FAIL。

- [ ] **Step 3: 增加精确模式守卫**

在 `_require_provider_directory()` 现有类型、uid、gid 条件中加入：

```python
or stat.S_IMODE(details.st_mode) != 0o2750
```

不要修改 installer 的目标模式；它已经创建 `02750`。

- [ ] **Step 4: 运行存储测试确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_airport_store
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add clash_sub/airport_store.py tests/test_lightweight_airport_store.py
git commit -m "fix: enforce airport provider directory mode"
```

---

### Task 4: 实现纯 HTTP 页面适配器

**Files:**
- Create: `clash_sub/airport_portal.py`
- Create: `tests/test_lightweight_airport_portal.py`

**Interfaces:**
- Produces: `AirportPortalError(code: str)`，字符串只等于稳定错误码。
- Produces: `AirportPortalClient.activate(activation_url: str) -> AirportPortalPage`。
- Produces: `AirportPortalClient.generate_source_url(page: AirportPortalPage) -> str`。
- `AirportPortalPage` 只在内存保存同源、Cookie opener、生成字段和 0–30 秒等待时间；其 `repr` 不得包含任何字段值。

- [ ] **Step 1: 建立 Fake opener 和正常协议失败测试**

用占位 URL 与合成 HTML 覆盖两种成功协议：第一次 POST 直接返回 `url:`；第一次返回 `subid:`，等待后第二次返回 `url:`。测试接口形状：

```python
client = AirportPortalClient(opener_factory=lambda: opener, sleeper=sleeps.append)
page = client.activate(ACTIVATION_URL)
source_url = client.generate_source_url(page)
self.assertEqual(source_url, "https://portal.example/generated-placeholder")
self.assertEqual(sleeps, [8])
```

Fake response 必须支持 `read(limit)`, `geturl()`, context manager、headers；Fake opener 记录请求 method、URL 和 form body，但断言失败消息中不出现这些内容。

- [ ] **Step 2: 写安全边界失败测试**

逐项覆盖：

- activation URL 非 HTTPS、含 userinfo、含 fragment；
- 页面重定向到 HTTP、跨域或超过三次；
- 页面超过 1 MiB；
- 缺少/重复 `id=Clash1_Anyttls`；
- 按钮路径不是同源指定 Clash 入口，订阅类型不是 `anytls_clash`；
- `sid/token/pid/delaytime` 缺失、重复、类型错误；
- `delaytime` 小于 0 或大于 30；
- JSON 超过 4 KiB、非对象、非法 `result/msg`、任务编号非法；
- 生成链接非 HTTPS、跨域、含 userinfo 或 fragment；
- GET/POST 网络失败映射为 `airport_portal_unavailable` 或 `airport_link_generation_failed`；
- 页面结构错误映射为 `airport_portal_unsupported`；
- Cookie 只在同一次 `activate`/`generate_source_url` 会话中复用，下一次 `activate` 使用新 opener。

每个错误断言 `str(error) == error.code`，并扫描异常、`repr(page)` 和测试输出不包含 fixture 中的 sid/token/url/path。

- [ ] **Step 3: 运行适配器测试确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_airport_portal
```

Expected: FAIL with `ModuleNotFoundError: clash_sub.airport_portal`。

- [ ] **Step 4: 实现页面读取与严格解析**

使用 `urllib.request.build_opener(ProxyHandler({}), redirect_handler, HTTPCookieProcessor(CookieJar()))` 建立每次操作独立会话。使用标准库 `HTMLParser` 精确识别唯一 `Clash1_Anyttls` 按钮；动态字段保持为私有 tuple，不写磁盘、不写异常。核心公开形状：

```python
class AirportPortalError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

class AirportPortalClient:
    def __init__(self, opener_factory=None, sleeper=None): ...
    def activate(self, activation_url): ...
    def generate_source_url(self, page): ...
```

GET 使用 15 秒超时，最多读取 `1 MiB + 1`；POST 使用 `application/x-www-form-urlencoded`，最多读取 `4 KiB + 1`。只接受已验证页面提供的固定字段，第一次返回任务编号时按 0–30 秒等待一次并发第二次 POST；不得循环轮询。

- [ ] **Step 5: 运行适配器测试确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_airport_portal
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add clash_sub/airport_portal.py tests/test_lightweight_airport_portal.py
git commit -m "feat: add airport portal client"
```

---

### Task 5: 接入服务状态机与运行时工厂

**Files:**
- Modify: `clash_sub/service.py`
- Modify: `clash_sub/runtime.py`
- Test: `tests/test_lightweight_service.py`
- Test: `tests/test_lightweight_end_to_end.py`

**Interfaces:**
- Consumes: `AirportPortalClient.activate()` 与 `generate_source_url()`。
- Produces: `configure_airport_portal(activation_url: str)`。
- Produces: `auto_refresh_airport()`。
- Preserves: `refresh_airport()` 作为菜单 3 的纯旧链接刷新。
- Preserves: `replace_airport_source(source_url)` 作为菜单 4，并保留现有 `activation_url`。
- Produces: 成功结果 `{"updated": True, "traffic_captured": bool}`；正常跳过 `{"updated": False, "skipped": True}`。

- [ ] **Step 1: 扩展服务 fixture 并写菜单 1 全链路测试**

给 `ClashSubService` 注入 `airport_portal` fake。测试 `configure_airport_portal()` 必须：每次 activate 后强制 generate；生成链接下载成功后才调用 store；写入：

```python
AirportSource(
    source_url=generated_url,
    traffic=downloaded.traffic,
    last_success=int(clock()),
    activation_url=input_activation_url,
)
```

页面、生成、下载或 store 任一步失败时，旧 provider 和旧 source 完整保留；不读取 x-ui、不 render、不运行 Mihomo、不创建 release、不激活 Nginx。

- [ ] **Step 2: 写菜单 2 自动状态机测试**

至少覆盖以下调用顺序：

```text
页面成功 + 旧链接成功：activate → download(old) → replace，不 generate
页面成功 + 旧链接失败：activate → download(old) → generate → download(new) → replace
页面不可用 + 旧链接成功：activate(fail) → download(old) → replace
页面不可用 + 旧链接失败：返回 skipped，store 不调用
页面不兼容 + 旧链接成功：download(old) 成功并更新
页面不兼容 + 旧链接失败：抛 airport_portal_unsupported
activation_url=None：抛 airport_activation_missing
```

旧链接成功时保留原 `source_url` 和 `activation_url`；新链接成功时只替换 `source_url`。任一成功下载都以本次 traffic 覆盖旧 traffic，缺失时明确写 `None`。

- [ ] **Step 3: 写菜单 3、菜单 4 与状态测试**

- `refresh_airport()` 不调用 portal，保留两个 URL，只更新正文、traffic、last_success；
- `replace_airport_source(new_url)` 不调用 portal，保留旧 `activation_url`；没有旧记录时使用 `activation_url=None`；
- `airport_status()` 返回 `activation_configured`、`source_configured`、`activation_host`、`source_host`、流量字段、成功时间与 provider 存在状态；
- 状态字典不包含 URL、path、query、token、task id；
- `airport_response_invalid` 在自动路径中与下载失败相同地触发 fallback。

- [ ] **Step 4: 运行服务测试确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_service tests.test_lightweight_end_to_end
```

Expected: FAIL，原因是新构造参数、方法或状态键尚不存在。

- [ ] **Step 5: 实现单一保存辅助函数与状态机**

在 `service.py` 内增加私有保存辅助，避免四个入口复制来源构造：

```python
def _replace_airport(self, downloaded, *, source_url, activation_url):
    source = AirportSource(
        source_url=source_url,
        traffic=downloaded.traffic,
        last_success=int(self._clock()),
        activation_url=activation_url,
    )
    self._airport.replace(downloaded.document, source)
    return {"updated": True, "traffic_captured": downloaded.traffic is not None}
```

在 `__init__` 增加必填注入 `airport_portal`，由 `runtime.build_service()` 构造 `AirportPortalClient()`。仅捕获 `AirportPortalError`、`SourceError` 和 `AirportStoreError` 并映射到规格错误码；任何异常文本都不得拼接进 `ServiceError`。

- [ ] **Step 6: 运行服务测试确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_service tests.test_lightweight_end_to_end
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add clash_sub/service.py clash_sub/runtime.py tests/test_lightweight_service.py tests/test_lightweight_end_to_end.py
git commit -m "feat: add airport refresh state machine"
```

---

### Task 6: 更新机场菜单与隐藏定时入口

**Files:**
- Modify: `clash_sub/cli.py`
- Test: `tests/test_lightweight_cli.py`

**Interfaces:**
- Consumes: Task 5 的四个服务入口和状态摘要。
- Produces: 隐藏命令 `clash-sub airport-scheduled-refresh`，不接受参数。

- [ ] **Step 1: 写精确菜单与输入测试**

菜单文本必须逐字匹配：

```text
机场订阅

1. 设置订阅开关页面
2. 自动开启订阅并刷新（推荐）
3. 手动开启订阅后刷新
4. 使用新订阅链接更新
5. 查看机场状态
0. 返回

请输入选项 [0-5]：
```

菜单 1 先显示全链路说明，再以 `请输入订阅开关页面：` 可见读取；菜单 4 先说明不会访问开关页面，再以 `请输入新的机场订阅链接：` 可见读取。空输入不得构造 service；所有 URL 不得出现在 stdout/stderr。

- [ ] **Step 2: 写菜单 2、3、5 与定时命令测试**

- 菜单 2 调用 `auto_refresh_airport()`；
- 菜单 2 遇到 `airport_activation_missing` 时显示“尚未设置订阅开关页面，请先使用菜单 1。”并留在机场子菜单，不按普通失败退出；
- 菜单 3 显示人工开启提示，回车才调用 `refresh_airport()`，输入 `0` 取消；提示不得显示已保存 URL；
- 菜单 5 只显示是否配置、两个主机名、流量、到期、最近成功与 provider 状态；
- `airport-scheduled-refresh` 成功或返回 `skipped` 时 exit 0；
- 该命令遇到 `operation_busy`、`airport_activation_missing` 或 `airport_refresh_skipped` 时 exit 0；
- 页面结构不兼容、store 错误等真实失败时 exit 1；
- `airport-scheduled-refresh anything` 必须以 `invalid_command` 拒绝，且不回显参数；
- 菜单和命令都不调用 `sync`。

- [ ] **Step 3: 运行 CLI 测试确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_cli
```

Expected: 新菜单和新命令测试 FAIL，既有非机场 CLI 回归继续通过。

- [ ] **Step 4: 实现菜单分发和隐藏入口**

把 `_AIRPORT_MENU_ROWS` 扩到 0–5，新增独立的 `_menu_airport_portal()`、`_menu_airport_manual_refresh()` 和 `_menu_airport_source()`；不要用 `getpass`。在 parser 增加无参数内部命令：

```python
# Internal systemd entry; never displayed in menus.
commands.add_parser("airport-scheduled-refresh", add_help=False)
```

内部命令直接构造 service 并调用 `auto_refresh_airport()`；正常 skip 不写错误。交互菜单 2 遇到页面临时不可用且旧链接失败时显示一次“本次已跳过，当前机场配置保持不变”，但不显示具体 URL。

- [ ] **Step 5: 运行 CLI 测试确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_cli
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add clash_sub/cli.py tests/test_lightweight_cli.py
git commit -m "feat: expose airport refresh workflows"
```

---

### Task 7: 将客户端 provider 刷新周期改为 24 小时

**Files:**
- Modify: `clash_sub/generator.py`
- Modify: `clash_sub/checks.py`
- Modify: `tests/test_lightweight_generator.py`
- Modify: `tests/test_lightweight_checks.py`
- Modify: `tests/test_lightweight_end_to_end.py`

**Interfaces:**
- Produces: owner `proxy-providers.AmyTelecom.interval == 86400`。
- Preserves: member 不含 `AmyTelecom`；主配置响应头仍为 `Profile-Update-Interval: 24`。

- [ ] **Step 1: 把测试期望改为 86400 并保持错误守卫**

```python
self.assertEqual(owner["proxy-providers"]["AmyTelecom"]["interval"], 86400)
```

将 checks 中“non-weekly interval”表述改为“wrong daily interval”，继续拒绝 `3600`、`604800`、布尔值和缺失 interval。

- [ ] **Step 2: 运行生成与校验测试确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_generator tests.test_lightweight_checks tests.test_lightweight_end_to_end
```

Expected: 仅 604800/86400 期望相关用例 FAIL。

- [ ] **Step 3: 修改生产常量**

将 `generator.py::_with_provider()` 和 `checks.py::_validate_proxy_providers()` 中固定值从 `604800` 改为 `86400`。不要改变 provider URL、path 或 owner/member 注入逻辑。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: 与 Step 2 相同。

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add clash_sub/generator.py clash_sub/checks.py tests/test_lightweight_generator.py tests/test_lightweight_checks.py tests/test_lightweight_end_to_end.py
git commit -m "refactor: refresh airport provider daily"
```

---

### Task 8: 安装并管理每周自动刷新 timer

**Files:**
- Create: `deploy/systemd/clash-sub-airport-refresh.service`
- Create: `deploy/systemd/clash-sub-airport-refresh.timer`
- Modify: `clash_sub/installer.py`
- Modify: `clash_sub/manage.py`
- Modify: `tests/test_lightweight_deployment.py`
- Modify: `tests/test_lightweight_installer.py`
- Modify: `tests/test_lightweight_manage.py`

**Interfaces:**
- Consumes: `clash-sub airport-scheduled-refresh`。
- Produces: timer 每周执行，`RandomizedDelaySec=6h`、`Persistent=true`；只启用 timer。

- [ ] **Step 1: 写 systemd 资产守卫测试**

`tests/test_lightweight_deployment.py` 必须断言：

```text
service: Type=oneshot
service: ExecStart=/usr/local/bin/clash-sub airport-scheduled-refresh
service: Wants/After=network-online.target
service: TimeoutStartSec=180
service: User=root, UMask=0077, NoNewPrivileges=true
service: ProtectSystem=strict, ProtectHome=true, PrivateTmp=true
service: ReadWritePaths 仅含 private 与 public/provider
timer: OnCalendar=weekly
timer: RandomizedDelaySec=6h
timer: Persistent=true
timer: WantedBy=timers.target
```

同时断言 service 没有 `[Install]`，不使用 `PrivateNetwork=true`，timer 不携带 URL。

- [ ] **Step 2: 写 installer 与 post-update 事务测试**

覆盖：

- fresh install 写入两个 unit，daemon-reload 后只执行 `enable --now clash-sub-airport-refresh.timer`，不 enable service；
- 捕获 timer 原 active/enabled 状态后才写文件；
- 任一 unit 写入、daemon-reload 或 enable 失败时恢复原文件与原 timer 状态；
- 回滚 fresh install 时 disable/stop timer 并删除本项目写入文件；
- 已有外部 unit 时按现有 `replaced_files` 机制字节级恢复；
- post-update 幂等重写两个 unit 并保持 timer 启用；
- 自定义 private/public root 正确渲染到 `ReadWritePaths`；
- 卸载/资产清单包含两个新增 unit；
- timer 状态字段通过安装日志严格校验，不接受非布尔值。

- [ ] **Step 3: 运行部署测试确认 RED**

```bash
.venv/bin/python -m unittest tests.test_lightweight_deployment tests.test_lightweight_installer tests.test_lightweight_manage
```

Expected: FAIL，原因是 unit 不存在、installer 未管理 timer。

- [ ] **Step 4: 创建加固的 service 和 timer**

service 的核心内容：

```ini
[Unit]
Description=Refresh the saved airport subscription
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/clash-sub airport-scheduled-refresh
TimeoutStartSec=180
User=root
Group=root
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/var/lib/clash-sub/private /var/lib/clash-sub/public/provider
```

timer 的核心内容：

```ini
[Unit]
Description=Refresh the saved airport subscription every seven days

[Timer]
OnCalendar=weekly
RandomizedDelaySec=6h
Persistent=true
Unit=clash-sub-airport-refresh.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 5: 扩展 installer 的写前日志和回滚**

在 `InstallState` 增加并严格校验：

```python
timer_enable_attempted: bool = False
timer_active: bool | None = None
timer_enabled: bool | None = None
timer_state_captured: bool = False
```

`harden_systemd()` 将两个 unit 纳入 `units` 和 write-ahead 文件记录；在任何文件写入前查询 timer 状态并保存。启用阶段依次启用 metadata socket 和 airport timer。rollback 先禁用本次 timer，再恢复文件，daemon-reload 后按捕获状态恢复原 timer。`_render_systemd_unit()` 需要同时替换默认 private root 和 public root sentinel，仍须在变更前拒绝换行、相对路径和缺失 sentinel。

- [ ] **Step 6: 更新 post-update 与资产清单**

`manage.run_post_update()` 继续调用 `_prepare_runtime_directories()` 后再 `harden_systemd()`；测试证明旧服务器缺失 provider 目录时先创建为 `02750 root:www-data`，再启用 timer。`InstallPaths.artifacts()`/卸载列表加入两个 unit，不增加旧流量 timer 的兼容删除逻辑。

- [ ] **Step 7: 运行部署测试确认 GREEN**

```bash
.venv/bin/python -m unittest tests.test_lightweight_deployment tests.test_lightweight_installer tests.test_lightweight_manage
```

Expected: 全部 PASS。

- [ ] **Step 8: 提交**

```bash
git add deploy/systemd/clash-sub-airport-refresh.service deploy/systemd/clash-sub-airport-refresh.timer clash_sub/installer.py clash_sub/manage.py tests/test_lightweight_deployment.py tests/test_lightweight_installer.py tests/test_lightweight_manage.py
git commit -m "feat: schedule weekly airport refresh"
```

---

### Task 9: 更新个人运维文档与端到端契约

**Files:**
- Modify: `README.md`
- Modify: `DEPLOYMENT.md`
- Modify: `docs/operations.md`
- Modify: `docs/template-design.md`
- Modify: `tests/test_repository_safety.py`
- Modify: `tests/test_secret_scan.py`
- Modify: `tests/test_lightweight_end_to_end.py`

**Interfaces:**
- Consumes: Tasks 1–8 的最终用户行为。
- Produces: 只描述当前个人服务器维护路径的中文文档。

- [ ] **Step 1: 更新首次部署和日常机场操作**

文档必须写明：

- 首次使用菜单 1 设置开关页面；该操作会生成新链接、下载成功后才同时保存两个 URL 和 provider；
- 菜单 2 是日常推荐操作：开启页面后先用旧链接，旧链接失败才生成新链接；页面临时不可用时仍尝试旧链接；
- 菜单 3 只用于用户已在浏览器手动开启订阅后刷新旧链接；
- 菜单 4 是页面改版时永久可用的人工兜底，并保留已有开关页面；
- 菜单 5 只显示两个来源主机和公开摘要；
- 每次自动刷新都会尝试开启页面，不记录持续开启时间；
- 服务器每 7 天刷新，客户端 provider 每 24 小时刷新，主配置每 24 小时刷新；
- 机场正文不解析 YAML，只拒绝空、超限和明显 HTML；坏的上游内容需要通过菜单 2 或 4 覆盖；
- 双文件事务不是严格的跨路径原子可见，允许毫秒级新正文/旧流量头窗口；
- 备份仍是五个文件，`airport-source.json` 同时保存 activation/source URL，`AmyTelecom.yaml` 不进备份。

删掉所有“provider 自动刷新 7 天”“更换机场订阅链接/刷新机场订阅/查看机场状态三项菜单”“来源记录只有一个 URL”的旧表述。不要写入本次设计过程或未来构想。

- [ ] **Step 2: 更新模板和安全守卫测试**

把 `docs/template-design.md` 的 provider 示例改为 `interval: 86400`。安全测试需要证明：

- 仓库没有真实 `sid/token` 页面 URL；
- CLI 不接受 activation/source URL 参数；
- 公开状态、异常与 fixture 不含长 token；
- 新 systemd unit 不含凭据；
- README/手册中的 URL 只使用 `.invalid` 或明确占位域名。

- [ ] **Step 3: 增加端到端机场降级测试**

在现有 harness 中完成至少一条完整链路：Schema v2 来源 → 页面成功 → 旧链接失败 → 生成新链接 → 原字节 provider 与新 source 一起保存 → metadata 请求返回新 traffic → `sync` 不被调用。再覆盖页面不可用 + 旧链接成功，确认链接与 activation URL 保留。

- [ ] **Step 4: 运行文档与端到端测试**

```bash
.venv/bin/python -m unittest tests.test_lightweight_end_to_end tests.test_repository_safety tests.test_secret_scan
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add README.md DEPLOYMENT.md docs/operations.md docs/template-design.md tests/test_lightweight_end_to_end.py tests/test_repository_safety.py tests/test_secret_scan.py
git commit -m "docs: document airport portal refresh"
```

---

### Task 10: 全量验证与最终审查

**Files:**
- Review: all files changed by Tasks 1–9

**Interfaces:**
- Produces: 可交给 Sol 最终审查的干净提交序列和验证报告。

- [ ] **Step 1: 运行完整测试**

```bash
.venv/bin/python -m unittest discover
```

Expected: 全部 PASS；只有仓库原有的可选 `MIHOMO_BIN` 真机测试可以 skip。任何真实失败都必须回到对应 Task 修复并重跑聚焦测试。

- [ ] **Step 2: 运行编译、秘密扫描和差异检查**

```bash
.venv/bin/python -m compileall -q clash_sub scripts tests
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
git diff --check
```

Expected: 两次秘密扫描均 clean，compileall 和 diff check 均 exit 0。

- [ ] **Step 3: 做规格覆盖审查**

逐项对照 `docs/superpowers/specs/2026-09-01-airport-portal-refresh-design.md`，至少确认：

- 五项菜单和 0 返回精确；
- 设置页面每次强制生成新链接；
- 自动刷新旧链接优先；
- 手动与新链接兜底不依赖页面适配器；
- 页面结构不兼容不会破坏人工更新；
- 机场正文不做 YAML/Mihomo 校验；
- 来源记录完整替换，provider/source 事务恢复契约未被削弱；
- timer 安装、回滚、post-update、卸载全部覆盖；
- 24 小时/7 天三个刷新周期没有混淆；
- owner/member、备份五文件和通用注释契约不变。

- [ ] **Step 4: 检查仓库状态与提交范围**

```bash
git status --short
git log --oneline --decorate -12
```

Expected: 只允许存在用户原有且未纳入本计划的未跟踪文件；生产代码、测试和本文档不得有未提交修改。不得提交 `private/**`、真实页面、真实订阅链接或临时响应。

- [ ] **Step 5: 输出交付报告**

报告必须包含：每个任务的提交、修改文件、聚焦测试、完整测试数量与 skip、秘密扫描结果、未实机验证项，以及任何偏离规格的明确裁决。不得写“全部完成”而省略失败或环境限制。
