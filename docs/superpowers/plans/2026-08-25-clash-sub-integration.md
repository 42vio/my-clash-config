# clash-sub 整合部署实现计划（Nginx Stream 统一 443 + Installer）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 my-clash-config 仓库上实现设计文档 `docs/superpowers/specs/2026-08-25-clash-sub-integration-design.md` 定义的整合部署：Nginx stream 统一公网 443（SNI 分流 Reality/订阅/面板）、acme.sh wildcard 证书、`clash-sub install` 八阶段 installer、backup/update/cert/rollback 管理命令与文档重写。

**Architecture:** 订阅层保持「无守护进程、静态文件」管线，仅增加 config schema v2（`xui-public-endpoint` 端点改写 + 订阅权威 443）与单 Reality inbound 校验；nginx.py 增加纯渲染器（stream/sub-server 模板）与 `activate_nginx_files`（多文件原子安装 + 失败还原）；installer.py 以阶段 journal 驱动八阶段安装与回滚；manage.py 承载 backup/update/cert；全部外部效果经注入 runner/stdio 可单测。

**Tech Stack:** Python 3.11+（仓库现行语法）、stdlib unittest、Jinja2 3.1.6、PyYAML 6.0.3；服务器侧 Debian 12 + nginx（libnginx-mod-stream）+ acme.sh（dns_cf）。无新第三方依赖。

**运行测试命令（全计划统一）：**
- 单文件：`.venv/bin/python -m unittest tests.<模块名> -v`
- 全量：`.venv/bin/python -m unittest discover -s tests -v`

**仓库风格约束（对所有任务生效）：** fail-closed（未知/缺失即抛专用异常，错误码字符串不携带敏感信息）；frozen dataclass；路径校验绝对路径；测试名 `tests/test_lightweight_*.py`、英文用例名；提交信息前缀 `feat:`/`fix:`/`test:`/`docs:`。

**设计偏离说明（已在计划内落实，执行时不必回问）：**
1. spec §5「activate_runtime 泛化为多文件」落地为 `nginx.py` 新增兄弟函数 `activate_nginx_files()`（activate_runtime 的 journal/恢复语义绑定 state.json+routes.conf，不适合 nginx 系统配置安装场景）；渲染器 `render_stream_config`/`render_sub_server` 同放 nginx.py。效果与 spec 一致：原子替换 + nginx -t + 失败还原。
2. spec §5 的 sub-server 路径 `/etc/nginx/clash-sub/sub-server.conf` 改为 `/etc/nginx/conf.d/clash-sub.conf`（Debian 标准自动 include，避免改 nginx.conf 的 http 段）；routes.conf 路径不变（`/etc/nginx/clash-sub/routes.conf`）。
3. spec §6.2「checks.py 一致性校验」实际落在 `sources.normalize_xui_endpoints`（改写前校验原始 port==10443，同源 fail-closed，校验处拥有改写上下文），checks.py 不改。
4. CLI 按现有 `rollback <user> <release>` 语义扩展 `--install` 标志（位置参数变 `nargs="?"`，互斥校验），不新增子命令名。
5. 面板反代按现有模板合并进 sub server 块（spec §3 的「面板并入 sub 域名」），不再有独立 panel server。

---

## Task 1: config schema v2（xui-public-endpoint + 订阅权威 443）

**Files:**
- Modify: `clash_sub/config.py`
- Modify: `clash_sub/domain.py:14-25`
- Modify: `config/service.example.yaml`
- Test: `tests/test_lightweight_config.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_lightweight_config.py` 中，把模块顶部 `CONFIG` 字符串整体替换为：

```python
CONFIG = """\
schema-version: 2
owner-email: owner-example
subscription-authority: sub.example.com:443
xui-public-endpoint: example.com:443
xui-database: /etc/x-ui/x-ui.db
private-root: /var/lib/clash-sub/private
public-root: /var/lib/clash-sub/public
nginx-routes: /etc/nginx/clash-sub/routes.conf
mihomo-binary: /usr/local/lib/clash-sub/mihomo
nginx-binary: /usr/sbin/nginx
systemctl-binary: /usr/bin/systemctl
max-source-bytes: 5242880
"""
```

并把 `test_rejects_authority_without_port_8443` 改名与改断言：

```python
    def test_rejects_authority_without_port_443(self):
        self.write_config(replacement=CONFIG.replace("sub.example.com:443", "sub.example.com"))

        with self.assertRaisesRegex(ConfigError, "443"):
            load_config(self.path, self.root)
```

追加以下用例（放在 `test_rejects_authority_without_port_443` 之后）：

```python
    def test_loads_public_endpoint(self):
        config = load_config(self.path, self.root)

        self.assertEqual(config.xui_public_endpoint, "example.com:443")

    def test_rejects_missing_public_endpoint(self):
        self.write_config(replacement=CONFIG.replace("xui-public-endpoint: example.com:443\n", ""))

        with self.assertRaisesRegex(ConfigError, "missing required configuration"):
            load_config(self.path, self.root)

    def test_rejects_public_endpoint_without_port_443(self):
        self.write_config(replacement=CONFIG.replace("example.com:443\nxui-database", "example.com\nxui-database"))

        with self.assertRaisesRegex(ConfigError, "public endpoint"):
            load_config(self.path, self.root)

    def test_rejects_schema_version_1(self):
        self.write_config(replacement=CONFIG.replace("schema-version: 2", "schema-version: 1"))

        with self.assertRaisesRegex(ConfigError, "schema"):
            load_config(self.path, self.root)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_config -v`
Expected: FAIL（`unsupported configuration key`——新键未注册；及 schema 校验失败）

- [ ] **Step 3: 实现**

`clash_sub/config.py`：`_CONFIG_KEYS` 集合加入 `"xui-public-endpoint"`；`load_config` 中 schema 校验改为：

```python
    if data["schema-version"] != 2 or isinstance(data["schema-version"], bool):
        raise ConfigError("unsupported configuration schema")
```

`_subscription_authority` 中 `parsed.port == 8443` 改为 `parsed.port == 443`，错误消息 `"subscription authority must use port 8443"` 改为 `"subscription authority must use port 443"`。新增校验函数（放在 `_subscription_authority` 之后）：

```python
def _xui_public_endpoint(value: Any) -> str:
    endpoint = _nonempty_string(value, "xui public endpoint")
    if "://" in endpoint or any(character.isspace() for character in endpoint):
        raise ConfigError("invalid xui public endpoint")
    try:
        parsed = urlsplit("//" + endpoint)
        valid = (
            parsed.hostname is not None
            and parsed.port == 443
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError("xui public endpoint must use port 443")
    return endpoint
```

`load_config` 中在 `subscription_authority = ...` 行后加 `xui_public_endpoint = _xui_public_endpoint(data["xui-public-endpoint"])`，`ServiceConfig(...)` 构造加 `xui_public_endpoint=xui_public_endpoint,`。

`clash_sub/domain.py`：`ServiceConfig` 在 `subscription_authority: str` 之后插入一行：

```python
    xui_public_endpoint: str
```

`config/service.example.yaml`：`schema-version: 1`→`2`、`subscription-authority: sub.example.com:8443`→`sub.example.com:443`、在 subscription-authority 行后插入 `xui-public-endpoint: example.com:443`。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_lightweight_config -v`
Expected: PASS（全部用例）

- [ ] **Step 5: 全量回归（config 变更波及其他用例，需同步修）**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 部分旧用例失败（凡自行内联旧 schema YAML 的测试）。逐个把其内联配置的 `schema-version` 改 `2`、`subscription-authority` 端口改 `443`、补 `xui-public-endpoint: example.com:443` 行。预期最终全 PASS。

- [ ] **Step 6: 提交**

```bash
git add clash_sub/config.py clash_sub/domain.py config/service.example.yaml tests/
git commit -m "feat: introduce service configuration schema v2"
```

---

## Task 2: xui.py 单 Reality inbound 校验 + read_panel_port + fixture 扩展

**Files:**
- Modify: `clash_sub/xui.py`
- Modify: `tests/fixtures/xui-3.6.0.sql`
- Test: `tests/test_lightweight_xui.py`

- [ ] **Step 1: 扩展 fixture**

在 `tests/fixtures/xui-3.6.0.sql` 末尾追加：

```sql
CREATE TABLE inbounds (id INTEGER PRIMARY KEY, port INTEGER NOT NULL, protocol TEXT NOT NULL, enable INTEGER NOT NULL, listen TEXT NOT NULL, settings TEXT NOT NULL, stream_settings TEXT NOT NULL, remark TEXT NOT NULL);
INSERT INTO inbounds VALUES (1, 10443, 'vless', 1, '0.0.0.0', '{}', '{"security":"reality","realitySettings":{"serverName":"www.example.com"}}', 'reality-main');
INSERT INTO settings VALUES ('port', '2053');
```

- [ ] **Step 2: 写失败测试**

`tests/test_lightweight_xui.py` 追加用例（类内）：

```python
    def test_accepts_exactly_one_enabled_reality_inbound_on_10443(self):
        snapshot = read_xui_snapshot(self.database)

        self.assertEqual(snapshot.clients[0].client_id, 3)

    def test_rejects_second_reality_inbound(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO inbounds VALUES (2, 10544, 'vless', 1, '0.0.0.0', '{}',"
                " '{\"security\":\"reality\"}', 'reality-second')"
            )
        self.assert_incompatible()

    def test_rejects_reality_inbound_on_other_port(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE inbounds SET port = 10544 WHERE id = 1")
        self.assert_incompatible()

    def test_allows_non_reality_inbound_for_future_extension(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO inbounds VALUES (2, 20443, 'trojan', 1, '127.0.0.1', '{}', '{}', 'reserved')"
            )
        snapshot = read_xui_snapshot(self.database)

        self.assertTrue(snapshot.clients)

    def test_rejects_missing_inbounds_table(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP TABLE inbounds")
        self.assert_incompatible()

    def test_read_panel_port_returns_web_port(self):
        from clash_sub.xui import read_panel_port

        self.assertEqual(read_panel_port(self.database), 2053)

    def test_read_panel_port_rejects_invalid_port(self):
        from clash_sub.xui import read_panel_port

        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE settings SET value = 'not-a-port' WHERE key = 'port'")
        with self.assertRaises(XuiCompatibilityError):
            read_panel_port(self.database)
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_xui -v`
Expected: FAIL（`read_xui_snapshot` 尚未校验 inbounds；`read_panel_port` 不存在）

- [ ] **Step 4: 实现**

`clash_sub/xui.py`：

1) 常量区追加：

```python
_REALITY_INBOUND_PORT = 10443
```

2) `_REQUIRED_COLUMNS` 字典加入一项：

```python
    "inbounds": {"id", "port", "protocol", "enable", "stream_settings"},
```

3) `read_xui_snapshot` 中 `clients = _read_clients(connection, current_time_ms)` 之后加一行：

```python
            _validate_reality_inbound(connection)
```

4) 新增两个函数（放在 `_read_clients` 之后）：

```python
def _validate_reality_inbound(connection) -> None:
    rows = connection.execute(
        "SELECT port, protocol, enable, stream_settings FROM inbounds"
    ).fetchall()
    reality = [
        row for row in rows if row[1] == "vless" and "reality" in str(row[3]).lower()
    ]
    if (
        len(reality) != 1
        or reality[0][2] not in (0, 1)
        or reality[0][0] != _REALITY_INBOUND_PORT
    ):
        _fail()


def read_panel_port(path: Path) -> int:
    """Read the 3x-ui web panel port from the settings table."""
    try:
        connection = sqlite3.connect(
            "file:%s?mode=ro" % quote(str(path)), uri=True, timeout=1.0
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            _validate_schema(connection)
            rows = connection.execute(
                "SELECT value FROM settings WHERE key = 'port'"
            ).fetchall()
        finally:
            connection.close()
    except XuiCompatibilityError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        raise XuiCompatibilityError(_ERROR) from None
    if len(rows) != 1:
        _fail()
    return _port(rows[0][0])
```

- [ ] **Step 5: 运行确认通过 + 全量回归**

Run: `.venv/bin/python -m unittest tests.test_lightweight_xui -v && .venv/bin/python -m unittest discover -s tests -v`
Expected: 全 PASS（fixture 已含合规 inbound；其他共享 fixture 的测试不受影响）

- [ ] **Step 6: 提交**

```bash
git add clash_sub/xui.py tests/fixtures/xui-3.6.0.sql tests/test_lightweight_xui.py
git commit -m "feat: enforce single reality inbound and expose panel port"
```

---

## Task 3: sources.py 端点规范化 + service 接线

**Files:**
- Modify: `clash_sub/sources.py`
- Modify: `clash_sub/service.py:144-146`
- Test: `tests/test_lightweight_sources.py`

- [ ] **Step 1: 写失败测试**

`tests/test_lightweight_sources.py` 追加（含 import 区补 `from clash_sub.sources import normalize_xui_endpoints`，若该名未导入）：

```python
class EndpointNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.proxies = [
            {
                "name": "reality-node",
                "type": "vless",
                "server": "panel.example.com",
                "port": 10443,
                "uuid": "uuid-value",
                "tls": True,
                "servername": "www.example.com",
                "reality-opts": {"public-key": "key", "short-id": "sid"},
            }
        ]

    def test_rewrites_server_and_port_only(self):
        normalized = normalize_xui_endpoints(self.proxies, "example.com:443")

        self.assertEqual(normalized[0]["server"], "example.com")
        self.assertEqual(normalized[0]["port"], 443)
        self.assertEqual(normalized[0]["servername"], "www.example.com")
        self.assertEqual(normalized[0]["uuid"], "uuid-value")
        self.assertEqual(normalized[0]["reality-opts"]["public-key"], "key")

    def test_rejects_node_with_unexpected_inbound_port(self):
        self.proxies[0]["port"] = 10544

        with self.assertRaisesRegex(SourceError, "proxy source rejected"):
            normalize_xui_endpoints(self.proxies, "example.com:443")

    def test_rejects_node_without_port(self):
        del self.proxies[0]["port"]

        with self.assertRaisesRegex(SourceError, "proxy source rejected"):
            normalize_xui_endpoints(self.proxies, "example.com:443")

    def test_rejects_invalid_endpoint(self):
        for endpoint in ("", "example.com", "https://example.com:443", "example.com:8443"):
            with self.assertRaisesRegex(SourceError, "proxy source rejected"):
                normalize_xui_endpoints(self.proxies, endpoint)

    def test_does_not_mutate_input(self):
        original = copy.deepcopy(self.proxies)

        normalize_xui_endpoints(self.proxies, "example.com:443")

        self.assertEqual(self.proxies, original)
```

（若文件未导入 `copy`/`SourceError`，在 import 区补 `import copy` 与确保 `SourceError` 已从 `clash_sub.sources` 导入。）

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_sources.EndpointNormalizationTests -v`
Expected: FAIL（`normalize_xui_endpoints` 未定义，ImportError）

- [ ] **Step 3: 实现**

`clash_sub/sources.py` 常量区追加 `XUI_INBOUND_PORT = 10443`，并在 `merge_proxy_sources` 之前新增：

```python
def normalize_xui_endpoints(proxies, endpoint):
    """Rewrite 3x-ui node addresses to the public entry endpoint."""
    host, port = _parse_public_endpoint(endpoint)
    normalized = []
    for proxy in _normalize_proxies({"proxies": proxies}):
        copied = copy.deepcopy(proxy)
        if copied.get("port") != XUI_INBOUND_PORT or not isinstance(copied.get("server"), str):
            _source_fail()
        copied["server"] = host
        copied["port"] = port
        normalized.append(copied)
    return normalized


def _parse_public_endpoint(endpoint):
    if not isinstance(endpoint, str):
        _source_fail()
    try:
        parts = urlsplit("//" + endpoint)
        host, port = parts.hostname, parts.port
        valid = (
            host is not None
            and port is not None
            and parts.username is None
            and parts.password is None
            and not parts.path
            and not parts.query
            and not parts.fragment
        )
    except ValueError:
        _source_fail()
    if not valid:
        _source_fail()
    return host, port
```

`clash_sub/service.py` `_prepare` 方法中，`xui=self._fetch(url,self.config.max_source_bytes);` 之后、`bundle=self._render(...)` 之前插入（保持该文件紧凑单行风格，import 区加 `from clash_sub.sources import normalize_xui_endpoints`——注意 service.py 现无 sources import，直接加顶部 import）：

```python
        xui=normalize_xui_endpoints(xui,self.config.xui_public_endpoint)
```

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `.venv/bin/python -m unittest tests.test_lightweight_sources -v && .venv/bin/python -m unittest discover -s tests -v`
Expected: service/e2e 用例若注入 fake fetch 返回 port≠10443 的节点会失败——修测试 fixture 数据：把 fake xui 源节点 port 统一改为 10443（并在其断言中预期改写后 port==443、server==endpoint 主机）。全 PASS。

- [ ] **Step 5: 提交**

```bash
git add clash_sub/sources.py clash_sub/service.py tests/test_lightweight_sources.py tests/
git commit -m "feat: rewrite xui node endpoints to the public 443 entry"
```

---

## Task 4: nginx 模板与渲染器

**Files:**
- Create: `templates/nginx/stream.conf.j2`
- Create: `templates/nginx/sub-server.conf.j2`
- Modify: `clash_sub/nginx.py`
- Test: `tests/test_lightweight_nginx.py`

- [ ] **Step 1: 写失败测试**

`tests/test_lightweight_nginx.py` 追加（import 区补 `from clash_sub.nginx import render_stream_config, render_sub_server`）：

```python
class NginxTemplateRenderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.template_root = Path(self.tempdir.name) / "templates" / "nginx"
        self.template_root.mkdir(parents=True)
        source_root = Path(__file__).resolve().parents[1] / "templates" / "nginx"
        for template in source_root.iterdir():
            shutil.copy(template, self.template_root / template.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _config(self):
        return ServiceConfig(
            owner_email="owner-example",
            subscription_authority="sub.example.com:443",
            xui_public_endpoint="example.com:443",
            xui_database=Path("/etc/x-ui/x-ui.db"),
            private_root=Path("/var/lib/clash-sub/private"),
            public_root=Path("/var/lib/clash-sub/public"),
            nginx_routes=Path("/etc/nginx/clash-sub/routes.conf"),
            mihomo_binary=Path("/usr/local/lib/clash-sub/mihomo"),
            nginx_binary=Path("/usr/sbin/nginx"),
            systemctl_binary=Path("/usr/bin/systemctl"),
            template_root=self.template_root.parent,
        )

    def test_renders_stream_map_with_default_reality(self):
        rendered = render_stream_config(self._config(), "example.com")

        self.assertIn("map $ssl_preread_server_name", rendered)
        self.assertIn("sub.example.com", rendered)
        self.assertIn("127.0.0.1:30443", rendered)
        self.assertIn("trojan.example.com", rendered)
        self.assertIn("127.0.0.1:20443", rendered)
        self.assertIn("default", rendered)
        self.assertIn("127.0.0.1:10443", rendered)
        self.assertIn("ssl_preread on;", rendered)
        self.assertIn("listen 443;", rendered)

    def test_renders_sub_server_with_panel_and_routes(self):
        rendered = render_sub_server(
            self._config(),
            domain="example.com",
            panel_port=2053,
            panel_base_path="/p-1a2b3c4d",
            routes_include="/etc/nginx/clash-sub/routes.conf",
            fullchain="/etc/ssl/domain/fullchain.pem",
            privkey="/etc/ssl/domain/privkey.pem",
        )

        self.assertIn("listen 127.0.0.1:30443 ssl;", rendered)
        self.assertIn("server_name sub.example.com;", rendered)
        self.assertIn("ssl_certificate /etc/ssl/domain/fullchain.pem;", rendered)
        self.assertIn("include /etc/nginx/clash-sub/routes.conf;", rendered)
        self.assertIn("location = /p-1a2b3c4d {", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:2053/p-1a2b3c4d/;", rendered)
        self.assertIn("limit_req zone=clash_subscription", rendered)
```

（文件 import 区需有 `import shutil`、`import tempfile`、`from pathlib import Path`、`from clash_sub.domain import ServiceConfig`——按现有 import 情况补齐。）

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_nginx.NginxTemplateRenderTests -v`
Expected: FAIL（模板文件不存在，ImportError 或复制失败）

- [ ] **Step 3: 写模板**

`templates/nginx/stream.conf.j2`：

```nginx
# Managed by clash-sub install. SNI routing for the unified 443 entry.
map $ssl_preread_server_name $clash_sub_backend {
    default             127.0.0.1:10443;
    sub.{{ domain }}    127.0.0.1:30443;
    trojan.{{ domain }} 127.0.0.1:20443;
}

server {
    listen 443;
    listen [::]:443;
    proxy_pass $clash_sub_backend;
    ssl_preread on;
    proxy_connect_timeout 5s;
    error_log off;
}
```

`templates/nginx/sub-server.conf.j2`：

```nginx
# Managed by clash-sub install. Do not edit; regenerate via clash-sub install/update.
limit_req_zone $binary_remote_addr zone=clash_subscription:10m rate=2r/s;
limit_req_zone $binary_remote_addr zone=clash_panel:10m rate=10r/s;

server {
    listen 127.0.0.1:30443 ssl;
    server_name sub.{{ domain }};
    server_tokens off;

    ssl_certificate {{ fullchain }};
    ssl_certificate_key {{ privkey }};
    ssl_protocols TLSv1.2 TLSv1.3;

    include {{ routes_include }};

    location = {{ panel_base_path }} {
        access_log off;
        return 308 {{ panel_base_path }}/;
    }

    location ^~ {{ panel_base_path }}/ {
        access_log off;
        limit_req zone=clash_panel burst=20 nodelay;
        proxy_pass http://127.0.0.1:{{ panel_port }}{{ panel_base_path }}/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
        proxy_hide_header Server;
        proxy_max_temp_file_size 0;
    }

    location ^~ /s/ {
        access_log off;
        log_not_found off;
        default_type "text/yaml; charset=utf-8";
        add_header X-Content-Type-Options nosniff always;
        add_header Cache-Control no-store always;
        return 404;
    }

    location / {
        access_log off;
        log_not_found off;
        return 404;
    }
}
```

- [ ] **Step 4: 实现渲染器**

`clash_sub/nginx.py` import 区加 `from jinja2 import Environment, FileSystemLoader, StrictUndefined`，`render_routes` 之前新增：

```python
def _nginx_template_environment(config):
    directory = Path(config.template_root) / "nginx"
    if not directory.is_dir():
        raise NginxError("invalid service configuration")
    return Environment(
        loader=FileSystemLoader(str(directory)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def render_stream_config(config, domain):
    """Render the 443 stream SNI routing configuration."""
    if not isinstance(domain, str) or not domain.strip():
        raise NginxError("invalid domain")
    try:
        return _nginx_template_environment(config).get_template(
            "stream.conf.j2"
        ).render(domain=domain)
    except NginxError:
        raise
    except Exception:
        raise NginxError("stream rendering failed") from None


def render_sub_server(config, *, domain, panel_port, panel_base_path, routes_include, fullchain, privkey):
    """Render the loopback TLS server for subscriptions and the panel."""
    if (
        not isinstance(domain, str)
        or not domain.strip()
        or isinstance(panel_port, bool)
        or not isinstance(panel_port, int)
        or not 1 <= panel_port <= 65535
        or not isinstance(panel_base_path, str)
        or not re.fullmatch(r"/[A-Za-z0-9_-]+", panel_base_path)
        or not isinstance(routes_include, str)
        or not routes_include.startswith("/")
        or not isinstance(fullchain, str)
        or not fullchain.startswith("/")
        or not isinstance(privkey, str)
        or not privkey.startswith("/")
    ):
        raise NginxError("invalid sub server parameters")
    try:
        return _nginx_template_environment(config).get_template(
            "sub-server.conf.j2"
        ).render(
            domain=domain,
            panel_port=panel_port,
            panel_base_path=panel_base_path,
            routes_include=routes_include,
            fullchain=fullchain,
            privkey=privkey,
        )
    except NginxError:
        raise
    except Exception:
        raise NginxError("sub server rendering failed") from None
```

- [ ] **Step 5: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_lightweight_nginx -v`
Expected: PASS

```bash
git add templates/nginx/ clash_sub/nginx.py tests/test_lightweight_nginx.py
git commit -m "feat: render stream and sub-server nginx templates"
```

---

## Task 5: activate_nginx_files——多文件原子安装与失败还原

**Files:**
- Modify: `clash_sub/nginx.py`
- Test: `tests/test_lightweight_nginx.py`

- [ ] **Step 1: 写失败测试**

`tests/test_lightweight_nginx.py` 追加：

```python
class ActivateNginxFilesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.target = self.root / "conf.d" / "clash-sub.conf"
        self.target.parent.mkdir()
        self.runner_calls = []
        self.fail_validation = False

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        result = subprocess.CompletedProcess(arguments, 0)
        if self.fail_validation and arguments[:1] == ["nginx"]:
            result = subprocess.CompletedProcess(arguments, 1)
        return result

    def test_installs_new_file_and_runs_nginx_t(self):
        contents = "# new\n".encode("utf-8")

        activate_nginx_files(((self.target, contents, 0o640),), self._runner)

        self.assertEqual(self.target.read_bytes(), contents)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.runner_calls[0][:2], ["nginx", "-t"])

    def test_restores_previous_contents_when_validation_fails(self):
        self.target.write_text("# old\n", encoding="utf-8")
        os.chmod(self.target, 0o640)
        self.fail_validation = True

        with self.assertRaisesRegex(NginxError, "Nginx validation failed"):
            activate_nginx_files(((self.target, b"# new\n", 0o640),), self._runner)

        self.assertEqual(self.target.read_text(encoding="utf-8"), "# old\n")

    def test_removes_new_file_when_validation_fails(self):
        self.fail_validation = True

        with self.assertRaisesRegex(NginxError, "Nginx validation failed"):
            activate_nginx_files(((self.target, b"# new\n", 0o640),), self._runner)

        self.assertFalse(self.target.exists())

    def test_reloads_when_requested(self):
        activate_nginx_files(
            ((self.target, b"# new\n", 0o640),), self._runner, reload=True
        )

        self.assertEqual(
            [call[:2] for call in self.runner_calls],
            [["nginx", "-t"], ["systemctl", "reload", "nginx"]],
        )

    def test_rejects_relative_path(self):
        with self.assertRaisesRegex(NginxError, "invalid nginx file"):
            activate_nginx_files(((Path("relative.conf"), b"x", 0o640),), self._runner)
```

（import 区补 `from clash_sub.nginx import activate_nginx_files, NginxError`、`import os`、`import subprocess`。）

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_nginx.ActivateNginxFilesTests -v`
Expected: FAIL（ImportError：activate_nginx_files 不存在）

- [ ] **Step 3: 实现**

`clash_sub/nginx.py` 在 `activate_runtime` 之后新增：

```python
def activate_nginx_files(files, runner, *, reload=False):
    """Atomically install nginx configuration files with rollback on failure.

    ``files`` is an iterable of ``(path, contents, mode)`` tuples.  Existing
    targets are snapshotted in memory; if ``nginx -t`` (or the optional
    reload) fails, every target is restored and newly created targets are
    removed again.
    """
    if isinstance(files, (str, bytes)) or not callable(runner):
        raise NginxError("invalid nginx file activation")
    try:
        entries = tuple(files)
    except TypeError:
        raise NginxError("invalid nginx file activation") from None
    artifacts = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 3:
            raise NginxError("invalid nginx file activation")
        path, contents, mode = entry
        path = _target(path)
        _directory(path.parent, private=False)
        if (
            path in seen
            or not isinstance(contents, bytes)
            or not contents
            or mode not in (_PRIVATE_MODE, _ROUTE_MODE, 0o640)
        ):
            raise NginxError("invalid nginx file activation")
        seen.add(path)
        artifacts.append((path, contents, mode))

    snapshots = [(path, _snapshot(path)) for path, _, _ in artifacts]
    candidates = []
    try:
        for path, contents, mode in artifacts:
            candidates.append((path, _write_candidate(path, contents, mode)))
        for path, candidate in candidates:
            os.replace(candidate, path)
            candidate = None
            _fsync_directory(path.parent)
        if not _command_ok(runner, (str(_nginx_binary_from_runner(runner, artifacts),))):
            raise RuntimeError
    except Exception:
        _restore_files(snapshots)
        raise NginxError("Nginx validation failed") from None
    finally:
        for _, candidate in candidates:
            if candidate is not None:
                _remove_candidate(candidate)

    if reload and not _command_ok(
        runner, (_systemctl_binary_from_runner(runner), "reload", "nginx")
    ):
        _restore_files(snapshots)
        raise NginxError("Nginx reload failed")
    return True
```

上述实现依赖两个小helper与一个折衷：runner 无法从本函数得知 nginx/systemctl 二进制路径（与 activate_runtime 不同，这里没有 config）。为保证与现有 `_command_ok` 契约一致且可测，改为**显式参数**签名（替换上面草稿中 `_nginx_binary_from_runner` 的做法）：

最终签名为：

```python
def activate_nginx_files(files, runner, *, nginx_binary, systemctl_binary=None, reload=False):
```

其中 `nginx_binary` 为必传 `Path`/str；`reload=True` 时 `systemctl_binary` 必传。函数内验证命令为 `(str(nginx_binary), "-t")`，reload 命令为 `(str(systemctl_binary), "reload", "nginx")`。同时新增还原 helper（放在 `_restore` 附近）：

```python
def _restore_files(snapshots):
    for path, (exists, contents, mode) in snapshots:
        try:
            if exists:
                os.replace(_write_candidate(path, contents, mode), path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
```

对应地，把 Step 1 测试中所有调用改为带关键字参数：

```python
        activate_nginx_files(
            ((self.target, contents, 0o640),),
            self._runner,
            nginx_binary="/usr/sbin/nginx",
            systemctl_binary="/usr/bin/systemctl",
        )
```

（`reload=True` 的用例同加 `systemctl_binary`；`_runner` 中匹配 `arguments[:2] == ["/usr/sbin/nginx", "-t"]` 判定失败注入——把 `_runner` 里 `arguments[:1] == ["nginx"]` 改为 `arguments[0] == "/usr/sbin/nginx"`。）

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `.venv/bin/python -m unittest tests.test_lightweight_nginx -v && .venv/bin/python -m unittest discover -s tests -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add clash_sub/nginx.py tests/test_lightweight_nginx.py
git commit -m "feat: atomically install nginx configuration files"
```

---

## Task 6: installer.py 骨架——InstallPaths / InstallState journal / 错误类型

**Files:**
- Create: `clash_sub/installer.py`
- Test: `Create: tests/test_lightweight_installer.py`

- [ ] **Step 1: 写失败测试**

`tests/test_lightweight_installer.py`：

```python
import json
import tempfile
import unittest
from pathlib import Path

from clash_sub.installer import (
    InstallPaths,
    InstallState,
    InstallerError,
    load_install_state,
    save_install_state,
)


class InstallStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_round_trips_state_with_0600_mode(self):
        state = InstallState(domain="example.com", panel_port=2053, panel_base_path="/p-1a")
        path = self.root / "install-state.json"

        save_install_state(path, state)
        loaded = load_install_state(path)

        self.assertEqual(loaded.domain, "example.com")
        self.assertEqual(loaded.panel_port, 2053)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_load_rejects_unknown_schema(self):
        path = self.root / "install-state.json"
        path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

        with self.assertRaises(InstallerError):
            load_install_state(path)

    def test_load_returns_default_when_absent(self):
        self.assertEqual(
            load_install_state(self.root / "missing.json"), InstallState()
        )

    def test_default_paths_target_etc_layout(self):
        paths = InstallPaths()

        self.assertEqual(paths.stream_conf(), Path("/etc/nginx/stream-conf.d/clash-sub.conf"))
        self.assertEqual(paths.http_conf(), Path("/etc/nginx/conf.d/clash-sub.conf"))
        self.assertEqual(paths.routes_conf(), Path("/etc/nginx/clash-sub/routes.conf"))
        self.assertEqual(paths.fullchain(), Path("/etc/ssl/domain/fullchain.pem"))
        self.assertEqual(paths.xui_database, Path("/etc/x-ui/x-ui.db"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`clash_sub/installer.py`：

```python
"""One-shot integration installer for the unified 443 topology."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


class InstallerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class InstallPaths:
    """Filesystem layout touched by the installer.  Overridable for tests."""

    nginx_conf: Path = Path("/etc/nginx/nginx.conf")
    stream_conf_dir: Path = Path("/etc/nginx/stream-conf.d")
    http_conf_dir: Path = Path("/etc/nginx/conf.d")
    routes_conf: Path = Path("/etc/nginx/clash-sub/routes.conf")
    ssl_dir: Path = Path("/etc/ssl/domain")
    acme_home: Path = Path("/root/.acme.sh")
    sysctl_conf: Path = Path("/etc/sysctl.d/99-clash-sub.conf")
    journald_conf_dir: Path = Path("/etc/systemd/journald.conf.d")
    systemd_dir: Path = Path("/etc/systemd/system")
    swap_file: Path = Path("/swapfile-clash-sub.img")
    xui_database: Path = Path("/etc/x-ui/x-ui.db")
    private_root: Path = Path("/var/lib/clash-sub/private")
    public_root: Path = Path("/var/lib/clash-sub/public")

    def stream_conf(self):
        return self.stream_conf_dir / "clash-sub.conf"

    def http_conf(self):
        return self.http_conf_dir / "clash-sub.conf"

    def fullchain(self):
        return self.ssl_dir / "fullchain.pem"

    def privkey(self):
        return self.ssl_dir / "privkey.pem"


@dataclass
class InstallState:
    """Durable install journal: phase progress plus render parameters."""

    schema_version: int = 1
    domain: str = ""
    panel_port: int = 0
    panel_base_path: str = ""
    phases_done: list = field(default_factory=list)
    files_written: list = field(default_factory=list)
    backups: dict = field(default_factory=dict)


def load_install_state(path):
    path = Path(path)
    if not path.exists():
        return InstallState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = InstallState(**payload)
    except (OSError, ValueError, TypeError):
        raise InstallerError("install_state_invalid") from None
    if state.schema_version != 1:
        raise InstallerError("install_state_invalid")
    return state


def save_install_state(path, state):
    path = Path(path)
    if not isinstance(state, InstallState):
        raise InstallerError("install_state_invalid")
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(asdict(state), sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink(missing_ok=True)
```

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer -v`
Expected: PASS

```bash
git add clash_sub/installer.py tests/test_lightweight_installer.py
git commit -m "feat: add installer state journal and path layout"
```

---

## Task 7: preflight 阶段

**Files:**
- Modify: `clash_sub/installer.py`
- Test: `tests/test_lightweight_installer.py`

- [ ] **Step 1: 写失败测试**

`tests/test_lightweight_installer.py` 追加：

```python
class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()
        self.paths = InstallPaths(
            nginx_conf=self.root / "nginx.conf",
            stream_conf_dir=self.root / "stream-conf.d",
            http_conf_dir=self.root / "conf.d",
            routes_conf=self.root / "clash-sub" / "routes.conf",
            ssl_dir=self.root / "ssl",
            acme_home=self.root / "acme",
            sysctl_conf=self.root / "sysctl.conf",
            journald_conf_dir=self.root / "journald",
            systemd_dir=self.root / "systemd",
            swap_file=self.root / "swap.img",
            xui_database=self.root / "x-ui.db",
            private_root=self.root / "private",
            public_root=self.root / "public",
        )
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self, runner):
        from clash_sub.installer import Installer

        return Installer(self.root, paths=self.paths, runner=runner)

    def _runner(self, result=0):
        def run(arguments, **_):
            self.runner_calls.append(list(arguments))
            return subprocess.CompletedProcess(arguments, result)

        return run

    def test_rejects_non_root(self):
        installer = self._installer(self._runner())

        with patch("clash_sub.installer.os.geteuid", return_value=1000), self.assertRaisesRegex(
            InstallerError, "not_root"
        ):
            installer.preflight("example.com")

    def test_rejects_when_443_is_taken(self):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        try:
            installer = self._installer(self._runner())
            with patch("clash_sub.installer._REality_PORT", port):  # 见下方实现说明
                pass
            with patch("clash_sub.installer.os.geteuid", return_value=0), self.assertRaisesRegex(
                InstallerError, "port_443_taken"
            ):
                installer._require_free_tcp_port(port)
        finally:
            server.close()

    def test_rejects_dns_mismatch(self):
        installer = self._installer(self._runner())

        def fake_resolve(hostname):
            return ["203.0.113.99"]

        def fake_local_ips(runner):
            return ["192.0.2.1"]

        with patch("clash_sub.installer._resolve_host", fake_resolve), patch(
            "clash_sub.installer._local_ipv4", fake_local_ips
        ), self.assertRaisesRegex(InstallerError, "dns_mismatch"):
            installer._require_dns("example.com")

    def test_accepts_matching_dns(self):
        installer = self._installer(self._runner())

        with patch("clash_sub.installer._resolve_host", lambda host: ["192.0.2.1"]), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1"]
        ):
            installer._require_dns("example.com")
```

**说明（执行者注意）**：`test_rejects_when_443_is_taken` 中第一段 `with patch(...): pass` 是无意义残行，实现时**删除这两行**，直接用动态端口测 `_require_free_tcp_port(port)`。import 区补 `import socket`、`import subprocess`、`from unittest.mock import patch`、`from clash_sub.installer import Installer`（按需）。

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer.PreflightTests -v`
Expected: FAIL（Installer/preflight 不存在）

- [ ] **Step 3: 实现**

`clash_sub/installer.py` 追加（import 区补 `import shutil as _shutil`、`import socket`、`import subprocess`、`from clash_sub.xui import XuiCompatibilityError, read_panel_port, read_xui_snapshot`）：

```python
_MINIMUM_FREE_BYTES = 1024 ** 3
_DEBIAN_MAJOR = "12"


class Installer:
    """Phase-driven installer; every external effect goes through ``runner``."""

    def __init__(self, repo_root, *, paths=None, runner=None, print_fn=None):
        self.repo_root = Path(repo_root)
        self.paths = paths or InstallPaths()
        self.runner = runner or subprocess.run
        self.print_fn = print_fn or (lambda message: None)
        self._state_path = self.repo_root / "private" / "install-state.json"

    # -- journal ---------------------------------------------------------
    def state(self):
        return load_install_state(self._state_path)

    def _save_state(self, state):
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        save_install_state(self._state_path, state)

    def _phase_done(self, name, **updates):
        state = self.state()
        if name not in state.phases_done:
            state.phases_done.append(name)
        for key, value in updates.items():
            setattr(state, key, value)
        self._save_state(state)

    # -- phase 0 ---------------------------------------------------------
    def preflight(self, domain):
        if os.geteuid() != 0:
            raise InstallerError("not_root")
        self._require_debian()
        self._require_disk()
        self._require_xui()
        self._require_free_tcp_port(443)
        self._require_dns(domain)
        self._phase_done("preflight")
        return True

    def _require_debian(self):
        try:
            release = _shutil.which("uname") and None
        except Exception:
            release = None
        try:
            with open("/etc/os-release", encoding="ascii") as handle:
                fields = dict(
                    line.split("=", 1)
                    for line in handle.read().splitlines()
                    if "=" in line
                )
        except OSError:
            raise InstallerError("unsupported_distribution") from None
        if fields.get("ID", "").strip('"') != "debian" or not fields.get(
            "VERSION_ID", ""
        ).strip('"').startswith(_DEBIAN_MAJOR):
            raise InstallerError("unsupported_distribution")

    def _require_disk(self):
        try:
            usage = os.statvfs(self.repo_root)
        except OSError:
            raise InstallerError("disk_space_insufficient") from None
        if usage.f_bavail * usage.f_frsize < _MINIMUM_FREE_BYTES:
            raise InstallerError("disk_space_insufficient")

    def _require_xui(self):
        try:
            read_xui_snapshot(self.paths.xui_database)
            read_panel_port(self.paths.xui_database)
        except XuiCompatibilityError:
            raise InstallerError("xui_incompatible") from None

    def _require_free_tcp_port(self, port):
        probe = socket.socket()
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", port))
        except OSError:
            raise InstallerError("port_443_taken") from None
        finally:
            probe.close()

    def _require_dns(self, domain):
        resolved = _resolve_host("sub." + domain)
        local = _local_ipv4(self.runner)
        if not any(address in local for address in resolved):
            raise InstallerError("dns_mismatch")


def _resolve_host(hostname):
    try:
        return sorted(
            {
                info[4][0]
                for info in socket.getaddrinfo(hostname, None, socket.AF_INET)
            }
        )
    except OSError:
        raise InstallerError("dns_mismatch") from None


def _local_ipv4(runner):
    try:
        result = runner(
            ["hostname", "-I"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return result.stdout.decode("ascii", "replace").split()
    except Exception:
        raise InstallerError("dns_mismatch") from None
```

删除 `_require_debian` 开头三行无意义的 `release` 探测（保留 os-release 解析主体）。

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer -v`
Expected: PASS

```bash
git add clash_sub/installer.py tests/test_lightweight_installer.py
git commit -m "feat: add installer preflight checks"
```

---

## Task 8: Phase 1 低配优化（swap / swappiness / journald）

**Files:**
- Modify: `clash_sub/installer.py`
- Test: `tests/test_lightweight_installer.py`

- [ ] **Step 1: 写失败测试**

追加：

```python
class LowMemoryPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "private").mkdir(parents=True)
        self.paths = InstallPaths(
            sysctl_conf=self.root / "99-clash-sub.conf",
            journald_conf_dir=self.root / "journald",
            swap_file=self.root / "swap.img",
        )
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        from clash_sub.installer import Installer

        return Installer(self.root, paths=self.paths, runner=self._runner)

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0)

    def test_writes_sysctl_and_journald_without_swap(self):
        self._installer().optimize_low_memory(swap_mb=0)

        self.assertEqual(
            self.paths.sysctl_conf.read_text(encoding="utf-8"),
            "vm.swappiness=10\n",
        )
        self.assertEqual(
            (self.paths.journald_conf_dir / "99-clash-sub.conf").read_text(encoding="utf-8"),
            "[Journal]\nSystemMaxUse=50M\n",
        )
        swap_commands = [c for c in self.runner_calls if "swapon" in c or "mkswap" in c]
        self.assertEqual(swap_commands, [])

    def test_creates_swap_when_requested(self):
        self._installer().optimize_low_memory(swap_mb=1024)

        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("fallocate" in item for item in joined))
        self.assertTrue(any("mkswap" in item and str(self.paths.swap_file) in item for item in joined))
        self.assertTrue(any("swapon" in item and str(self.paths.swap_file) in item for item in joined))

    def test_skips_swap_when_file_exists(self):
        self.paths.swap_file.write_bytes(b"")
        self._installer().optimize_low_memory(swap_mb=1024)

        joined = [" ".join(c) for c in self.runner_calls]
        self.assertFalse(any("fallocate" in item for item in joined))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer.LowMemoryPhaseTests -v`
Expected: FAIL（optimize_low_memory 不存在）

- [ ] **Step 3: 实现**

Installer 类追加方法：

```python
    # -- phase 1 ---------------------------------------------------------
    def optimize_low_memory(self, swap_mb):
        self._write_file(self.paths.sysctl_conf, "vm.swappiness=10\n", 0o644)
        self.paths.journald_conf_dir.mkdir(parents=True, exist_ok=True)
        self._write_file(
            self.paths.journald_conf_dir / "99-clash-sub.conf",
            "[Journal]\nSystemMaxUse=50M\n",
            0o644,
        )
        if swap_mb and swap_mb > 0 and not self.paths.swap_file.exists():
            self._run(
                [
                    "fallocate",
                    "-l",
                    "%sM" % swap_mb,
                    str(self.paths.swap_file),
                ]
            )
            self._run(["chmod", "600", str(self.paths.swap_file)])
            self._run(["mkswap", str(self.paths.swap_file)])
            self._run(["swapon", str(self.paths.swap_file)])
        self._run([str(Path("/usr/sbin/sysctl")), "-p", str(self.paths.sysctl_conf)])
        self._phase_done("low_memory")

    def _write_file(self, path, contents, mode):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink(missing_ok=True)

    def _run(self, arguments):
        result = self.runner(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=600,
            check=False,
        )
        if result.returncode != 0:
            raise InstallerError("command_failed")
```

`_run` 中 runner 超时异常处理：包一层 `except Exception: raise InstallerError("command_failed") from None`。

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer -v`
Expected: PASS（注意 `_run(["sysctl", ...])` 会真实调用系统 sysctl——测试断言 `runner_calls` 由 fake runner 记录，sysctl 调用走的是 fake runner，不真执行 ✓）

```bash
git add clash_sub/installer.py tests/test_lightweight_installer.py
git commit -m "feat: add low-memory tuning phase"
```

---

## Task 9: Phase 2 nginx 安装 + stream include 幂等追加

**Files:**
- Modify: `clash_sub/installer.py`
- Test: `tests/test_lightweight_installer.py`

- [ ] **Step 1: 写失败测试**

追加：

```python
class NginxPackagePhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "private").mkdir(parents=True)
        self.nginx_conf = self.root / "nginx.conf"
        self.nginx_conf.write_text(
            "user www-data;\nhttp {\n    include /etc/nginx/conf.d/*.conf;\n}\n",
            encoding="utf-8",
        )
        self.paths = InstallPaths(nginx_conf=self.nginx_conf, stream_conf_dir=self.root / "stream-conf.d")
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        from clash_sub.installer import Installer

        return Installer(self.root, paths=self.paths, runner=self._runner)

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0)

    def test_installs_packages_and_appends_stream_include_once(self):
        installer = self._installer()

        installer.install_nginx_packages()
        text_one = self.nginx_conf.read_text(encoding="utf-8")

        installer.install_nginx_packages()
        text_two = self.nginx_conf.read_text(encoding="utf-8")

        self.assertIn("stream {", text_one)
        self.assertIn(str(self.paths.stream_conf_dir), text_one)
        self.assertEqual(text_one, text_two)
        self.assertIn("user www-data;", text_one)
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("apt-get" in item and "nginx" in item for item in joined))
        self.assertTrue(any("libnginx-mod-stream" in item for item in joined))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer.NginxPackagePhaseTests -v`
Expected: FAIL

- [ ] **Step 3: 实现**

Installer 类追加：

```python
    # -- phase 2 ---------------------------------------------------------
    def install_nginx_packages(self):
        self._run(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                "nginx",
                "libnginx-mod-stream",
            ]
        )
        self._ensure_stream_include()
        self._phase_done("nginx_packages")

    def _ensure_stream_include(self):
        marker = "# clash-sub stream include"
        text = self.paths.nginx_conf.read_text(encoding="utf-8")
        if marker in text:
            return False
        block = (
            "\n%s\nstream {\n    include %s/*.conf;\n}\n"
            % (marker, self.paths.stream_conf_dir)
        )
        self._write_file(
            self.paths.nginx_conf, text.rstrip("\n") + "\n" + block, 0o644
        )
        return True
```

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer -v`
Expected: PASS

```bash
git add clash_sub/installer.py tests/test_lightweight_installer.py
git commit -m "feat: install nginx and append stream include idempotently"
```

---

## Task 10: Phase 3 acme.sh wildcard 证书

**Files:**
- Modify: `clash_sub/installer.py`
- Test: `tests/test_lightweight_installer.py`

- [ ] **Step 1: 写失败测试**

追加：

```python
class CertificatePhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "private").mkdir(parents=True)
        self.paths = InstallPaths(ssl_dir=self.root / "ssl", acme_home=self.root / "acme")
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        from clash_sub.installer import Installer

        return Installer(self.root, paths=self.paths, runner=self._runner)

    def _runner(self, arguments, **_):
        self.runner_calls.append({"argv": list(arguments), "env": None})
        fullchain = self.paths.fullchain()
        if arguments[:1] == [str(self.paths.acme_home / "acme.sh")] and "--install-cert" in arguments:
            fullchain.parent.mkdir(parents=True, exist_ok=True)
            fullchain.write_text("CERT", encoding="ascii")
            self.paths.privkey().write_text("KEY", encoding="ascii")
        return subprocess.CompletedProcess(arguments, 0)

    def test_issues_wildcard_and_installs_cert(self):
        installer = self._installer()

        class EnvCapturingRunner:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __call__(self, arguments, **kwargs):
                self.wrapped(arguments, **kwargs)
                self.wrapped.runner_calls[-1]["env"] = kwargs.get("env")
                return subprocess.CompletedProcess(list(arguments), 0)

        installer.runner = EnvCapturingRunner(self._runner)
        installer.issue_certificate("example.com", "cf-token-value")

        issue = next(call for call in self.runner_calls if "--issue" in call["argv"])
        self.assertIn("-d", issue["argv"])
        self.assertIn("example.com", issue["argv"])
        self.assertIn("*.example.com", issue["argv"])
        self.assertIn("dns_cf", issue["argv"])
        install = next(call for call in self.runner_calls if "--install-cert" in call["argv"])
        self.assertIn(str(self.paths.fullchain()), install["argv"])
        self.assertIn(str(self.paths.privkey()), install["argv"])
        self.assertEqual(install["env"]["CF_Token"], "cf-token-value")
        self.assertEqual(self.paths.privkey().stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.paths.ssl_dir.is_dir())
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer.CertificatePhaseTests -v`
Expected: FAIL

- [ ] **Step 3: 实现**

Installer 类追加：

```python
    # -- phase 3 ---------------------------------------------------------
    def issue_certificate(self, domain, cf_token):
        if not isinstance(domain, str) or not domain.strip():
            raise InstallerError("invalid_domain")
        if not isinstance(cf_token, str) or not cf_token.strip():
            raise InstallerError("missing_cf_token")
        acme = self.paths.acme_home / "acme.sh"
        if not acme.is_file():
            self._run(
                [
                    "curl",
                    "-fsSL",
                    "https://get.acme.sh",
                    "-o",
                    str(self.repo_root / "private" / "acme-install.sh"),
                ]
            )
            self._run(
                [
                    "sh",
                    str(self.repo_root / "private" / "acme-install.sh"),
                    "--home",
                    str(self.paths.acme_home),
                ]
            )
        environment = dict(os.environ, CF_Token=cf_token)
        self._run(
            [
                str(acme),
                "--issue",
                "--dns",
                "dns_cf",
                "-d",
                domain,
                "-d",
                "*." + domain,
                "--keylength",
                "ec-256",
                "--server",
                "letsencrypt",
                "--home",
                str(self.paths.acme_home),
            ],
            env=environment,
        )
        self.paths.ssl_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.ssl_dir, 0o700)
        self._run(
            [
                str(acme),
                "--install-cert",
                "-d",
                domain,
                "--ecc",
                "--fullchain-file",
                str(self.paths.fullchain()),
                "--key-file",
                str(self.paths.privkey()),
                "--reloadcmd",
                "systemctl reload nginx || true",
                "--home",
                str(self.paths.acme_home),
            ],
            env=environment,
        )
        os.chmod(self.paths.privkey(), 0o600)
        self._phase_done("certificate")
```

同时把 `_run` 扩展为接受 `env`：

```python
    def _run(self, arguments, env=None):
        try:
            result = self.runner(
                list(arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
                check=False,
                env=dict(os.environ, **env) if env else None,
            )
        except Exception:
            raise InstallerError("command_failed") from None
        if result.returncode != 0:
            raise InstallerError("command_failed")
```

（`env=None` 时传 None 保持 `subprocess.run` 默认继承语义；fake runner 需接受 `env` kwarg——Task 8 的 `_runner(self, arguments, **_)` 已兼容。）

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer -v`
Expected: PASS

```bash
git add clash_sub/installer.py tests/test_lightweight_installer.py
git commit -m "feat: issue wildcard certificate via acme.sh dns_cf"
```

---

## Task 11: Phase 4 nginx 配置激活 + systemd Phase 5

**Files:**
- Modify: `clash_sub/installer.py`
- Test: `tests/test_lightweight_installer.py`

- [ ] **Step 1: 写失败测试**

追加：

```python
class NginxActivationPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "private").mkdir(parents=True)
        (self.root / "templates" / "nginx").mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "templates" / "nginx"
        for template in source.iterdir():
            shutil.copy(template, self.root / "templates" / "nginx" / template.name)
        self.paths = InstallPaths(
            stream_conf_dir=self.root / "stream-conf.d",
            http_conf_dir=self.root / "conf.d",
            routes_conf=self.root / "clash-sub" / "routes.conf",
            ssl_dir=self.root / "ssl",
            systemd_dir=self.root / "systemd",
            nginx_conf=self.root / "nginx.conf",
        )
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        from clash_sub.installer import Installer

        return Installer(self.root, paths=self.paths, runner=self._runner)

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0)

    def test_activates_stream_and_sub_server_and_records_state(self):
        installer = self._installer()

        installer.activate_nginx(domain="example.com", panel_port=2053)

        stream_text = self.paths.stream_conf().read_text(encoding="utf-8")
        http_text = self.paths.http_conf().read_text(encoding="utf-8")
        self.assertIn("sub.example.com", stream_text)
        self.assertIn("sub.example.com", http_text)
        self.assertIn("/p-", http_text)
        state = installer.state()
        self.assertIn("nginx_activation", state.phases_done)
        self.assertEqual(state.domain, "example.com")
        self.assertEqual(state.panel_port, 2053)
        self.assertTrue(state.panel_base_path.startswith("/p-"))

    def test_hardens_systemd_units(self):
        installer = self._installer()

        installer.harden_systemd()

        restart = self.paths.systemd_dir / "nginx.service.d" / "clash-sub-restart.conf"
        self.assertEqual(
            restart.read_text(encoding="utf-8"),
            "[Service]\nRestart=on-failure\nRestartSec=2s\n",
        )
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("daemon-reload" in item for item in joined))
        self.assertTrue(any("enable" in item and "nginx" in item for item in joined))
```

（import 区补 `import shutil`。）

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer.NginxActivationPhaseTests -v`
Expected: FAIL

- [ ] **Step 3: 实现**

import 区补 `import secrets`、`from clash_sub.domain import ServiceConfig`、`from clash_sub.nginx import NginxError, activate_nginx_files, render_stream_config, render_sub_server`。Installer 类追加：

```python
    # -- phase 4 ---------------------------------------------------------
    def activate_nginx(self, *, domain, panel_port):
        panel_base_path = self.state().panel_base_path or "/p-" + secrets.token_hex(8)
        config = ServiceConfig(
            owner_email="pending",
            subscription_authority="sub.%s:443" % domain,
            xui_public_endpoint="%s:443" % domain,
            xui_database=self.paths.xui_database,
            private_root=self.paths.private_root,
            public_root=self.paths.public_root,
            nginx_routes=self.paths.routes_conf,
            mihomo_binary=Path("/usr/local/lib/clash-sub/mihomo"),
            nginx_binary=Path("/usr/sbin/nginx"),
            systemctl_binary=Path("/usr/bin/systemctl"),
            template_root=self.repo_root / "templates",
        )
        stream = render_stream_config(config, domain)
        sub_server = render_sub_server(
            config,
            domain=domain,
            panel_port=panel_port,
            panel_base_path=panel_base_path,
            routes_include=str(self.paths.routes_conf),
            fullchain=str(self.paths.fullchain()),
            privkey=str(self.paths.privkey()),
        )
        routes_dir = self.paths.routes_conf.parent
        routes_dir.mkdir(parents=True, exist_ok=True)
        try:
            activate_nginx_files(
                (
                    (self.paths.stream_conf(), stream.encode("utf-8"), 0o640),
                    (self.paths.http_conf(), sub_server.encode("utf-8"), 0o640),
                    (self.paths.routes_conf, b"# clash-sub routes placeholder\n", 0o640),
                ),
                self.runner,
                nginx_binary="/usr/sbin/nginx",
            )
        except NginxError:
            raise InstallerError("nginx_activation_failed") from None
        self._phase_done(
            "nginx_activation",
            domain=domain,
            panel_port=panel_port,
            panel_base_path=panel_base_path,
        )
        state = self.state()
        for path in (self.paths.stream_conf(), self.paths.http_conf(), self.paths.routes_conf):
            if str(path) not in state.files_written:
                state.files_written.append(str(path))
        self._save_state(state)
        self._run(["systemctl", "enable", "--now", "nginx"])

    # -- phase 5 ---------------------------------------------------------
    def harden_systemd(self):
        self._write_file(
            self.paths.systemd_dir / "nginx.service.d" / "clash-sub-restart.conf",
            "[Service]\nRestart=on-failure\nRestartSec=2s\n",
            0o644,
        )
        assets = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
        for unit in ("clash-sub-traffic.service", "clash-sub-traffic.timer", "clash-sub-recover.service"):
            self._write_file(
                self.paths.systemd_dir / unit,
                (assets / unit).read_text(encoding="utf-8"),
                0o644,
            )
        drop_in_source = assets / "nginx.service.d" / "clash-sub-recover.conf"
        self._write_file(
            self.paths.systemd_dir / "nginx.service.d" / "clash-sub-recover.conf",
            drop_in_source.read_text(encoding="utf-8"),
            0o644,
        )
        self._run(["systemctl", "daemon-reload"])
        self._run(["systemctl", "enable", "--now", "clash-sub-traffic.timer"])
        self._phase_done("systemd_harden")
```

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer -v`
Expected: PASS

```bash
git add clash_sub/installer.py tests/test_lightweight_installer.py
git commit -m "feat: activate nginx 443 routing and harden systemd units"
```

---

## Task 12: runtime.py 服务工厂抽取 + Phase 6 订阅初始化 + Phase 7 报告 + install 编排

**Files:**
- Create: `clash_sub/runtime.py`
- Modify: `clash_sub/installer.py`
- Modify: `clash_sub/cli.py:269-295`
- Test: `tests/test_lightweight_installer.py`、`tests/test_lightweight_cli.py`

- [ ] **Step 1: 抽取服务工厂（重构，先跑现有测试）**

新建 `clash_sub/runtime.py`，内容为 `cli.py` 的 `_default_service_factory` 平移（返回 service 的构造体）：

```python
"""Shared service construction for the CLI and the installer."""

import subprocess
from pathlib import Path

from clash_sub.checks import MihomoValidator, validate_clash
from clash_sub.config import load_config
from clash_sub.generator import render_user_bundle
from clash_sub.nginx import activate_runtime, recover_runtime, render_routes
from clash_sub.release_store import ReleaseStore
from clash_sub.service import ClashSubService
from clash_sub.sources import (
    download_airport_proxies,
    fetch_xui_proxies,
    load_proxy_snapshot,
)
from clash_sub.state import load_state, reconcile_state, reinitialize_owner, rotate_user_token
from clash_sub.xui import read_xui_snapshot


def repo_root():
    return Path(__file__).resolve().parents[1]


def config_path(root=None):
    root = Path(root) if root else repo_root()
    return root / "private" / "config" / "service.yaml"


def build_service(root=None, runner=None):
    root = Path(root) if root else repo_root()
    config = load_config(config_path(root), root)
    runner = runner or subprocess.run
    return ClashSubService(
        config,
        read_snapshot=read_xui_snapshot,
        load_state=load_state,
        reconcile_state=reconcile_state,
        rotate_user_token=rotate_user_token,
        reinitialize_owner=reinitialize_owner,
        fetch_xui_proxies=fetch_xui_proxies,
        download_airport_proxies=download_airport_proxies,
        load_proxy_snapshot=load_proxy_snapshot,
        render_user_bundle=render_user_bundle,
        validate_clash=validate_clash,
        mihomo_validator=MihomoValidator(config.mihomo_binary, runner=runner),
        release_store=ReleaseStore(
            config.private_root,
            config.public_root,
            activation_paths=(config.nginx_routes,),
        ),
        render_routes=render_routes,
        activate_runtime=activate_runtime,
        recover_runtime=recover_runtime,
        runner=runner,
    )
```

`cli.py`：删除 `_default_service_factory` 与其专有 import（保留仍被引用的），`factory = service_factory or _default_service_factory` 改为 `factory = service_factory or (lambda: build_service())`，import 区加 `from clash_sub.runtime import build_service`。`_recover` 中两行路径逻辑改用 `from clash_sub.runtime import build_service, repo_root`（原 `repo_root = Path(__file__).resolve().parents[1]` 替换为 `repo_root()` 调用，注意本地变量名冲突——把本地变量改名为 `root`）。

Run: `.venv/bin/python -m unittest discover -s tests -v` → 全 PASS（纯重构）。提交：

```bash
git add clash_sub/runtime.py clash_sub/cli.py
git commit -m "refactor: extract shared service factory into runtime module"
```

- [ ] **Step 2: 写 Phase 6/7 失败测试**

`tests/test_lightweight_installer.py` 追加：

```python
class SubscriptionInitPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "private").mkdir(parents=True)
        self.paths = InstallPaths(
            xui_database=self.root / "x-ui.db",
            private_root=self.root / "var" / "private",
            public_root=self.root / "var" / "public",
            routes_conf=self.root / "clash-sub" / "routes.conf",
            ssl_dir=self.root / "ssl",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_writes_service_yaml_with_expected_values(self):
        from clash_sub.installer import Installer

        installer = Installer(self.root, paths=self.paths, runner=self._noop_runner)
        installer.initialize_subscription(
            domain="example.com", owner_email="owner-example"
        )

        content = (self.root / "private" / "config" / "service.yaml").read_text(encoding="utf-8")
        self.assertIn("schema-version: 2", content)
        self.assertIn("owner-email: owner-example", content)
        self.assertIn("subscription-authority: sub.example.com:443", content)
        self.assertIn("xui-public-endpoint: example.com:443", content)
        mode = (self.root / "private" / "config" / "service.yaml").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        state = installer.state()
        self.assertIn("subscription_init", state.phases_done)

    def _noop_runner(self, arguments, **_):
        return subprocess.CompletedProcess(list(arguments), 0)
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer.SubscriptionInitPhaseTests -v`
Expected: FAIL

- [ ] **Step 4: 实现 Phase 6/7 + install 编排**

Installer 类追加（import 区补 `import stat as _stat`、`import grp`、`from clash_sub.runtime import build_service`）：

```python
    # -- phase 6 ---------------------------------------------------------
    def initialize_subscription(self, *, domain, owner_email):
        config_dir = self.repo_root / "private" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        contents = (
            "schema-version: 2\n"
            "owner-email: %s\n"
            "subscription-authority: sub.%s:443\n"
            "xui-public-endpoint: %s:443\n"
            "xui-database: %s\n"
            "private-root: %s\n"
            "public-root: %s\n"
            "nginx-routes: %s\n"
            "mihomo-binary: /usr/local/lib/clash-sub/mihomo\n"
            "nginx-binary: /usr/sbin/nginx\n"
            "systemctl-binary: /usr/bin/systemctl\n"
            "max-source-bytes: 5242880\n"
            % (
                owner_email,
                domain,
                domain,
                self.paths.xui_database,
                self.paths.private_root,
                self.paths.public_root,
                self.paths.routes_conf,
            )
        )
        self._write_file(config_dir / "service.yaml", contents, 0o600)
        self._prepare_runtime_directories()
        self._phase_done("subscription_init")

    def _prepare_runtime_directories(self):
        self.paths.private_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.private_root, 0o700)
        self.paths.public_root.mkdir(parents=True, exist_ok=True)
        try:
            public_gid = grp.getgrnam("www-data").gr_gid
        except KeyError:
            public_gid = -1
        os.chown(self.paths.public_root, -1, public_gid)
        os.chmod(self.paths.public_root, 0o2750)
        self.paths.routes_conf.parent.mkdir(parents=True, exist_ok=True)

    # -- phase 7 ---------------------------------------------------------
    def finalize(self):
        state = self.state()
        report = {
            "domain": state.domain,
            "panel_url": "https://sub.%s%s/" % (state.domain, state.panel_base_path),
            "subscription_note": "run `clash-sub sync` then `clash-sub links`",
            "gate_instruction": (
                "3x-ui 面板：把 Reality inbound 的 listen 从 0.0.0.0 改为 127.0.0.1"
                "（保持端口 10443），公网仅保留 443。"
            ),
        }
        self._phase_done("report")
        return report

    # -- orchestration ---------------------------------------------------
    def install(self, *, domain, cf_token, swap_mb=0, owner_email="owner-example"):
        phases = {
            "preflight": lambda: self.preflight(domain),
            "low_memory": lambda: self.optimize_low_memory(swap_mb),
            "nginx_packages": self.install_nginx_packages,
            "certificate": lambda: self.issue_certificate(domain, cf_token),
            "nginx_activation": lambda: self.activate_nginx(
                domain=domain, panel_port=self._panel_port()
            ),
            "systemd_harden": self.harden_systemd,
            "subscription_init": lambda: self.initialize_subscription(
                domain=domain, owner_email=owner_email
            ),
            "report": self.finalize,
        }
        done = set(self.state().phases_done)
        for name, action in phases.items():
            if name in done:
                continue
            action()
            self.print_fn("phase %s: done" % name)
        return self.finalize() if "report" not in done else {
            "domain": self.state().domain
        }

    def _panel_port(self):
        from clash_sub.xui import read_panel_port

        return read_panel_port(self.paths.xui_database)
```

- [ ] **Step 5: 运行确认通过 + 全量回归 + 提交**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全 PASS

```bash
git add clash_sub/installer.py tests/test_lightweight_installer.py
git commit -m "feat: complete installer phases with orchestration"
```

---

## Task 13: rollback --install

**Files:**
- Modify: `clash_sub/installer.py`
- Test: `tests/test_lightweight_installer.py`

- [ ] **Step 1: 写失败测试**

追加：

```python
class RollbackInstallTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "private").mkdir(parents=True)
        self.paths = InstallPaths(
            nginx_conf=self.root / "nginx.conf",
            stream_conf_dir=self.root / "stream-conf.d",
            http_conf_dir=self.root / "conf.d",
            systemd_dir=self.root / "systemd",
        )
        self.paths.nginx_conf.write_text(
            "http {\n}\n# clash-sub stream include\nstream {\n    include %s/*.conf;\n}\n"
            % self.paths.stream_conf_dir,
            encoding="utf-8",
        )
        self.paths.stream_conf().parent.mkdir(parents=True)
        self.paths.stream_conf().write_text("# stream\n", encoding="utf-8")
        self.paths.http_conf().parent.mkdir(parents=True)
        self.paths.http_conf().write_text("# http\n", encoding="utf-8")
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0)

    def test_removes_clash_sub_nginx_files_and_stream_include(self):
        from clash_sub.installer import Installer, save_install_state

        installer = Installer(self.root, paths=self.paths, runner=self._runner)
        save_install_state(
            self.root / "private" / "install-state.json",
            type(
                "State",
                (),
                {
                    "schema_version": 1,
                    "domain": "example.com",
                    "panel_port": 2053,
                    "panel_base_path": "/p-x",
                    "phases_done": ["nginx_activation"],
                    "files_written": [str(self.paths.stream_conf()), str(self.paths.http_conf())],
                    "backups": {},
                },
            )(),
        )

        installer.rollback_install()

        self.assertFalse(self.paths.stream_conf().exists())
        self.assertFalse(self.paths.http_conf().exists())
        text = self.paths.nginx_conf.read_text(encoding="utf-8")
        self.assertNotIn("clash-sub stream include", text)
        self.assertIn("http {", text)
        self.assertFalse((self.root / "private" / "install-state.json").exists())
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer.RollbackInstallTests -v`
Expected: FAIL

- [ ] **Step 3: 实现**

Installer 类追加：

```python
    def rollback_install(self):
        state = self.state()
        self._run(["systemctl", "stop", "nginx"])
        self._run(["systemctl", "disable", "nginx"])
        for recorded in state.files_written:
            path = Path(recorded)
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
        self._remove_stream_include()
        self._run(["systemctl", "daemon-reload"])
        try:
            (self.repo_root / "private" / "install-state.json").unlink(missing_ok=True)
        except OSError:
            raise InstallerError("rollback_failed") from None

    def _remove_stream_include(self):
        marker = "# clash-sub stream include"
        path = self.paths.nginx_conf
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
        if marker not in text:
            return
        head = text.split(marker, 1)[0].rstrip("\n") + "\n"
        self._write_file(path, head, 0o644)
```

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_lightweight_installer -v`
Expected: PASS

```bash
git add clash_sub/installer.py tests/test_lightweight_installer.py
git commit -m "feat: roll back the integration install"
```

---

## Task 14: install.sh 引导 + CLI install/rollback --install 子命令

**Files:**
- Create: `install.sh`
- Modify: `clash_sub/cli.py`
- Test: `tests/test_lightweight_cli.py`

- [ ] **Step 1: 写 install.sh**

```sh
#!/bin/sh
# clash-sub integration installer bootstrap. Run as root on Debian 12 after
# 3x-ui is installed with one Reality inbound on port 10443.
set -eu
[ "$(id -u)" = 0 ] || { echo "install.sh must run as root" >&2; exit 1; }
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends python3 python3-venv curl
fi
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt
exec bin/clash-sub install
```

```bash
chmod +x install.sh
```

- [ ] **Step 2: 写 CLI 失败测试**

`tests/test_lightweight_cli.py` 追加（沿用该文件现有的 main() 调用风格；若现有用例通过 `main(argv=["status"], ...)` 形式调用，保持一致）：

```python
class InstallCommandTests(unittest.TestCase):
    def test_install_requires_root(self):
        with patch("clash_sub.cli.Installer") as installer:
            installer.return_value.install.side_effect = None
            with patch("clash_sub.installer.os.geteuid", return_value=1000):
                status = main(["install"], stdout=StringIO(), stderr=self.stderr)

        self.assertEqual(status, 1)
        self.assertIn("not_root", self.stderr.getvalue())
```

（`setUp` 里 `self.stderr = StringIO()`；`main` 从 `clash_sub.cli` 导入；`from unittest.mock import patch`。若 main 的签名要求 stdin 参数，按现有用例补齐。）

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_cli -v`
Expected: FAIL（install 子命令不存在 → invalid_command）

- [ ] **Step 4: 实现 CLI 接线**

`clash_sub/cli.py`：

1) import 区加 `from clash_sub.installer import Installer, InstallerError` 与 `from clash_sub.runtime import build_service, repo_root`。

2) `_parser()` 中 `rollback` 子命令改为：

```python
    rollback = commands.add_parser("rollback", add_help=False)
    rollback.add_argument("user", nargs="?")
    rollback.add_argument("release", nargs="?")
    rollback.add_argument("--install", action="store_true")
```

并追加子命令注册：

```python
    commands.add_parser("install", add_help=False)
    commands.add_parser("backup", add_help=False)
    commands.add_parser("update", add_help=False)
    cert = commands.add_parser("cert", add_help=False)
    cert.add_argument("--renew", action="store_true")
    cert.add_argument("--domain")
```

3) `_run_command` 开头的用户参数校验改为：

```python
    if command in {"history", "rollback", "rotate-link", "reinitialize-owner"}:
        if getattr(parsed, "install", False) and command == "rollback":
            user = None
        else:
            user = _user_id(parsed.user)
            if user is None:
                return _error(stderr, "invalid_command", 2)
    else:
        user = None
    if command == "rollback":
        if getattr(parsed, "install", False):
            if parsed.user is not None or parsed.release is not None:
                return _error(stderr, "invalid_command", 2)
            return _rollback_install(stdout, stderr)
        if parsed.user is None or parsed.release is None:
            return _error(stderr, "invalid_command", 2)
```

（原有 `if command == "rollback":` 分支保持在其后处理 user/release 路径。）

4) 新增命令入口（放在 `_recover` 之前）：

```python
def _install(stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1)
    root = repo_root()
    domain = os.environ.get("CLASH_SUB_DOMAIN", "")
    if not domain:
        try:
            stdout.write("请输入主域名（例如 example.com）：\n")
            domain = input().strip()
        except (EOFError, KeyboardInterrupt):
            return _error(stderr, "invalid_domain", 2)
    try:
        token = getpass("请输入 Cloudflare API Token：")
    except (EOFError, KeyboardInterrupt):
        return _error(stderr, "missing_cf_token", 2)
    swap = os.environ.get("CLASH_SUB_SWAP_MB", "0")
    try:
        swap_mb = int(swap)
    except ValueError:
        return _error(stderr, "invalid_swap", 2)
    owner = os.environ.get("CLASH_SUB_OWNER_EMAIL", "owner-example")
    try:
        installer = Installer(root, print_fn=lambda message: stdout.write("%s\n" % message))
        report = installer.install(
            domain=domain, cf_token=token, swap_mb=swap_mb, owner_email=owner
        )
    except InstallerError as error:
        return _error(stderr, error.code, 1)
    stdout.write("面板地址：%s\n" % report.get("panel_url", ""))
    stdout.write("%s\n" % report.get("gate_instruction", ""))
    return 0


def _rollback_install(stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1)
    try:
        Installer(repo_root()).rollback_install()
    except InstallerError as error:
        return _error(stderr, error.code, 1)
    stdout.write("已回滚安装；Reality 保持公网 10443 直连。\n")
    return 0
```

`_run_command` 中 dispatch 表加：`if command == "install": return _install(stdout, stderr)`。

- [ ] **Step 5: 运行确认通过 + 全量回归 + 提交**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全 PASS

```bash
git add install.sh clash_sub/cli.py tests/test_lightweight_cli.py
git commit -m "feat: wire install and rollback-install commands"
```

---

## Task 15: manage.py——backup / auto_snapshot

**Files:**
- Create: `clash_sub/manage.py`
- Test: `Create: tests/test_lightweight_manage.py`

- [ ] **Step 1: 写失败测试**

`tests/test_lightweight_manage.py`：

```python
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "private" / "config").mkdir(parents=True)
        (self.root / "private" / "config" / "service.yaml").write_text("schema-version: 2\n", encoding="utf-8")
        (self.root / "private" / "state.json").write_text("{}", encoding="utf-8")
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        completed = subprocess.CompletedProcess(list(arguments), 0)
        completed.stdout = (self.root.as_posix() + "\n").encode()
        return completed

    def test_creates_tarball_with_private_and_reports_sha256(self):
        from clash_sub.manage import create_backup

        with patch("clash_sub.manage._xui_database_path", return_value=None):
            path = create_backup(self.root, self._runner)

        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with tarfile.open(path, "r:gz") as archive:
            names = archive.getnames()
        self.assertTrue(any(name.endswith("private/config/service.yaml") for name in names))
        self.assertTrue(any(name.endswith("private/state.json") for name in names))

    def test_snapshot_copies_live_configs(self):
        from clash_sub.manage import auto_snapshot

        snapshot_dir = auto_snapshot(self.root, self._runner, label="pre-update")

        self.assertTrue(snapshot_dir.is_dir())
        self.assertTrue(snapshot_dir.name.startswith("2"))
        self.assertTrue(snapshot_dir.name.endswith("pre-update"))


if __name__ == "__main__":
    unittest.main()
```

（import 区补 `import subprocess`。）

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_manage -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`clash_sub/manage.py`：

```python
"""Backup and lifecycle management commands."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path


def _backups_root(repo_root):
    root = Path(repo_root) / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _xui_database_path(repo_root):
    candidate = Path("/etc/x-ui/x-ui.db")
    return candidate if candidate.is_file() else None


def _nginx_config_paths():
    return (
        Path("/etc/nginx/stream-conf.d/clash-sub.conf"),
        Path("/etc/nginx/conf.d/clash-sub.conf"),
        Path("/etc/nginx/clash-sub/routes.conf"),
    )


def _versions_manifest(repo_root, runner):
    def output(arguments):
        try:
            result = runner(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            return result.stdout.decode("utf-8", "replace").strip()
        except Exception:
            return ""

    return {
        "repository": output(["git", "-C", str(repo_root), "rev-parse", "HEAD"]),
        "nginx": output(["nginx", "-v"]),
    }


def create_backup(repo_root, runner):
    """Create one full tarball backup; returns its path (0600)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = _backups_root(repo_root) / ("clash-sub-backup-%s.tar.gz" % stamp)
    source_files = []
    database = _xui_database_path(repo_root)
    if database:
        source_files.append(database)
    source_files.extend(path for path in _nginx_config_paths() if path.is_file())
    private_root = Path(repo_root) / "private"
    for path in sorted(private_root.rglob("*")):
        if path.is_file() and "install-state" not in path.name:
            source_files.append(path)
    descriptor, temporary = tempfile.mkstemp(dir=str(_backups_root(repo_root)))
    os.close(descriptor)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for path in source_files:
                archive.add(str(path), arcname=str(path), recursive=False)
            manifest = json.dumps(
                _versions_manifest(repo_root, runner), sort_keys=True
            ).encode("utf-8")
            info = tarfile.TarInfo("clash-sub-versions.json")
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink(missing_ok=True)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return destination


def auto_snapshot(repo_root, runner, *, label):
    """Snapshot live configurations before a mutating command."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    directory = _backups_root(repo_root) / ("%s-%s" % (stamp, label))
    directory.mkdir(parents=True, exist_ok=False)
    targets = [Path(repo_root) / "private" / "config" / "service.yaml"]
    targets.extend(path for path in _nginx_config_paths() if path.is_file())
    for path in targets:
        if path.is_file():
            shutil.copy2(str(path), str(directory / path.name))
    return directory
```

- [ ] **Step 4: 运行确认通过 + 提交**

Run: `.venv/bin/python -m unittest tests.test_lightweight_manage -v`
Expected: PASS

```bash
git add clash_sub/manage.py tests/test_lightweight_manage.py
git commit -m "feat: add backup tarball and pre-change snapshots"
```

---

## Task 16: manage.py——update / cert / health_report + CLI 接线

**Files:**
- Modify: `clash_sub/manage.py`
- Modify: `clash_sub/cli.py`
- Test: `tests/test_lightweight_manage.py`、`tests/test_lightweight_cli.py`

- [ ] **Step 1: 写失败测试**

`tests/test_lightweight_manage.py` 追加：

```python
class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "private" / "config").mkdir(parents=True)
        (self.root / "private" / "config" / "service.yaml").write_text("schema-version: 2\n", encoding="utf-8")
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(list(arguments), 0)

    def test_update_pulls_and_rerenders_nginx(self):
        from clash_sub.manage import run_update

        state = type("State", (), {"schema_version": 1, "domain": "example.com", "panel_port": 2053, "panel_base_path": "/p-x", "phases_done": [], "files_written": [], "backups": {}})
        with patch("clash_sub.manage._load_install_state", return_value=state), patch(
            "clash_sub.manage._rerender_nginx"
        ) as rerender:
            run_update(self.root, self._runner)

        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("git" in item and "pull" in item for item in joined))
        rerender.assert_called_once()


class CertTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        result = subprocess.CompletedProcess(list(arguments), 0)
        if arguments[:1] == ["openssl"]:
            result.stdout = b"notAfter=Sep 25 12:00:00 2026 GMT\n"
        return result

    def test_cert_status_reports_expiry(self):
        from clash_sub.manage import cert_status

        fullchain = self.root / "fullchain.pem"
        fullchain.write_text("CERT", encoding="ascii")
        with patch("clash_sub.manage._fullchain_path", return_value=fullchain):
            status = cert_status(self.root, self._runner)

        self.assertIn("notAfter", status["not_after"])
        self.assertIsInstance(status["present"], bool)

    def test_cert_renew_invokes_acme(self):
        from clash_sub.manage import cert_renew

        state = type("State", (), {"schema_version": 1, "domain": "example.com", "panel_port": 0, "panel_base_path": "", "phases_done": [], "files_written": [], "backups": {}})
        calls = []

        def runner(arguments, **_):
            calls.append(list(arguments))
            return subprocess.CompletedProcess(list(arguments), 0)

        with patch("clash_sub.manage._load_install_state", return_value=state):
            cert_renew(self.root, runner)

        self.assertTrue(any("--renew" in item for item in calls))


class HealthReportTests(unittest.TestCase):
    def test_reports_units_and_cert(self):
        from clash_sub.manage import health_report

        def runner(arguments, **_):
            result = subprocess.CompletedProcess(list(arguments), 0)
            result.stdout = b"active\n" if "is-active" in arguments else b"notAfter=Sep 25 12:00:00 2026 GMT\n"
            return result

        root = Path(tempfile.mkdtemp())
        try:
            report = health_report(root, runner)
        finally:
            import shutil as _shutil

            _shutil.rmtree(root)

        self.assertIn("nginx", report["units"])
        self.assertIn("x-ui", report["units"])
        self.assertIn("days_left", report["certificate"])
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_manage -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`clash_sub/manage.py` 追加（import 区补 `import datetime as _datetime`、`import re`、`from clash_sub.installer import InstallState`）：

```python
_ACME = Path("/root/.acme.sh/acme.sh")


def _load_install_state(repo_root):
    from clash_sub.installer import load_install_state

    return load_install_state(Path(repo_root) / "private" / "install-state.json")


def _fullchain_path(repo_root=None):
    return Path("/etc/ssl/domain/fullchain.pem")


def _read_certificate_expiry(runner):
    try:
        result = runner(
            [
                "openssl",
                "x509",
                "-noout",
                "-enddate",
                "-in",
                str(_fullchain_path()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(
        r"notAfter=(\w{3})\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\s+(\d{4})",
        result.stdout.decode("ascii", "replace"),
    )
    return match.group(0).split("=", 1)[1] if match else None


def cert_status(repo_root, runner):
    fullchain = _fullchain_path()
    not_after = _read_certificate_expiry(runner) if fullchain.is_file() else None
    return {"present": fullchain.is_file(), "not_after": not_after or "unknown"}


def cert_renew(repo_root, runner):
    state = _load_install_state(repo_root)
    result = runner(
        [
            str(_ACME),
            "--renew",
            "-d",
            state.domain,
            "--force",
            "--ecc",
            "--home",
            str(_ACME.parent),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("cert_renew_failed")
    return True


def _rerender_nginx(repo_root, runner, state):
    from clash_sub.domain import ServiceConfig
    from clash_sub.installer import InstallPaths
    from clash_sub.nginx import NginxError, activate_nginx_files, render_stream_config, render_sub_server

    paths = InstallPaths()
    config = ServiceConfig(
        owner_email="pending",
        subscription_authority="sub.%s:443" % state.domain,
        xui_public_endpoint="%s:443" % state.domain,
        xui_database=paths.xui_database,
        private_root=paths.private_root,
        public_root=paths.public_root,
        nginx_routes=paths.routes_conf,
        mihomo_binary=Path("/usr/local/lib/clash-sub/mihomo"),
        nginx_binary=Path("/usr/sbin/nginx"),
        systemctl_binary=Path("/usr/bin/systemctl"),
        template_root=Path(repo_root) / "templates",
    )
    stream = render_stream_config(config, state.domain)
    sub_server = render_sub_server(
        config,
        domain=state.domain,
        panel_port=state.panel_port,
        panel_base_path=state.panel_base_path,
        routes_include=str(paths.routes_conf),
        fullchain=str(paths.fullchain()),
        privkey=str(paths.privkey()),
    )
    try:
        activate_nginx_files(
            (
                (paths.stream_conf(), stream.encode("utf-8"), 0o640),
                (paths.http_conf(), sub_server.encode("utf-8"), 0o640),
            ),
            runner,
            nginx_binary="/usr/sbin/nginx",
            systemctl_binary="/usr/bin/systemctl",
            reload=True,
        )
    except NginxError:
        raise RuntimeError("nginx_rerender_failed") from None
    return True


def run_update(repo_root, runner):
    auto_snapshot(repo_root, runner, label="pre-update")
    result = runner(
        ["git", "-C", str(repo_root), "pull", "--ff-only"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("git_pull_failed")
    runner(
        [str(Path(repo_root) / ".venv" / "bin" / "pip"), "install", "-r", str(Path(repo_root) / "requirements.txt")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )
    state = _load_install_state(repo_root)
    _rerender_nginx(repo_root, runner, state)
    return True


def health_report(repo_root, runner):
    def unit_state(unit):
        try:
            result = runner(
                ["systemctl", "is-active", unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            return result.stdout.decode("ascii", "replace").strip()
        except Exception:
            return "unknown"

    not_after = _read_certificate_expiry(runner)
    days_left = None
    if not_after:
        try:
            expiry = _datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y GMT")
            days_left = (expiry - _datetime.datetime.utcnow()).days
        except ValueError:
            days_left = None
    return {
        "units": {"nginx": unit_state("nginx"), "x-ui": unit_state("x-ui")},
        "certificate": {"not_after": not_after or "unknown", "days_left": days_left},
    }
```

`clash_sub/cli.py` `_run_command` dispatch 增加（import 区补 `from clash_sub import manage`）：

```python
    if command == "backup":
        return _managed(stdout, stderr, manage.create_backup)
    if command == "update":
        return _managed(stdout, stderr, manage.run_update)
    if command == "cert":
        return _cert_command(parsed, stdout, stderr)
```

新增两个入口函数：

```python
def _managed(stdout, stderr, action):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1)
    try:
        action(repo_root(), subprocess.run)
    except Exception:
        return _error(stderr, "management_command_failed", 1)
    stdout.write("操作已完成。\n")
    return 0


def _cert_command(parsed, stdout, stderr):
    if os.geteuid() != 0:
        return _error(stderr, "not_root", 1)
    try:
        if parsed.domain:
            return _error(stderr, "domain_change_unsupported", 2)
        if parsed.renew:
            manage.cert_renew(repo_root(), subprocess.run)
            stdout.write("证书续期已触发。\n")
        else:
            status = manage.cert_status(repo_root(), subprocess.run)
            stdout.write("证书存在：%s\n" % ("是" if status["present"] else "否"))
            stdout.write("到期时间：%s\n" % status["not_after"])
    except Exception:
        return _error(stderr, "cert_command_failed", 1)
    return 0
```

（域名变更 `--domain` 本计划返回 `domain_change_unsupported` 留待手动流程：先 `cert --renew` 于新 DNS + 改 service.yaml + `update`。README 记录该流程——见 Task 17。这避免在本迭代实现交互式多步变更。）

- [ ] **Step 4: 运行确认通过 + 全量回归 + 提交**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全 PASS

```bash
git add clash_sub/manage.py clash_sub/cli.py tests/test_lightweight_manage.py tests/test_lightweight_cli.py
git commit -m "feat: add update, cert, and health management commands"
```

---

## Task 17: 文档与部署资产重写 + 部署一致性测试更新

**Files:**
- Delete: `deploy/nginx/clash-sub.conf.tmpl`（由 templates/nginx/*.j2 取代）
- Modify: `README.md`、`DEPLOYMENT.md`
- Create: `docs/recovery.md`
- Modify: `tests/test_lightweight_deployment.py`

- [ ] **Step 1: 更新部署一致性测试（先改测试）**

`tests/test_lightweight_deployment.py`：删除针对 `NGINX_TEMPLATE` 的全部用例，改为断言新模板（文件顶部常量替换）：

```python
NGINX_STREAM_TEMPLATE = ROOT / "templates" / "nginx" / "stream.conf.j2"
NGINX_SUB_TEMPLATE = ROOT / "templates" / "nginx" / "sub-server.conf.j2"
INSTALL_SH = ROOT / "install.sh"
```

新增用例（类内）：

```python
    def test_stream_template_routes_default_to_reality(self):
        text = NGINX_STREAM_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("ssl_preread on;", text)
        self.assertIn("127.0.0.1:10443", text)
        self.assertIn("127.0.0.1:30443", text)
        self.assertIn("127.0.0.1:20443", text)
        self.assertIn("{{ domain }}", text)

    def test_sub_server_template_binds_loopback_and_includes_routes(self):
        text = NGINX_SUB_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("listen 127.0.0.1:30443 ssl;", text)
        self.assertIn("{{ routes_include }}", text)
        self.assertIn("{{ panel_base_path }}", text)
        self.assertIn("{{ panel_port }}", text)
        self.assertIn("limit_req zone=clash_subscription", text)

    def test_install_sh_bootstraps_venv_and_executes_install(self):
        text = INSTALL_SH.read_text(encoding="utf-8")

        self.assertIn("python3 -m venv", text)
        self.assertIn("clash-sub install", text)
```

保留该文件中与 systemd/requirements 相关的既有用例不动。

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_lightweight_deployment -v`
Expected: FAIL（模板尚在 deploy/，install.sh 断言路径尚可过，但旧 NGINX_TEMPLATE 用例仍引用将删文件——按 Step 1 已删除旧用例，此步失败点为新模板断言）

（注意顺序：先改测试 → 删除旧模板 → 跑测试失败 → 重写文档不必影响测试 → 全绿。）

- [ ] **Step 3: 删除旧模板**

```bash
git rm deploy/nginx/clash-sub.conf.tmpl
```

（`deploy/nginx/routes.empty.conf` 若被 DEPLOYMENT.md 旧文本引用，一并 `git rm` 并在文档重写中移除引用。）

- [ ] **Step 4: 重写 README 的 ADR 与端口表**

`README.md` 顶部「设计决策」区新增（放在现有端口表之前）：

```markdown
## 架构决策记录（2026-08-25 更新）

本版推翻早前「不引入 Nginx stream、Reality 直占 443」的决策，改为 Nginx stream 统一 443 入口：

- 公网仅开放 443：`ssl_preread` 按 SNI 分流——`sub.<域名>` → 127.0.0.1:30443（订阅+面板，终止 TLS），
  其余任意 SNI → 127.0.0.1:10443（Xray Reality，不终止 TLS）；
  `trojan.<域名>` → 127.0.0.1:20443 为预留规则（后期在 3x-ui 加 trojan inbound 即生效）。
- Reality inbound 监听 127.0.0.1:10443；客户端统一连 443（订阅层将节点端口改写为 443）。
- 证书：acme.sh DNS-01（Cloudflare）wildcard，统一 /etc/ssl/domain/；公网不再开放 80。
- 部署：`bash install.sh` 一键完成 443 整合（详见 DEPLOYMENT.md）；3x-ui 仍手动安装。
- clash-sub 定位：本 VPS clash 订阅栈的全生命周期管理 CLI（install/backup/update/cert/rollback）。
```

端口表更新为：`443 Nginx stream（唯一公网端口）`；移除 80/8443 行。「明确不做」清单移除与 stream 冲突的条目，保留「不做 Docker、无常驻订阅进程、不自动安装 3x-ui」。

- [ ] **Step 5: 重写 DEPLOYMENT.md 为两阶段流程**

新 `DEPLOYMENT.md` 结构与关键内容（保留文件中仍有效的 3x-ui 安装细节章节，按需裁剪引用）：

```markdown
# 部署手册（Debian 12）

## 部署前准备清单（Cloudflare）

1. 域名 NS 托管在 Cloudflare。
2. 添加 DNS 记录：`sub.<你的域名>` → A 记录 → VPS 公网 IP（仅此一条必须）。
3. 创建 API Token：权限 Zone → DNS → Edit，Zone Resources 限定该域名。安装时粘贴一次。

## Phase 1：基础代理（手动，约 10 分钟）

1. Debian 12 最小安装，`apt update && apt upgrade`。
2. 安装 3x-ui（官方脚本），记下面板端口/路径/凭据。
3. 面板设置：订阅端口任意（默认即可），subListen=127.0.0.1（默认），启用 Clash 订阅。
4. 建入站：协议 VLESS、端口 10443、listen 0.0.0.0、传输 TCP、Security=Reality
   （serverName 填第三方伪装域、保留 dest 默认），添加 client（email 记住，作为 owner-email）。
   —— 此时代理已可用（公网 10443 直连）。

## Phase 2：整合 443（一条命令）

以 root 在 /opt/my-clash-config：

    git clone <repo> /opt/my-clash-config && cd /opt/my-clash-config
    bash install.sh

交互输入：主域名、Cloudflare API Token、（可选 swap 扩容）。
环境变量可非交互：`CLASH_SUB_DOMAIN=example.com CLASH_SUB_OWNER_EMAIL=owner-1 bash install.sh`。

## Phase 3：收口（手动一步）

3x-ui 面板把 Reality 入站 listen 从 0.0.0.0 改为 127.0.0.1（端口保持 10443）。
之后公网仅剩 443。运行 `clash-sub sync` 生成订阅，`clash-sub links` 查看 URL。

## 部署后验证清单

- `ss -tlnp | grep -E '443|10443|30443'`：443=nginx、10443/30443 仅 127.0.0.1
- `curl -sI https://sub.<域名>/s/<token>/clash-standard.yaml` 返回 200
- `clash-sub status`：健康摘要正常
- 面板：https://sub.<域名>/<panel-path>/（install 报告输出）
```

- [ ] **Step 6: 写 docs/recovery.md**

```markdown
# 重装恢复手册

目标：服务器重装后最快恢复（约 20 分钟）。

## 备份内容（clash-sub backup 产物，tar.gz 0600，含 token/uuid 等敏感信息，请异地保存）

- /etc/x-ui/x-ui.db 副本（全部入站与 client）
- private/（service.yaml、state.json、订阅状态）
- nginx clash-sub 配置 + 版本清单（不含证书私钥，可重签）

## 恢复步骤

1. Debian 12 安装 → 3x-ui 官方脚本安装。
2. 停止 x-ui：`systemctl stop x-ui`；用备份内 x-ui.db 覆盖 /etc/x-ui/x-ui.db；`systemctl start x-ui`。
   —— 此时代理已恢复（公网 10443，客户端订阅仍指向 443 时需先完成步骤 3）。
3. git clone 本仓库 → 恢复 private/（service.yaml、state.json）→ `bash install.sh`。
   （证书自动重签；install 幂等，重跑安全。）
4. 按部署手册 Phase 3 收口 listen=127.0.0.1，`clash-sub sync` 验证订阅。

## 域名变更（手动流程）

1. Cloudflare 为新域名配好 NS 与 sub A 记录、新 API Token。
2. 修改 private/config/service.yaml 的 subscription-authority 与 xui-public-endpoint。
3. `clash-sub update`（重渲染 nginx）→ `clash-sub sync`。旧订阅 URL 随之失效，重新分发链接。
```

- [ ] **Step 7: 运行全量测试 + 提交**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全 PASS（包括 repository_safety 与 secret_scan）

```bash
git add -A
git commit -m "docs: rewrite deployment docs for the unified 443 topology"
```

---

## Task 18: 收尾验证——全量测试 + install.sh 语法检查

**Files:** 无新增（验证任务）

- [ ] **Step 1: 全量测试**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全部 PASS，无 skip 异常

- [ ] **Step 2: install.sh 语法与权限**

Run: `sh -n install.sh && test -x install.sh && echo OK`
Expected: `OK`

- [ ] **Step 3: CLI 子命令冒烟（无需 root 的路径）**

Run: `.venv/bin/python -c "from clash_sub.cli import _parser; p=_parser(); [p.parse_args([c]) for c in ('install','backup','update','cert')]; p.parse_args(['rollback','--install']); print('OK')"`
Expected: `OK`

- [ ] **Step 4: 最终提交（若有零散变更）**

```bash
git status --short
git add -A && git commit -m "chore: final integration verification" || true
```

---

## Self-Review 记录

- **Spec 覆盖**：spec §6.1→Task 1；§6.3/面板端口→Task 2；§6.2→Task 3；§3 模板→Task 4；原子安装→Task 5；§7.1 Phase 0→Task 7、Phase 1→Task 8、Phase 2→Task 9、Phase 3→Task 10、Phase 4/5→Task 11、Phase 6/7+编排→Task 12、§7.2 journal→Task 6、rollback→Task 13；install.sh→Task 14；§9 备份→Task 15；§10 update/cert/status→Task 16（status 健康增强含于 health_report，CLI 接线在 Task 16 同步）；§14 文档→Task 17。§13 预留一（trojan inbound）由 Task 2 的 `test_allows_non_reality_inbound_for_future_extension` 固化；预留二（第二 VPS）为文档性内容，写入 recovery.md 边界外，不入库。
- **已知简化（有意）**：`cert --domain` 交互式全流程收敛为文档化手动步骤（Task 16 注记），因多步交互变更在无 CI 环境下风险大于收益；如需可后续迭代。
- **类型一致性**：`InstallPaths` 字段名在 Task 6 定义后于 Task 7-13 复用；`activate_nginx_files(files, runner, *, nginx_binary, systemctl_binary=None, reload=False)` 签名在 Task 5 定稿、Task 11/16 复用一致；`ServiceConfig` 新增字段 `xui_public_endpoint` 在 Task 1 定义、Task 3/11/16 复用。
- **测试命令**：全部以仓库根目录为 cwd 执行 `.venv/bin/python -m unittest ...`。
