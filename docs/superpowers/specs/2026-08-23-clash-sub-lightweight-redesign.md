# Clash 订阅服务轻量化重设计

**日期：** 2026-08-23
**状态：** 设计已确认
**目标仓库名：** `my-clash-config`
**管理命令：** `clash-sub`

本设计取代 2026-08-19 与 2026-08-21 的订阅转换、Docker Compose 和发布服务设计。旧规格与计划仅作为历史记录保留，不得继续执行。

## 1. 目标和约束

在一台约 512 MiB RAM、256 MiB Swap、10 GiB 磁盘的 VPS 上，为 owner 和少量受信任用户发布完整 Clash/Mihomo 配置：

1. 每位普通用户只获得自己独立的 3x-ui 客户端节点。
2. owner 的配置合并自己的 3x-ui 节点、机场节点快照和家庭节点。
3. 一份基础模板派生 `balanced`、`standard`、`privacy` 三种配置。
4. 对外只发布最后一次成功验证的静态 YAML。
5. 平时不运行 Python、Mihomo、转换器或发布进程。
6. 不要求管理员记住 `refresh`；日常管理统一从无参数 `clash-sub` 菜单进入。

设计优先级依次为：凭据隔离、失败时保留可用配置、操作简单、低常驻资源占用。

## 2. 明确不做的事情

- 不部署 Docker、Subweb、subconverter 或其他在线转换页面。
- 不允许访问者提交任意 URL 让 VPS 代为抓取或转换。
- 不自动登录机场后台，不保存机场 Cookie。
- 不保存机场五分钟临时 URL。
- 不提供流量状态网页，不使用“流量节点”。
- 不启用 Telegram 提醒。
- 不在订阅请求到达时生成配置或实时查询 3x-ui。
- 不每五分钟轮询 3x-ui，也不按固定时间重复生成配置。
- 不把任何真实节点、订阅地址、UUID、密码、REALITY 密钥或公开订阅 Token 提交到 Git；仓库私有不改变此规则。
- 不实现设备绑定；公开订阅 URL 仍是 bearer credential。
- 不提供自建或第三方短链；短链接本身会成为第二套 bearer credential 和轮换状态。
- 不自动安装或修改 3x-ui；部署文档只提供人工步骤和检查命令。
- 不在旧 Jrohy/Trojan 服务器上原地清理或迁移；旧拓扑只做文档记录。

## 3. 最终运行架构

常驻进程只有：

- 3x-ui 管理的 Xray。
- 宿主机 Nginx。

按需运行：

- `clash-sub`：同步用户、导入机场、生成配置、更新流量头、查看状态和回滚。
- 固定版本 Mihomo：只在候选配置发布前执行校验。
- 每日流量头任务：短时读取 3x-ui 流量并更新 Nginx 元数据，不生成 YAML。
- acme.sh 续期任务：只维护 HTTPS 证书。

数据流：

```text
3x-ui SQLite（只读发现客户端）
        +
3x-ui 回环 /clash/<subId>（节点和流量来源）
        +
机场 Clash 快照 + owner 家庭节点
        +
基础模板及 variant
        ↓
clash-sub 手动同步
        ↓
候选 YAML → 结构检查 → Mihomo 检查
        ↓
最近五个成功版本 → 当前静态版本
        ↓
Nginx HTTPS 8443
```

## 4. 端口、域名和证书

固定端口职责：

| 端口 | 服务 | 公网状态 |
| --- | --- | --- |
| TCP 443 | VLESS + RAW/TCP + REALITY | 开放 |
| TCP 8443 | Nginx：3x-ui 面板和 Clash 订阅 | 开放 |
| TCP 80 | Nginx：ACME HTTP-01 和通用响应 | 开放 |
| SSH 自定义端口 | sshd | 开放 |
| 3x-ui 面板端口 | 回环 HTTP | 不开放 |
| 3x-ui 订阅端口 | 回环 HTTP | 不开放 |

REALITY 使用 VPS 公网 IP 的 TCP 443，不依赖域名证书。`panel.<domain>` 和 `sub.<domain>` 共用一张 SAN 证书，Nginx 在 8443 终止 TLS。

复用 3x-ui 安装环境中的 acme.sh，但证书使用 acme.sh 的 `--install-cert` 安装到稳定的 Nginx 路径；Nginx 不直接引用 `~/.acme.sh` 内部文件。续期成功后执行经过固定参数配置的 Nginx reload。

3x-ui 面板只监听回环 HTTP，由 Nginx 反向代理。3x-ui 订阅服务也只监听回环 HTTP，不配置自己的证书，不设置公网入口。

## 5. 3x-ui 客户端发现

`clash-sub sync` 不保存管理员 API Token。它以 SQLite 只读模式打开本机 3x-ui 数据库，只查询生成所需的最小字段：

- 客户端数据库主键。
- `email`（仅作为内部管理员识别标记）。
- `subId`。
- 启用状态。
- 配额和到期字段。
- 3x-ui 订阅端口、Clash 路径和相关启用状态。

约束：

1. 数据库连接必须使用只读模式，代码中不存在写 SQL。
2. 3x-ui 固定到明确验证的版本。
3. 同步前校验数据库表、字段和唯一性；结构不匹配时全局失败关闭。
4. 数据库结构不匹配、被锁定或无法读取时，不修改当前发布、不删除用户、不重载 Nginx。
5. 每日流量任务失败时保留上一份响应头；YAML 仍可下载。
6. 数据库只用于发现和关联客户端，节点格式仍以 3x-ui Clash 输出为准。

客户端的稳定内部身份使用 3x-ui 客户端数据库主键。`email` 改名只更新内部管理员显示，不更换公开 Token、URL、文件名或配置标题；`subId` 轮换只更新内部来源，不改变最终订阅地址。删除后重新创建的客户端视为新身份并获得新 Token。

owner 在首次同步时由私有配置中的 `owner_email` 唯一匹配，随后持久化对应的 3x-ui 客户端数据库主键。以后修改 owner 的 `email` 只更新内部管理员显示，不改变 owner 身份；删除该数据库客户端后必须由管理员重新确认新的 owner，不能仅凭相同 email 自动继承 owner 权限。

## 6. 自动构造回环 Clash 地址

`clash-sub` 从已验证的 3x-ui 设置和客户端 `subId` 自动构造：

```text
http://127.0.0.1:<subPort>/<subClashPath>/<subId>
```

不允许用户为单个客户端手工填写源 URL，也不允许使用数据库中的公网 Host 替换回环地址。

发布前置检查要求：

- 订阅服务监听 `127.0.0.1`。
- Clash 输出已启用。
- `subId` 非空且唯一。
- 回环接口返回 YAML mapping。
- `proxies` 是非空列表。
- VLESS REALITY 节点包含 UUID、服务器、端口、SNI、指纹、公钥和 short ID。

任一用户的回环接口临时失败时，保留该用户上一份成功配置；其他用户继续同步。全局数据库结构失败时不处理任何用户。

## 7. 模板和 variant

现有三份原始配置继续保存在被 Git 忽略的只读参考目录，生成器不得覆盖。可维护模板结构为：

```text
templates/
  clash.yaml.j2
  variants/
    balanced.yaml
    standard.yaml
    privacy.yaml
```

三种 variant 的节点范围：

| variant | owner | 普通用户 | 用途 |
| --- | --- | --- | --- |
| `balanced` | owner 3x-ui + 机场 + 家庭节点 | 不发布 | 通用完整配置 |
| `standard` | owner 3x-ui + 机场，不含家庭节点 | 仅本人 3x-ui | 标准跨平台配置、适合 Windows、默认发给其他用户 |
| `privacy` | owner 3x-ui + 机场 + 家庭节点 | 不发布 | 隐私优先配置 |

`balanced-win` 全面改名为 `standard`，不保留兼容别名。不得使用 `public` 或 `shared` 命名，以免暗示带 UUID 的配置可以公开转发。

公共结构只保存在基础模板；variant 文件只描述 DNS、规则或策略组等真实差异。用户来源隔离由数据模型决定，不靠模板注释或节点命名约定。

## 8. 机场和家庭节点

机场必须提供 Clash YAML。`clash-sub` 菜单中的“更新机场订阅”通过隐藏输入接收五分钟 URL：

1. URL 不进入 argv、shell history、环境变量、日志或持久化配置。
2. 仅允许 HTTPS，并使用固定超时、最大响应体和重定向上限。
3. 响应必须是合法 YAML，且包含可验证的 `proxies`。
4. 成功后只保存规范化的机场节点快照，不保存 URL。
5. 新快照写入后立即生成并验证 owner 的三个 variant。
6. 下载、解析或任一 owner variant 校验失败时，旧机场快照和旧 owner 发布保持不变。

家庭节点由 owner 在 VPS 私有文件中维护，只在 `balanced` 和 `privacy` 中注入。`standard` 明确排除家庭节点。

不同来源出现同名节点时，只对冲突项追加稳定来源后缀；所有策略组引用同步使用最终名称。

## 9. 用户和订阅地址

每位 3x-ui 客户端对应一位最终订阅用户。首次同步自动：

- 发现新增客户端。
- 为其生成由至少 32 字节密码学安全随机核心和六位易读识别码组成的 Token。
- 保存稳定用户映射。
- 生成该用户的 `standard` 配置。
- 在 `clash-sub` 的链接列表中显示最终地址。

公开路径不包含客户端名称：

```text
普通用户：https://sub.<domain>:8443/s/<token>/clash-standard.yaml

owner：
https://sub.<domain>:8443/s/<token>/clash-balanced.yaml
https://sub.<domain>:8443/s/<token>/clash-standard.yaml
https://sub.<domain>:8443/s/<token>/clash-privacy.yaml
```

Token 是唯一授权凭据。为保持静态 Nginx 架构并允许管理员以后查看链接，Token 以明文保存在 VPS 的 root-only 私有状态中；它不进入 Git、普通日志或 Nginx access log。静态发布目录按 Token 隔离，禁止目录浏览和任意路径解析。

Token 固定为 `<base64url-random-core>-<readable-code>`：随机核心至少 32 字节且使用无填充 Base64URL 编码；识别码为六位密码学随机大写字母或数字，排除 `I`、`L`、`O`、`0`、`1` 等易混字符，并在现存用户中校验唯一。管理界面使用识别码辅助人工核对；服务器必须匹配完整 Token，识别码不能单独下载订阅。Token 轮换时随机核心和识别码同时更换。

删除或禁用 3x-ui 客户端后，下次手动同步撤销其公开路径，但保留 root-only 历史版本用于审计和人工恢复。重新启用同一数据库客户端可恢复原 Token；删除后重新创建则使用新 Token。

## 10. 客户端显示名称

用户名称不进入 URL、下载文件名或公开响应头。Nginx 按 variant 对每个有效路径设置固定名称：

```text
Profile-Title: Clash Standard
Content-Disposition: attachment; filename="Clash-Standard.yaml"
```

另外两个 variant 对应 `Clash Balanced` / `Clash-Balanced.yaml` 和 `Clash Privacy` / `Clash-Privacy.yaml`。3x-ui 的 `email` 仅用于服务器内部身份匹配和管理员状态显示，不进入公开 URL 或响应。若客户端不支持这些响应头，它仍可从 URL 文件名识别配置类型。

最终响应不转发 3x-ui 的 `Profile-Web-Page-Url`、`Support-Url`、`Announce`、Sub ID 或其他内部身份头，也不设置强制客户端更新间隔。

## 11. 手动同步和管理界面

用户日常只需记住：

```bash
clash-sub
```

无参数命令显示交互菜单：

```text
1. 更新机场订阅
2. 同步所有配置
3. 查看订阅链接
4. 查看状态和历史版本
```

行为：

- “更新机场订阅”成功后自动生成 owner 三个 variant，并更新 owner 流量头。
- “同步所有配置”重新读取 3x-ui、模板、机场快照和家庭节点，只为内容发生变化的用户创建新版本，同时更新所有流量头。
- 新客户端在同步时自动创建，不需要逐个录入订阅地址。
- “查看订阅链接”一次按 3x-ui 内部 `email` 分组显示全部有效用户、允许的 variant、六位识别码和完整 URL；不要求逐个选择或确认，输出不写入项目日志。
- 配置内容哈希未变化时不创建版本、不运行 Mihomo；只有流量头或标题元数据同时发生变化时，才可能单独验证并 reload Nginx。
- 状态页面显示最后成功时间、当前版本、待同步来源和最近错误，但不显示 Token、UUID、Sub ID 或源 URL。

为 systemd 和排错保留非交互子命令，但不要求用户日常记忆：

- `clash-sub sync`
- `clash-sub traffic-update`
- `clash-sub status`
- `clash-sub links`
- `clash-sub history <user>`
- `clash-sub rollback <user> <release>`
- `clash-sub rotate-link <user>`

不提供 `refresh` 或兼容别名。

## 12. 生成、验证和发布

一次用户同步：

1. 读取允许的节点来源。
2. 从 3x-ui Clash YAML 中只提取并规范化 `proxies`。
3. 合并机场和家庭节点，解决名称冲突。
4. 渲染允许的 variant 到 staging。
5. 使用安全 YAML 解析检查结构、节点唯一性和全部策略组引用。
6. 扫描结果，确保不存在源订阅 URL、机场临时 URL、公开 Token 或内部回环地址。
7. 使用固定版本 Mihomo 执行真实配置检查。
8. 写入 manifest、来源哈希和响应头元数据。
9. 原子切换当前版本和静态发布路径。
10. 仅保留最近五个成功版本。

owner 的三个 variant 是一个原子发布集合，必须全部成功或全部保留旧版本。普通用户彼此隔离，一个用户失败不阻止其他用户更新。

参考原件永久保留，不参与五版本清理。失败候选和下载临时文件必须及时清理，不得进入历史版本。

## 13. 流量头

公开响应支持：

```text
Subscription-Userinfo: upload=...; download=...; total=...; expire=...
```

流量只代表对应用户的 3x-ui 配额。owner 的机场和家庭节点不虚构统一流量。

更新规则：

- 每日 systemd timer 运行一次 `clash-sub traffic-update`。
- 手动“同步所有配置”同时更新全部用户流量头。
- “更新机场订阅”同时更新 owner 流量头。
- Clash 客户端刷新只读取最近一次保存的流量头，不实时查询 3x-ui。
- 流量任务不生成 YAML、不创建 release。

流量头先写候选 Nginx include，严格校验整数和格式，再执行 `nginx -t`。只有检查通过才原子替换并 reload。失败时继续使用上一份流量头和静态 YAML。

已知限制：用户若在两次流量头更新之间突然耗尽配额，客户端显示可能暂时滞后；3x-ui 仍按自身配额规则禁用客户端。本项目不增加 Telegram、状态页或实时查询来消除此延迟。

## 14. Nginx 静态发布

Nginx 直接读取最后一次成功发布的 YAML，不反向代理 Python publisher。订阅 location：

- 只接受 `/s/<token>/clash-<allowed-variant>.yaml` 固定形状。
- 禁止目录列表、URL 解码绕过、点路径、反斜杠和额外段。
- 未知 Token、未知 variant、已撤销用户和缺失文件返回相同通用 404。
- 禁止在订阅 location 记录完整 URI。
- 限制请求速率和请求体大小。
- 设置 `Content-Type: text/yaml; charset=utf-8`、`X-Content-Type-Options: nosniff` 和 `Cache-Control: no-store`。
- 通过原子生成的 Nginx map/include 设置每个路径的 `Profile-Title`、`Content-Disposition` 和 `Subscription-Userinfo`。

3x-ui 原始 `/sub/`、`/json/`、`/clash/` 和 HTML 信息页不经公网 Nginx 暴露。

## 15. 安全边界

1. 每位用户使用独立 3x-ui UUID、Sub ID、公开 Token、配额和到期时间。
2. 普通用户配置不得包含 owner 的机场、家庭节点、名称或来源痕迹。
3. 公开 Token 泄漏后可下载完整节点凭据；轮换最终链接不能撤销已导入的 UUID，真正撤销仍需禁用或更换 3x-ui 客户端凭据。
4. 所有私有状态目录为 `0700`，私有文件为 `0600`；Nginx 只获得读取当前静态发布所需的最小权限。
5. 日志不记录订阅 URL、Token、Sub ID、UUID、节点密码、机场 URL、域名或敏感文件路径。
6. 外部 YAML 一律使用安全解析器，模板渲染使用严格未定义变量模式，不执行模板中的任意代码。
7. 固定 3x-ui、Mihomo 和 Python 依赖版本；升级先在本地 fixture 和测试 VPS 验证。
8. 3x-ui 数据库兼容性失败时 fail closed，不做迁移猜测。
9. Nginx 面板入口使用随机 Base Path、强密码、2FA、登录限速和通用错误响应。
10. REALITY 降低主动探测特征但不承诺不可识别或不会被封锁。

## 16. 备份和故障行为

- 每用户保留最近五个成功 release。
- `rollback` 只切换到已有、哈希验证通过的成功 release，不重新抓取来源。
- Token 轮换创建新公开目录并立即撤销旧路径；旧 Token 不进入输出或历史日志。
- 3x-ui 数据库读取失败：保留全部当前配置，`sync` 非零退出并显示数据库兼容错误。
- 3x-ui 单用户 Clash 接口失败：只保留该用户旧版本，继续处理其他用户。
- 机场更新失败：保留旧机场快照和 owner 三个旧 variant。
- Mihomo 校验失败：不发布候选。
- Nginx 配置检查失败：不替换 include、不 reload。
- 磁盘空间不足：在写 staging 前停止，不清理当前版本或参考原件。

## 17. 部署和运维文档

仓库提供按步骤执行的干净服务器安装文档，不提供自动修改整台服务器的一键脚本。文档至少包括：

1. 固定版本 3x-ui 的人工安装和初始化。
2. VLESS + REALITY TCP 443 配置和 Target 验证。
3. 3x-ui 面板及订阅服务回环监听检查。
4. Nginx 80/8443 配置和防火墙规则。
5. acme.sh SAN 证书申请、安装、续期和 Nginx reload。
6. Python 虚拟环境、固定依赖和 Mihomo 校验器安装。
7. `clash-sub` 私有配置、首次同步和链接分发。
8. 手机 SSH 更新机场的完整流程。
9. 3x-ui 升级前后的数据库兼容检查。
10. 流量头 timer、状态、日志、历史和回滚。
11. 故障恢复和换 VPS/IP/域名清单。

另保留独立历史说明，记录旧服务器的 Trojan 443、fallback 1443、`trojan-web` 80 和 Nginx 8080/1443 关系；该文档不作为新服务器安装步骤。

## 18. 验收标准

### 功能

- 一份基础模板生成 `balanced`、`standard`、`privacy`。
- owner 三份配置包含正确来源，`standard` 不含家庭节点。
- 普通用户只有 `standard`，且只包含自己的 3x-ui 节点。
- 新增 3x-ui 客户端后，一次手动同步自动生成最终链接。
- 管理界面一次显示全部有效链接，并以内部用户标识和六位易读识别码清楚区分归属。
- URL、下载文件名和响应头都不包含用户名称；客户端显示 `Clash Balanced`、`Clash Standard` 或 `Clash Privacy`。
- 机场更新后自动发布 owner 三份新配置。
- 每日任务和手动同步都能更新流量头而不创建无意义 release。

### 可靠性

- 内容不变时不创建版本。
- 任何失败都不覆盖最后成功配置。
- owner 三个 variant 原子发布。
- 每用户只保留五个成功版本并可回滚。
- 3x-ui 数据库 schema 变化时测试证明 fail closed。
- Nginx 和 Mihomo 检查均使用真实二进制完成。

### 安全

- Git 跟踪内容、测试输出和日志通过敏感信息扫描。
- 公开路径不能列目录、跨用户读取或访问未授权 variant。
- 普通用户配置中不存在 owner 来源信息。
- 3x-ui 原始订阅、数据库、面板源端口和私有状态不对公网开放。
- Token 路径不进入 access log。
- Token 保留至少 32 字节随机核心；六位识别码不能单独访问且不包含用户名；系统不提供短链。
- 机场临时 URL 不进入 argv、环境变量、文件或日志。

### 资源

- 空闲时除 3x-ui/Xray 和 Nginx 外无项目常驻进程。
- 手动同步和每日流量任务结束后释放全部 Python/Mihomo 内存。
- 不安装 Docker、Subweb 或 subconverter。

## 19. 实施与审查策略

1. 新实现以本设计为唯一产品规格，旧 Docker/Compose 计划不得复用。
2. 主体开发、测试和文档更新使用 Terra 完成。
3. 主体完成且本地测试通过后，使用 Sol 对安全边界、失败行为、迁移删除和文档一致性做独立最终审查。
4. Sol 审查发现的问题由 Terra 修复，再由 Sol 复核高风险项。
5. 在本地实现和测试通过前，不连接或修改真实 VPS；任何 VPS 写操作仍需用户单独批准。
