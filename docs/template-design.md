# 模板设计与私密数据

本页是本仓库模板、机场引用、注释规则与私密数据边界的唯一说明。它只描述当前维护路径；部署后的服务器操作见[部署清单](../DEPLOYMENT.md)，日常同步命令见[运维手册](operations.md)。

## 文件结构

| 路径 | 职责 | 是否受跟踪 |
| --- | --- | --- |
| `templates/base/Clash-Compat.yaml` | Compat 完整基础模板；不携带动态节点与机场痕迹。 | 是 |
| `templates/dns/Clash-Balance.yaml` | 只含 Balance 的完整 `dns:` 段及其注释。 | 是 |
| `templates/profiles.yaml` | 两个 profile 的 DNS 配方，以及节点/机场注入策略组列表。 | 是 |
| `private/clash-verge-home.js` | 本机 Clash Verge 全局扩展脚本，唯一的 Home 载体。 | 否，`.gitignore` 忽略 |

## 生成流程

1. 生成 Compat：以 Compat 完整基础模板注入用户 3x-ui 节点；owner 再注入 `AmyTelecom` 机场 provider，输出 `Clash-Compat.yaml`。
2. 生成 Balance：先取 Compat 完整基础模板，以 `templates/dns/Clash-Balance.yaml` 的整个 `dns:` 段（含注释）整体替换 DNS，再按 owner 路径注入 3x-ui 节点与机场 provider，输出 `Clash-Balance.yaml`。
3. 普通用户只生成 Compat，渲染层直接拒绝传入机场 provider。

## 导出矩阵

| 身份 | 生成 profile | 机场 provider |
| --- | --- | --- |
| owner | `compat`、`balance` | 有，`AmyTelecom` 以固定形态注入 |
| 普通用户 | 仅 `compat` | 无机场 provider、无机场节点 |

## 机场引用

owner 主配置中的机场 provider 固定为：

```yaml
proxy-providers:
  AmyTelecom:
    type: http
    url: "https://订阅域名/s/<owner-token>/AmyTelecom.yaml"
    path: ./proxy_providers/AmyTelecom.yaml
    interval: 604800
```

provider 显示名固定 `AmyTelecom`，两份 owner 主配置引用同一 URL，Clash Verge 可单独手动刷新，自动刷新间隔 7 天。服务器侧的真实文件是 `/var/lib/clash-sub/public/provider/AmyTelecom.yaml`，只通过 owner 令牌路由访问；普通用户令牌没有任何机场路由。

订阅响应头（`Subscription-Userinfo` 流量头、`Profile-Update-Interval: 24`）由部署侧在请求时按需附加：模板、provider 文件与发布物从不携带流量数据，流量数字只保存在服务器私密目录的来源记录与缓存里。

模板同步会在净化时移除机场在本机的痕迹：机场 provider 本体、以机场缓存文件为 `path`/`url` 的本地别名 provider、这些 provider 在策略组 `use` 与 YAML merge/锚点中的引用，以及提及该缓存文件名的注释。受影响的策略组记入 `profiles.yaml` 的机场注入列表，owner 发布时重新挂上 `AmyTelecom`。

## 注释与 YAML 结构

注释属于模板的正式内容：

- Compat 的通用注释进入所有最终主配置。
- Balance 的 DNS 内容与 DNS 注释作为整体覆盖 Compat 的 DNS 段；Balance 独有的 DNS 注释随之保留。
- Balance 的非 DNS 独有注释只进入差异报告，不自动合并。
- 动态注入用户节点、机场 provider 和规则时，不丢失原有注释，也不整份重排 YAML。

模板读写使用 round-trip YAML，保留 comment、anchor、alias 和 merge（`<<`）关系。若手工编辑导致引用无效、重复键或 YAML 不可解析，候选验证会拒绝写入。

## iCloud 模板同步

模板更新直接读取 iCloud 中两个已命名文件：

```text
~/Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/
├── Clash-Compat.yaml
└── Clash-Balance.yaml
```

```bash
./bin/clash-sub template-sync
```

只更新一个来源时，每次只传一个选项；未指定的来源不会被读取或改写：

```bash
./bin/clash-sub template-sync --compat /path/Clash-Compat.yaml
./bin/clash-sub template-sync --balance /path/Clash-Balance.yaml
```

同步规则：

1. 以 `Clash-Compat.yaml` 净化后整体更新 Compat 基础模板。
2. 从 `Clash-Balance.yaml` 提取完整 `dns:` 段及归属注释，更新 Balance DNS 模板。
3. 比较两者去除 DNS 后的其余差异，只把差异的顶层路径写入报告，不合并、不输出任何值。
4. 报告 Compat 内容变化、Balance DNS 变化、被忽略的非 DNS 差异与注释保留结果。
5. 候选先经过渲染校验（用真实渲染器分别生成一次 owner 配置和一次普通用户配置并校验）和秘密扫描；任一失败都不替换仓库中的当前模板。

同步报告逐项给出：

- 「Compat 基础 / Balance DNS：已更新或无变化」与最终写入的文件。
- 结构变化摘要：相对当前跟踪模板新增、删除、修改的 YAML 路径（键名与数量，超过 6 项折叠为计数；列表只记增减条数，不列内容）。
- Compat 通用注释保留情况（全部保留 N 行，或保留 M/N 行）。
- Balance 独有 DNS 注释保留行数。
- 被忽略的 Balance 非 DNS 差异顶层路径。

报告只允许出现公开路径、键名、数量与是否更新；不得输出机场 URL、节点、Home 内容或任何标量值。

## Home 扩展脚本

服务器不再保存或生成 Home 配置。Home 敏感内容只维护在本机 `private/clash-verge-home.js`（Clash Verge Rev 的全局扩展脚本）。

脚本只对两个精确 profile 标题生效：

- `Clash-Compat`
- `Clash-Balance`

其他 profile 原样返回。脚本把 Home 节点、`HomeServer` 和 `ProxyServer` 分组插入主配置，两个分组放在“自动选择”之后；对应规则保持在规则列表预定的高优先级位置。

维护方式：直接编辑该脚本，保存后用 `node --check private/clash-verge-home.js` 验证语法，再在 Clash Verge 中重新载入配置观察效果。不要把脚本内容、节点名或地址粘贴到文档、提交或终端记录。

## 私密数据边界

`private/**`、`.env` 和生成发布物均不受跟踪。3x-ui 的实际连接数据、机场订阅、订阅令牌、域名、节点地址和认证材料只可留在对应的私密文件、受保护的服务器运行时目录或机场更新的交互式输入中；机场地址输入按当前需求可见，但不会写入项目状态或日志。服务器上的机场来源记录（`airport-source.json`，含订阅链接与流量数字）与流量缓存（`traffic-cache.json`）同属私密运行时数据，固定 0600，永不进入仓库。

不要在模板注释、示例、提交信息、终端重定向输出或故障报告中记录这些值。提交前运行：

```bash
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
```

第一条扫描受跟踪仓库内容；第二条在本机存在私密目录时额外比对私密值是否泄露到受跟踪文件。两者都应报告 `scan clean`。
