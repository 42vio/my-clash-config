# Clash 订阅项目全新模板设计

## 目标

彻底移除旧的 office、universal、privacy 和 Home 服务端模板体系，将项目收敛为两个 DNS 档位：`compat` 与 `balance`。Home 信息只保留在本机 Clash Verge Rev 全局扩展脚本中；机场订阅独立更新，不再与 owner 主配置发布绑定。

本次为不兼容升级。旧模板、旧文件名、旧订阅 URL 和旧路由直接删除，不提供重定向或兼容服务。

## 最终输出与身份

最终文件名与 Clash Verge 标题严格区分大小写：

| 身份 | 输出文件 | Clash Verge 标题 | 机场 provider |
|---|---|---|---|
| owner | `Clash-Compat.yaml` | `Clash-Compat` | 有 |
| owner | `Clash-Balance.yaml` | `Clash-Balance` | 有 |
| 普通用户 | `Clash-Compat.yaml` | `Clash-Compat` | 无 |

普通用户不生成 Balance。owner 与普通用户不通过文件名区分，而由订阅令牌和服务端授权区分。

## 仓库文件结构

```text
templates/
├── base/
│   └── Clash-Compat.yaml
├── dns/
│   └── Clash-Balance.yaml
├── nginx/
│   ├── stream.conf.j2
│   └── sub-server.conf.j2
└── profiles.yaml

private/
└── clash-verge-home.js
```

`templates/base/Clash-Compat.yaml` 是完整基础模板。`templates/dns/Clash-Balance.yaml` 只保存从 iCloud Balance 源文件提取的完整 `dns` 段及其注释。

`private/clash-verge-home.js` 被 Git 忽略，权限保持为 `0600`。本机 `private/home.yaml`、`private/sources/owner/` 和 `.DS_Store` 全部删除。

## iCloud 模板同步

模板更新直接读取 iCloud 中两个已命名的文件：

- `Clash-Compat.yaml`
- `Clash-Balance.yaml`

同步规则：

1. 使用 `Clash-Compat.yaml` 整体更新 Compat 基础模板。
2. 从 `Clash-Balance.yaml` 提取完整 `dns` 段及归属于该段的注释，更新 Balance DNS 模板。
3. 比较 Balance 与 Compat 的其余差异，将非 DNS 差异写入更新报告，但不合并。
4. 报告 Compat 内容变化、Balance DNS 变化、被忽略的非 DNS 差异及注释保留结果。
5. 新模板验证失败时，不替换仓库中的当前模板。

## 注释保留

注释属于模板的正式内容：

- Compat 的通用注释进入所有最终主配置。
- Balance 的 DNS 内容与 DNS 注释作为整体覆盖 Compat 的 DNS 段。
- Balance 的非 DNS 独有注释只进入差异报告，不自动合并。
- 动态注入用户节点、机场 provider 和规则时，不得导致原有注释丢失或整份 YAML 被无意义重排。
- 测试直接断言关键通用注释和 Balance DNS 独有注释仍然存在。

## 配置生成与发布

Compat 生成链路：

```text
Compat 完整基础模板
→ 注入用户节点信息
→ owner 注入 AmyTelecom provider
→ 输出 Clash-Compat.yaml
```

Balance 生成链路：

```text
Compat 完整基础模板
→ 整段替换为 Balance DNS（含注释）
→ 注入 owner 节点信息
→ 注入 AmyTelecom provider
→ 输出 Clash-Balance.yaml
```

统一 `sync` 生成 owner 和全部普通用户的有效配置。执行前要求 Compat、Balance DNS 和当前机场 provider 都存在且有效。所有结果通过 YAML、Mihomo 和授权映射检查后才原子切换发布；任一结果失败则保持当前发布不变。

主配置发布可回滚，但只回滚主配置和相应路由映射，不回滚机场 provider、身份令牌或本机 Home 脚本。

## 机场订阅

机场更新与 owner 配置生成完全解耦。

原始机场订阅只进入同目录的随机临时文件，不长期保存。下载、转换和验证成功后，使用 `os.replace` 原子替换：

```text
/var/lib/clash-sub/public/provider/AmyTelecom-Provider.yaml
```

权限为 `root:www-data`、`0640`。只保留当前有效版本，不保留历史或机场回滚版本。

“更新机场订阅”只更新这个 provider 文件，不触发 `sync`、主配置生成或发布切换。失败时保留当前 provider，并输出隐藏源 URL、令牌、UUID 和节点敏感字段的错误信息。

owner 主配置使用：

```yaml
proxy-providers:
  AmyTelecom:
    type: http
    url: "https://订阅域名/s/<owner-token>/AmyTelecom-Provider.yaml"
    path: ./proxy_providers/AmyTelecom-Provider.yaml
    interval: 604800
```

provider 的 Clash 显示名称保持 `AmyTelecom`。当前 owner 令牌拥有精确 Nginx 路由；普通用户令牌不能访问。owner 令牌轮换时重建路由，provider 实体文件不移动。

两份 owner 主配置引用同一稳定 URL。Clash Verge 可单独手动刷新 provider，自动刷新间隔为 7 天。

## 本机 Home 扩展

服务器不再保存或生成 Home 配置。Home 敏感内容只维护在本机 `private/clash-verge-home.js`。

脚本只对以下两个精确 profile 标题生效：

- `Clash-Compat`
- `Clash-Balance`

其他 profile 原样返回。脚本将 Home 节点、`HomeServer` 和 `ProxyServer` 分组插入主配置，并把两个分组放在“自动选择”之后；对应规则保持在规则列表预定的高优先级位置。脚本直接维护，不提供生成器，也不再使用 `home.yaml`。

## 服务端状态与路由

运行时主要目录：

```text
/var/lib/clash-sub/
├── private/
│   ├── state.json
│   ├── status.json
│   ├── operation.lock
│   ├── releases/
│   ├── current/
│   ├── staging/
│   └── journals/
└── public/
    └── provider/
        └── AmyTelecom-Provider.yaml
```

`state.json` 保存 owner、普通用户、x-ui 客户端映射及订阅令牌。旧订阅文件名和路由直接删除，不提供兼容入口。旧客户端必须重新添加新链接。

## 备份与恢复

最小备份只包含：

```text
/etc/x-ui/x-ui.db
/etc/nginx/stream-conf.d/clash-sub.conf
/etc/nginx/conf.d/clash-sub.conf
/var/lib/clash-sub/private/state.json
```

不备份证书、机场 provider、配置发布历史、运行状态、锁、日志、systemd 文件和安装状态。

恢复顺序：

1. 恢复 3x-ui 数据库。
2. 恢复 `state.json`。
3. 重新安装项目并重新签发证书。
4. 重新导入机场订阅，生成 `AmyTelecom-Provider.yaml`。
5. 执行 `sync`。
6. 核对并恢复 Nginx 配置。

保留原订阅链接要求恢复后的 x-ui 客户端 ID 与数据库一致，且不重新初始化 owner、不轮换订阅令牌。

## 旧设计清理

删除以下内容：

- `compat-office`、`compat-universal`、`balance-office` 和 privacy 模板；
- 旧档位定义、生成分支和对应测试；
- 旧订阅文件名、URL、Nginx 路由及任何兼容逻辑；
- `private/home.yaml`、`private/sources/owner/` 和所有 `.DS_Store`；
- 旧计划文档和 `plans/` 目录。

清理完成后，不得在有效代码、模板、测试和文档中残留旧业务名称。

## 文档结构

仓库只保留四份中文 Markdown 文档：

- `README.md`：项目用途、最终输出、身份差异、文档导航和常用命令。
- `DEPLOYMENT.md`：安装、监听收口、目录权限、升级卸载、首次导入、备份恢复。
- `docs/template-design.md`：模板组成、注释、iCloud 同步、导出矩阵、机场引用和 Home 脚本。
- `docs/operations.md`：模板更新、机场更新、provider 刷新、用户管理、同步、回滚、Home 脚本维护和故障处理。

文档按个人运维手册编写，不解释通用基础知识，但保留完整操作顺序、路径、注意事项和恢复条件。

## 验证标准

自动测试和仓库检查至少覆盖：

- 输出文件名与 Clash 标题大小写完全正确；
- owner 两份、普通用户一份的导出矩阵；
- 普通用户不能访问机场 provider；
- provider 单独更新不触发主配置发布；
- 机场更新使用验证后原子替换，失败保留旧文件；
- Balance 只替换 DNS；
- Compat 通用注释与 Balance DNS 独有注释均保留；
- Home 脚本只匹配两个精确标题，并把分组放在“自动选择”后；
- 不残留 privacy、office、旧 universal、旧 URL 或 `home.yaml`；
- 安装、同步、回滚、恢复和秘密扫描测试通过；
- 仓库最终只有四份 Markdown 文档。
