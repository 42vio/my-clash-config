# 模板设计与私密数据

本页是本仓库模板、Home 覆盖层与私密数据边界的唯一说明。它只描述当前维护路径；部署后的服务器操作见[部署清单](../DEPLOYMENT.md)，日常同步命令见[运维手册](operations.md)。

## 文件结构

| 路径 | 职责 | 是否受跟踪 |
| --- | --- | --- |
| `templates/base/compat-office.yaml` | Compat 公共基础；不携带动态节点或机场 provider。 | 是 |
| `templates/dns/balance-office.yaml` | 只含完整的 `dns:` 段。 | 是 |
| `templates/profiles.yaml` | 三个 profile 的 DNS/Home 配方，以及注入策略组列表。 | 是 |
| `private/home.yaml` | Home 私密覆盖层；本机模板同步读取它来分离或验证 Home 范围。 | 否，`.gitignore` 忽略 |

`private/home.yaml` 是严格的六字段 YAML 对象：`proxies`、`proxy-groups`、`extend-proxy-groups`、`inject-node-groups`、`inject-home-node-groups` 与 `rules`。维护时只按该结构增删；不要把真实节点、地址、域名、凭据或规则值抄到文档、提交或报告中。

## 生成流程

1. `Compat-Office.yaml` 被同步后清除动态节点、机场 provider 和 Home 范围，写成 `templates/base/compat-office.yaml`；策略组注入信息写入 `templates/profiles.yaml`。
2. 生成 `compat-office` 时，以 Compat 公共基础叠加用户 3x-ui；owner 再叠加机场与 Home。
3. `compat-universal` 也从 Compat 基础生成，但不叠加 Home。因此 Universal 是“Compat 基础去 Home”的派生物，不维护第三份公共模板。
4. `balance-office` 先取 Compat 公共基础，再以 `templates/dns/balance-office.yaml` 的整个 `dns:` 段替换 DNS，随后按 owner 路径叠加 3x-ui、机场与 Home。

`Compat-Universal.yaml` 不参与日常模板更新。它只在首次初始化时与 `Compat-Office.yaml` 比较，用来推导 `private/home.yaml` 所代表的 Home 范围；之后的日常更新仍以既有 Home 覆盖层为准。

## 授权矩阵

| 角色 | 可生成 profile | `AmyTelecom.yaml` | Home |
| --- | --- | --- | --- |
| owner | `compat-office`、`compat-universal`、`balance-office` | 有，且只给 owner 发布 | `compat-office`、`balance-office` 包含；`compat-universal` 排除 |
| member | 仅 `compat-universal` | 无机场 provider、无机场节点 | 无 |

owner 的 `AmyTelecom.yaml` 是单独的机场发布物；它不是公共模板的一部分，也绝不可进入 member 的 profile。Privacy 当前不提供：当前版本没有 Privacy profile、发布文件或维护命令。

## Compat 基础与 Universal

Compat 基础是所有当前 profile 的共同 YAML 骨架。`profiles.yaml` 固定记录：`compat-office` 使用 Compat DNS 和 Home，`compat-universal` 使用 Compat DNS 但不使用 Home，`balance-office` 使用 Balance DNS 和 Home。

首次初始化的差分只用于识别 Office 相对 Universal 多出的 Home 节点、策略组、公共策略组扩展和规则；结果写入私密覆盖层。以后不要通过手工复制 `Compat-Universal.yaml` 来更新 Universal，也不要把 Home 定义回填到公共基础。

## Balance DNS

`templates/dns/balance-office.yaml` 必须只含一个完整 `dns:` 映射。生成 `balance-office` 时，程序整体替换 Compat 的 `dns:`，不进行递归合并；Balance 独有的 DNS 注释也随该完整段一同保留。不要把 Balance 设置拆成若干局部补丁，更不要在本页写通用 DNS 配置建议。

## Home 覆盖层

Home 是 owner-only 私密数据。仓库本地的 `private/home.yaml` 必须是当前用户拥有的普通文件、不是 symlink、链接数安全且权限精确为 `0600`；格式或权限不符合时，模板同步应失败而非猜测修复。

服务器上的 Home 仅通过人工 SFTP 覆盖到私密目录。服务端同样拒绝非普通文件或 symlink，并要求私密父目录受预期 owner 控制；通过校验后将文件权限收紧为 `0600`。不存在 Home 上传命令、批量导入接口或由 `template-sync` 向服务器传输 Home 的路径。

Home 的对象注释也属于覆盖层内容。生成 Office profile 时会把对应的 Home 节点、策略组和规则及其注释带入；生成 Universal 或 member profile 时则不带入。

## 注释与 YAML 结构

模板读写使用 round-trip YAML。维护输入时可保留 YAML comment、anchor、alias 和 merge（`<<`）关系；同步、拆分、组合和输出会以带注释的 YAML 对象处理，不应为了“格式化”而把它们展开、改写成普通字典，或删除有语义的注释。

尤其要分别保留 Compat 公共注释、Balance DNS 注释和 Home 对象注释。若手工编辑导致引用关系无效、重复键或 YAML 不可解析，候选验证会拒绝写入。

## 模板更新

日常同步默认只读取 iCloud 中这两个文件：

```text
~/Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/
├── Compat-Office.yaml
└── Balance-Office.yaml
```

```bash
./bin/clash-sub template-sync
```

只更新一个来源时，明确传一个选项；另一来源不会被读取或改写：

```bash
./bin/clash-sub template-sync --compat-office /path/Compat-Office.yaml
./bin/clash-sub template-sync --balance-office /path/Balance-Office.yaml
```

同步会先构造并验证所有候选（公共基础、profile 配方、选中的 Balance DNS 与 Home 范围），包括渲染后的授权边界与秘密检查；全部通过后才以原子替换写入选中的目标。读取失败、解析失败、校验失败或写入失败时不应留下半更新结果；写入阶段若出错，程序会尝试恢复先前文件。

同步报告只列公开路径、是否更新和公开规则/集合计数。YAML 注释在模板中由 round-trip 机制保留，但报告不得输出 Home 值、动态节点名、地址、订阅 URL 或凭据；检查结果后仍需查看受跟踪 diff 并运行两种密钥扫描。

## 私密数据边界

`private/**`、`.env` 和生成发布物均不受跟踪。Home、3x-ui 的实际连接数据、机场订阅、订阅令牌、域名、节点地址和认证材料只可留在对应的私密文件、受保护的服务器运行时目录或交互式隐藏输入中。

不要在模板注释、示例、提交信息、终端重定向输出或故障报告中记录这些值。提交前运行：

```bash
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
```

第一条扫描受跟踪仓库内容；第二条在本机存在私密目录时额外比对 Home 值是否泄露到受跟踪文件。两者都应报告 `scan clean`。
