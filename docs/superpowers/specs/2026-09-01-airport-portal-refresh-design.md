# 机场订阅页面自动刷新设计

## 目标

在现有机场来源持久化与 `AmyTelecom.yaml` 原子更新能力上，增加一套可在 Debian 服务器运行的纯 HTTP 订阅页面适配器，实现：

- 通过带 `sid/token` 的订阅开关页面自动开启订阅；
- 优先复用已经保存的真实订阅链接；
- 旧链接失效时自动从页面生成新链接；
- 页面自动化失效时仍可人工开启订阅或直接输入新链接；
- 每 7 天自动刷新一次服务器上的机场配置；
- 下载结果只经过传输与明显错误页面检查，保存时保持原始字节、顺序与注释不变。

本设计只影响机场订阅链路。owner/member 发布矩阵、主配置生成、模板注释、Home 客户端脚本、Mihomo 校验、release 与 Nginx 路由契约保持不变。

## 已验证的页面协议

当前机场页面无需账号、密码或持久 Cookie。在无痕环境中，仅凭订阅开关页面 URL 即可访问。

纯 HTTP 实测确认以下流程可以运行：

1. GET `/Subscription/index` 返回订阅页面并开启订阅；
2. 页面中的 `Clash1_Anyttls` 按钮调用 `/Subscription/GetSubscription`；
3. 第一次 POST 可能直接返回订阅链接，也可能返回任务编号；
4. 返回任务编号时，等待页面指定时间后再次 POST；
5. 第二次 POST 返回新的真实订阅链接；
6. 新旧真实订阅链接字符串不同，但实测下载正文完全一致，并且都包含 `Subscription-Userinfo`。

页面中的确认弹窗只负责浏览器复制提示，不参与服务器端链接生成，因此服务端不需要浏览器或图形界面。

## 架构

新增独立的页面适配器：

```text
CLI / systemd timer
        │
        ▼
ClashSubService
        │
        ├── AirportPortalClient
        │     ├── 开启订阅
        │     └── 必要时生成真实订阅链接
        │
        ├── download_airport_document
        │     ├── 下载原始正文
        │     ├── 基础响应检查
        │     └── 捕获流量信息
        │
        └── AirportStore
              └── 以可恢复双文件事务更新来源记录与 AmyTelecom.yaml
```

建议新增 `clash_sub/airport_portal.py`。页面 HTML、生成任务和接口响应的处理全部封装在该文件中，不进入通用下载模块。页面改版时只修改适配器；人工链接更新链路不依赖该适配器。

不使用无头浏览器，不执行远端 JavaScript，不新增 Chromium 或其他浏览器依赖。

## 机场菜单

最终菜单为：

```text
机场订阅

1. 设置订阅开关页面
2. 自动开启订阅并刷新（推荐）
3. 手动开启订阅后刷新
4. 使用新订阅链接更新
5. 查看机场状态
0. 返回
```

### 1. 设置订阅开关页面

菜单标题保持简洁，进入后显示完整说明：

```text
此操作会验证订阅开关页面、生成新的订阅链接，
并在下载成功后更新 AmyTelecom.yaml。
任一步失败都会保留当前配置。

请输入订阅开关页面：
```

URL 使用可见交互输入，清除首尾空白，不接受命令行 URL 参数。

每次执行都强制完成全链路验证：

```text
输入新的 activation_url
→ GET 页面并开启订阅
→ 解析指定 AnyTLS Clash 按钮
→ 创建链接生成任务
→ 必要时等待并查询任务
→ 获得新的 source_url
→ 下载并检查响应
→ 捕获流量信息
→ 事务更新来源记录与 AmyTelecom.yaml
```

不得优先复用旧 `source_url`，因为该操作必须确认新的开关页面具备完整的链接生成能力。任一步失败都保留旧开关页面、旧真实链接、旧机场文件、旧流量和旧成功时间。

### 2. 自动开启订阅并刷新

这是日常推荐操作，也是定时任务复用的服务方法。

```text
读取 activation_url 与 source_url
→ 尝试访问开关页面
→ 优先使用旧 source_url 下载
```

分支行为：

- 页面成功、旧链接成功：更新正文、流量和时间，不生成新链接；
- 页面成功、旧链接失败：使用已经取得的页面生成新链接，再次下载；
- 页面临时不可用、旧链接成功：使用旧链接正常更新；
- 页面临时不可用、旧链接失败：本次跳过，完整保留旧数据；
- 页面可访问但结构不兼容、旧链接失败：安全失败，提示使用菜单 4；
- 未配置开关页面：交互操作提示先使用菜单 1，定时任务直接跳过。

页面临时不可用时不尝试生成新链接，因为生成任务依赖该页面提供的动态参数。

### 3. 手动开启订阅后刷新

交互提示为：

```text
请在浏览器中手动打开机场订阅开关页面。
确认订阅已开启后，按回车继续；输入 0 取消：
```

程序不得重新显示保存的开关页面地址。用户确认后，程序只下载已保存的 `source_url`，不访问或解析订阅页面，也不生成新链接。

下载成功后更新正文、流量和时间；失败时保留旧数据，并提示使用菜单 4。

### 4. 使用新订阅链接更新

进入后提示：

```text
此操作不会访问订阅开关页面。
下载成功后会保存新订阅链接，并更新 AmyTelecom.yaml。

请输入新的机场订阅链接：
```

URL 使用可见交互输入，清除首尾空白，不接受命令行 URL 参数。

该操作完全绕过页面适配器：

```text
输入新的 source_url
→ 下载并检查响应
→ 保留现有 activation_url
→ 事务更新 source_url、正文、流量和时间
```

如果尚无机场来源记录，则以 `activation_url=null` 创建记录。该操作是页面改版或自动化长期失效时的永久人工兜底。

### 5. 查看机场状态

只显示：

- 订阅开关页面是否已配置；
- 真实订阅链接是否已配置；
- 开关页面来源主机；
- 真实订阅来源主机；
- 总量、已用量、剩余量；
- 到期时间；
- 最近成功时间；
- `AmyTelecom.yaml` 是否存在。

不得显示 URL 路径、查询参数、Token、任务编号或接口响应。

## 私密来源记录

继续使用：

```text
/var/lib/clash-sub/private/airport-source.json
```

文件保持 `0600 root:root`、常规文件、硬链接数 1、不可为 symlink。

Schema 升级为版本 2：

```json
{
  "schema_version": 2,
  "activation_url": "https://example.invalid/Subscription/index?sid=placeholder&token=placeholder",
  "source_url": "https://example.invalid/subscription-placeholder",
  "traffic": {
    "upload": 1,
    "download": 2,
    "total": 3,
    "expire": 4
  },
  "last_success": 1788192000
}
```

规则：

- `activation_url` 可以为 `null`；
- `source_url` 必须与已保存的机场正文和成功时间成套存在；
- `traffic` 可以为 `null`；
- 流量缺失或非法时，新记录保存 `traffic=null`，不得沿用旧流量；
- 两个 URL 都是私密凭据；
- Schema v1 不迁移，测试服务器采用全新部署。

`AirportStore.replace(document, source)` 继续将来源记录与机场正文作为可恢复的双文件事务更新。所有成功的正文更新都经过该事务；菜单 1 和菜单 4 同时更新对应 URL，菜单 2 和菜单 3 保留未变的 URL。

来源记录在业务上是更新字段，在文件系统上仍采用同目录临时文件加 `os.replace` 整体替换：先读取旧记录，保留未变化的字段，构造完整新记录，写入并同步临时文件后再替换正式 `airport-source.json`。不得直接打开正式 JSON 原地覆盖，以免崩溃或并发读取留下空文件、半截 JSON。

现有事务的正式文件切换顺序保持为：

1. 替换 `public/provider/AmyTelecom.yaml`；
2. 替换 `private/airport-source.json`；
3. 同步两个目录并清理事务记录；
4. 第二步或后续持久化失败时，根据事务记录恢复旧组合。

这不是两个路径同时可见的严格原子切换。两次替换之间存在毫秒级窗口，一次恰好并发的订阅请求可能取得新机场正文和旧流量响应头；后续请求恢复一致，失败恢复后也只保留完整旧组合。该短暂的响应头与正文错位可以接受，不引入全局读取锁、版本目录或指针文件。

## 基于当前主分支的接入约束

当前代码尚未包含本设计，实施时必须同时完成以下接入，不能只新增页面适配器：

- `airport_source.py` 当前是精确键集合的 Schema v1。升级为 v2 时，`AirportSource` 模型、序列化、解析、所有构造位置和测试 fixture 必须同时增加 `activation_url`；`metadata.py` 继续只读取其中的 `traffic`，但必须能读取 v2；
- `service.py` 现有 `replace_airport_source(url)`、`refresh_airport()` 和 `airport_status()` 分别演进为菜单 4、菜单 3 和菜单 5 的能力；新增菜单 1 的 `configure_airport_portal(url)` 与菜单 2/定时任务共用的 `auto_refresh_airport()`，避免建立两套自动刷新状态机；
- 现有下载器保持“下载原始字节、只拒绝传输失败和空响应”的方向，在其上增加明显 HTML 拒绝，不恢复曾经删除的 YAML 结构解析与 Mihomo provider 探针；
- `generator.py` 与 `checks.py` 当前都把机场 provider 间隔固定为 `604800`，必须连同生成、守卫、端到端和文档测试统一改为 `86400`；
- `installer.py` 当前 systemd 事务只管理 metadata 与 recover 单元。新增 refresh service/timer 后，安装前状态捕获、文件写入、启用、失败回滚、post-update 和卸载必须同步扩展；
- 机场状态当前只反映一个来源 URL。Schema v2 后只在机场子菜单显示开关页面与真实链接的配置状态及两个主机名，通用状态页面不新增凭据相关内容；
- `AirportStore` 在写入前除现有目录类型、属主和组检查外，还必须要求 `public/provider` 模式精确为 `02750`，防止目录被放宽后低权限进程替换 provider 文件。

## 页面适配器

### 输入与输出

建议接口边界：

```python
class AirportPortalClient:
    def activate(self, activation_url: str) -> AirportPortalPage:
        ...

    def generate_source_url(self, page: AirportPortalPage) -> str:
        ...
```

`activate()` 返回只存在于内存的页面上下文。上下文包含后续生成任务所需的动态参数，但不得持久化或进入异常文本。

### 页面读取

- 只接受 HTTPS；
- 禁止 URL 用户名、密码和 fragment；
- 最多三次 HTTPS 重定向；
- 最终页面必须与输入页面同源；
- 正常验证 TLS 证书；
- 15 秒超时；
- 页面最大 1 MB；
- 单次操作允许使用内存 Cookie 会话，操作结束后丢弃；
- 不保存 Cookie。

### 页面解析

使用 Python 标准 HTML 解析器，不执行脚本。只识别精确的 `id=Clash1_Anyttls` 按钮，并验证其调用参数指向同源 `/Subscription/Clash` 且订阅类型为 `anytls_clash`。

从页面上下文提取：

- `sid`；
- `token`；
- `pid`；
- `delaytime`；
- AnyTLS Clash 按钮提供的完整相对订阅参数。

字段缺失、重复、类型错误、页面结构不符或出现歧义时，返回 `airport_portal_unsupported`，不得猜测其他按钮或执行页面代码。

### 链接生成

固定向同源 `/Subscription/GetSubscription` 发送表单编码 POST：

1. 第一次请求提交 `sid`、`token`、`pid` 和按钮提供的订阅信息；
2. 响应为 `url:` 时直接取得链接；
3. 响应为 `subid:` 时，等待页面指定时间；
4. 等待时间只接受 0–30 秒整数；
5. 第二次请求额外提交任务编号；
6. 接口 JSON 最大 4 KB；
7. `result`、`msg`、任务编号及链接格式必须严格校验；
8. 新链接必须为 HTTPS、无 URL 用户名密码、无 fragment，并与开关页面同源。

接口失败、超时或返回非法任务结果时，返回稳定错误码，不得包含原始响应。

## 机场下载与基础响应检查

所有实际下载正文的操作共用同一管道：

- 只接受 HTTPS；
- 最多三次 HTTPS 重定向；
- 15 秒超时；
- 最大 5 MB；
- 正文必须非空；
- 响应类型为 `text/html` 或 `application/xhtml+xml` 时拒绝；
- 去除 UTF-8 BOM 与开头空白后，正文以 `<!DOCTYPE html`、`<html`、`<head` 或 `<body` 开始时拒绝，匹配不区分大小写；
- 不依赖 URL 扩展名或 YAML MIME；机场实测响应为 `text/plain`；
- 不解析 YAML，不检查顶层类型，也不要求 `proxies`、`proxy-groups`、`rules` 或其他业务键；
- 捕获最终响应的单个 `Subscription-Userinfo`；
- 流量头缺失或非法不影响正文更新。

通过检查后，将下载到的原始字节直接写入 `AmyTelecom.yaml`，不得解码后重写、重新序列化或改变注释、顺序、缩进和换行。该检查只阻止空响应和明显的网页错误内容，不承诺判断任意正文是否为语义正确的 Clash 配置；这是保留现有“机场文件由上游保证可用”契约的有意选择。

对状态机而言，传输失败、空响应、超限响应和 `airport_response_invalid` 都表示该 `source_url` 本次下载失败。菜单 2 使用旧链接遇到这些结果时，如果页面上下文可用，继续生成新链接并重试；其他菜单按各自定义保留完整旧数据。

## 定时刷新

新增：

```text
clash-sub-airport-refresh.service
clash-sub-airport-refresh.timer
```

定时器要求：

- 每 7 天执行一次；
- 随机延迟 0–6 小时；
- `Persistent=true`；
- 默认随安装启用；
- 调用菜单 2 相同的服务方法；
- 使用无 URL 参数的非交互内部命令 `clash-sub airport-scheduled-refresh`；
- 与人工机场操作共用现有操作锁；
- 操作锁已被占用时按本次跳过处理；
- 不进行密集自动重试；
- 未配置开关页面时跳过；
- 页面临时不可用且旧链接失败时跳过；
- 不执行 `sync`，不读取 3x-ui，不生成主配置，不调用 Mihomo，不操作 release，不重载 Nginx。

`clash-sub-airport-refresh.service` 为一次性服务，需要网络访问并依赖 `network-online.target`，单次整体超时约 180 秒；只启用 timer，不单独启用 service。安装、升级失败回滚和卸载必须把这两个 unit 纳入现有 systemd 文件与启用状态事务，不能只把 unit 文件复制到服务器。

项目按全新部署处理，不增加旧流量定时单元的迁移或清理逻辑。

## 三层刷新周期

刷新周期固定为：

| 层级 | 周期 | 访问对象 |
|---|---:|---|
| `Clash-Compat` / `Clash-Balance` 主配置 | 24 小时 | 自有订阅服务器 |
| `AmyTelecom` proxy-provider | 24 小时（86400 秒） | 自有订阅服务器 |
| 服务器更新 `AmyTelecom.yaml` | 7 天，随机延迟 0–6 小时 | 机场页面和机场订阅 |

主配置继续返回：

```text
Profile-Update-Interval: 24
```

生成的 owner 配置将 `AmyTelecom` provider 设置为：

```yaml
proxy-providers:
  AmyTelecom:
    interval: 86400
```

主配置与 provider 的日常刷新只访问自有订阅服务器，不增加机场上游访问频率。provider 独立刷新可以在服务器每周更新机场文件后最多约 24 小时内取得新内容，避免服务器与客户端都使用 7 天周期时出现接近 14 天的错位延迟。人工操作和 Clash Verge 手动刷新不受上述周期限制。

## 稳定结果与错误码

新增或明确使用：

```text
airport_activation_url_invalid
airport_activation_missing
airport_portal_unavailable
airport_portal_unsupported
airport_link_generation_failed
airport_response_invalid
airport_refresh_skipped
```

`airport_refresh_skipped` 是正常结果，不作为失败。其余错误根据具体交互返回，但不得包含 URL、Token、任务编号、HTML、YAML正文或接口原始响应。

现有 HTTPS、重定向、超时、大小、文件安全和事务错误码保持稳定，避免把所有错误压缩为一个无法定位的通用错误。

## 备份与恢复

`airport-source.json` 继续属于重建备份文件，Schema v2 已同时包含两个 URL，因此备份数量不增加。

以下文件仍不进入备份：

- `AmyTelecom.yaml`；
- 事务临时文件；
- 可重新生成的运行时文件。

服务器恢复后：

1. 恢复私密来源记录；
2. 使用菜单 2 重新取得机场正文；
3. 如果页面自动化失效，手动开启订阅后使用菜单 3 刷新；
4. 如果真实链接也已失效，使用菜单 4 输入新链接；
5. 机场文件恢复后再执行 `sync`。

## 验收标准

### 页面适配器

- GET 页面可以单独触发开启订阅；
- 第一次 POST 直接返回链接时成功；
- 第一次 POST 返回任务编号、等待后第二次成功；
- 等待时间越界、JSON异常、任务编号异常和链接非法时安全失败；
- 页面临时不可用与页面结构不兼容可以区分；
- 不执行远端 JavaScript；
- 页面参数和接口响应不进入异常与输出。

### 服务状态机

- 旧链接成功时绝不调用链接生成接口；
- 旧链接失败后才生成新链接；
- 页面临时不可用时仍尝试一次旧链接；
- 页面和旧链接都不可用时跳过并保留旧数据；
- 菜单 1 每次强制生成新链接；
- 菜单 3 不访问页面、不显示页面地址、不生成链接；
- 菜单 4 完全绕过页面适配器；
- 所有失败保持旧来源记录与旧机场文件；
- 任意成功下载都更新流量和成功时间；
- 缺失或非法流量头保存为 `null`。

### 下载与存储

- HTML、空文件和超限文件被拒绝；
- 非 HTML、非空且未超限的响应不做 YAML 结构校验；
- 下载正文的原始字节、注释、顺序和换行完整保留；
- Schema v2 文件安全检查完整；
- 来源记录采用完整临时文件原子替换，未变化字段被保留；
- 稳定状态和事务恢复只能得到完整旧组合或完整新组合；
- 允许两次正式文件替换之间的一次并发请求短暂取得新正文与旧流量头。

### CLI、部署与安全

- 菜单名称、编号、提示和取消行为精确匹配设计；
- URL 只能通过可见交互输入，不能作为命令参数；
- 定时任务复用菜单 2 的服务方法；
- 定时 service 有网络依赖和整体超时，只启用 timer；
- 安装事务、升级失败回滚、post-update 和卸载完整覆盖新增 systemd 单元；
- 主配置继续声明 `Profile-Update-Interval: 24`；
- owner 配置中的 `AmyTelecom.interval` 精确为 `86400`；
- 服务器机场刷新周期精确为 7 天并带 0–6 小时随机延迟；
- 安装、备份、恢复和卸载测试覆盖新增单元；
- `public/provider` 目录必须精确为 `02750 root:www-data`，存储前检查模式、属主和组；
- 全新部署不实现旧流量定时单元的迁移与兼容；
- 仓库、测试 fixture、命令输出和错误文本不出现真实机场凭据；
- 完整测试、秘密扫描、`compileall`、systemd 单元守卫和 `git diff --check` 全部通过。
