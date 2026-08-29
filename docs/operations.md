# 运维手册（clash-sub）

日常管理只记一个命令：

```bash
clash-sub
```

无参数命令显示**循环式**交互菜单（操作结束回到菜单，`0`、EOF 或 Ctrl-C
退出；轮换链接、强制续期、用户回退、owner 重新初始化与安装回滚都会二次
确认，安装回滚要求输入确认文本）。日常操作在主菜单，其余功能收纳在四个
二级菜单里：

```text
╔──────────────────────────────────────────────╗
│  clash-sub 管理脚本                          │
│  0. 退出                                     │
│──────────────────────────────────────────────│
│  1. 更新机场订阅                             │
│  2. 重新生成所有配置                         │
│  3. 查看订阅链接                             │
│  4. 查看运行状态                             │
│──────────────────────────────────────────────│
│  5. 程序维护                                 │
│  6. 证书管理                                 │
│  7. 备份与恢复                               │
│  8. 用户与版本                               │
╚──────────────────────────────────────────────╝

请输入选项 [0-8]：
```

```text
程序维护（主菜单 5）        证书管理（主菜单 6）
1. 更新代码并同步配置（推荐）  1. 查看证书状态
2. 仅更新仓库代码            2. 强制续期证书
3. 升级 Mihomo 校验器        0. 返回主菜单
0. 返回主菜单

备份与恢复（主菜单 7）      用户与版本（主菜单 8）
1. 创建完整备份              1. 查看用户历史版本
2. 恢复中断的配置发布         2. 回退用户版本
3. 回滚整合安装              3. 轮换用户订阅链接
0. 返回主菜单                4. 重新初始化 owner
                            0. 返回主菜单
```

交互细节：二级菜单的 `0` 返回主菜单；空选项或超出范围的选项报错并重新
显示当前菜单；普通操作完成后提示「按回车键返回当前菜单」；ANSI 颜色只在
真实交互终端启用（标题、编号与「推荐」绿色，返回提示黄色，错误与危险
操作关键词红色），管道、重定向与全部输出值不带颜色码。

需要 user ID、release ID 的操作由菜单逐项提示。内部命令 `update
--post-update` 与 systemd 专用的 `traffic-update` 不出现在菜单；本地开发
命令 `template-sync` 也不出现在服务器菜单。

`clash-sub update`（「程序维护 → 仅更新仓库代码」等价）保持原语义：
快照 → git pull → pip → 新进程 post-update，**不自动同步**。成功输出明确提醒：

```text
代码更新完成。
如果本次修改涉及模板或生成逻辑，请继续执行：
clash-sub sync

也可以以后直接使用：
clash-sub update && clash-sub sync
```

「程序维护 → 更新代码并同步配置」等价于 shell 的 `clash-sub update &&
clash-sub sync`，但 sync 由 pull 后磁盘入口启动的**新进程**执行，禁止在
已加载旧模块的菜单进程中调用旧 service 对象；update 失败时绝不 sync。
update 成功后菜单退出（不继续使用旧代码处理后续选项），失败保留错误码
退出。

「程序维护 → 升级 Mihomo 校验器」（或 `clash-sub mihomo-update`）只检查
Mihomo 最新稳定版。升级前
校验 GitHub release 提供的 SHA-256，并用候选二进制检查当前已发布的全部 YAML；
全部通过后才原子替换。它不随 `clash-sub update` 自动运行，升级成功后按提示
执行 `clash-sub sync`。

所有输出默认脱敏：不显示令牌、UUID、subId、节点凭据或源 URL。唯一例外是
「查看订阅链接」与 `rotate-link`——它们的目的就是展示完整地址。

## 非交互子命令（systemd 与排错用）

```text
clash-sub sync                    # 重新读取 3x-ui/模板/机场原件（当前 release）/家庭覆盖层并发布（菜单 2 等价）
clash-sub traffic-update          # 仅更新流量响应头（每日 timer 调用）
clash-sub status                  # 最后成功时间、每用户当前版本、待同步来源、最近错误
clash-sub links                   # 全部有效订阅地址（按 email 分组 + 六位识别码）
clash-sub history <user-id>       # 该用户最近五个成功版本
clash-sub rollback <user-id> <release-id>
clash-sub rotate-link <user-id>   # 轮换令牌；随后用 root-only links 重新查看
clash-sub reinitialize-owner <numeric-client-id>  # 仅持久化 owner ID 消失后的人工迁移
clash-sub recover                 # root-only；启动恢复 unit 调用，不要求 Nginx 已运行
clash-sub update                  # 代码更新（见上，成功后按提示执行 sync）
clash-sub mihomo-update           # 检查并升级 Mihomo 最新稳定版
clash-sub cert [--renew]          # 证书状态 / 强制续期
clash-sub backup                  # 全量备份
clash-sub install                 # root-only 整合安装（见 DEPLOYMENT.md）
clash-sub rollback --install      # 回滚整合安装（二次确认）
clash-sub template-sync           # 开发机本地命令：工作稿 → templates/ + private/home.yaml（见下节）
```

失败时输出只有稳定错误代码（如 `操作失败（错误代码：xui_snapshot_failed）`），
不含敏感值。没有 `refresh` 命令，也没有兼容别名。

## 本地模板工作流（开发机）

公共策略只有一个事实来源 `templates/clash.yaml`；家庭差异在私有覆盖层
`private/home.yaml`（六个顶层字段：`proxies`、`proxy-groups`、
`extend-proxy-groups`、`inject-node-groups`、`inject-home-node-groups`、
`rules`），privacy 的 DNS 覆盖在 `templates/variants/privacy-dns.yaml`，
组合关系在 `templates/variants/manifest.yaml`。在 Mac 上修改公共规则/
策略组/DNS 的日常流程：

```text
1. 从服务器下载最新已发布的 clash-balanced.yaml，保存为 private/workbench/balanced.yaml（0600）
2. 在工作稿上修改，并在本机 Clash 导入实测
3. 在仓库内运行 ./bin/clash-sub template-sync
4. 只查看 tracked 公共模板的 git diff，运行测试与两种 secret scan
5. 若 tracked 模板有变更：提交并 push，服务器只执行 clash-sub update
6. 用 SFTP 把 private/home.yaml 直接覆盖 /var/lib/clash-sub/private/home.yaml
7. 在服务器执行 clash-sub sync（独立的校验与发布步骤）
```

固定命令序列（本地一条、SFTP 一次、服务器一条）：

```bash
./bin/clash-sub template-sync
# 使用 SFTP：private/home.yaml → /var/lib/clash-sub/private/home.yaml
clash-sub sync
```

工作稿不是永久原稿：`private/workbench/balanced.yaml` 是每轮直接下载
服务器最新 `clash-balanced.yaml` 后保存、修改并实测的**滚动本地工作
副本**（`chmod 600`）。工作稿中的注释、锚点与排版不属于同步数据（解析
再输出会规范化）；策略组与规则的语义顺序保持。

`template-sync` 的安全语义：

- 只读固定路径 `private/workbench/balanced.yaml`（不接收私密路径参数）；
  拒绝 symlink、硬链接、非 `0600`、非当前用户、超限（5 MiB）、坏
  UTF-8/YAML、Jinja 标记与 `_` 前缀控制字段。
- 输出固定为双路三个目标：公共 `templates/clash.yaml` 与
  `templates/variants/manifest.yaml`（`0644`），以及私有家庭覆盖层
  `private/home.yaml`（`0600`，被 Git 忽略）；任一步失败保持全部目标
  原字节。
- 家庭 scope 由现有 `private/home.yaml` 声明：其 `proxy-groups` 名称集合
  声明哪些工作稿策略组属于家庭；未声明的新组按公共候选处理，不会把机场
  或 3x-ui 节点猜测成家庭节点；scope 缺失或悬空直接失败，不静默删除
  家庭行为。
- 剥离全部动态节点（proxy 对象与组内**完全相等**的成员名）；新出现且含
  动态节点的公共组默认进入 manifest 全局注入。
- 候选先写临时目录，用合成节点重渲染 owner 三 variant 与 member
  standard，通过结构校验、隔离校验（owner standard 与 member standard
  不含任何家庭对象或名称）与泄漏比对（工作稿私密标量、家庭名称与规则、
  tracked secret scan）后才原子替换。
- 本机不需要安装 Mihomo，也不需要全局安装 `clash-sub`；最终 Mihomo 校验
  固定由服务器 `clash-sub sync` 执行。
- 成功只打印变更文件路径与下一步提示，不打印 diff；错误只有稳定代码
  （`template_source_invalid` / `template_candidate_invalid` /
  `template_secret_leak` / `template_write_failed`）。家庭 scope 文件
  （现有 `private/home.yaml`）的读取与结构失败沿用 `home_*` 系列稳定
  代码——如文件缺失或权限不符时返回 `home_source_invalid`，坏 YAML 时
  返回 `home_yaml_invalid`。
- 无网络、无服务器副作用、无 git 写操作；工作稿永远不进入 Git（`private/`
  被全量忽略），也永远不上传服务器。

## 家庭覆盖层上传与服务器发布（SFTP + sync）

家庭覆盖层的正式源文件在服务器固定为
`/var/lib/clash-sub/private/home.yaml`。唯一文档化的上传方式是用支持
SFTP 的客户端把本机 `private/home.yaml` **直接覆盖**这个固定正式路径：
SFTP 连接、认证与覆盖动作由维护者自行完成，程序不管理 SFTP 凭据，也
不存在任何其他文档化的传输方式或上传入口。SFTP 只完成源文件覆盖，
必须另外在服务器执行现有命令才算校验并发布：

```bash
clash-sub sync
```

`sync` 安全读取已被替换的正式 `home.yaml`（检查私有根与路径、拒绝
symlink/硬链接/错误所有者/空文件/超限文件，并把安全文件规范为 `0600`），
渲染 owner balanced/privacy 与 owner standard、member standard，用服务器
已有 Mihomo 验证候选 owner 配置，通过后才按正常激活事务发布新 release。
owner standard 与 member standard 不使用家庭覆盖层，也不含任何家庭节点、
家庭组、家庭规则或相关名称痕迹；owner 家庭源失败不影响 member 的既有
隔离语义。

失败语义是**不对称**的：Mihomo、生成或激活失败时不发布新 owner release，
旧 owner release 继续服务，但 SFTP 已经完成的覆盖**不会恢复**——坏 YAML、
传输截断或语义无效的正式 `home.yaml` 会留在服务器上，后续 owner 同步
持续失败，直到维护者修正本机文件并重新覆盖；旧内容只能从覆盖前另行
保留的备份恢复。错误输出只有稳定脱敏代码（如 `home_yaml_invalid`、
`home_schema_invalid`），不含 YAML 内容或任何 proxy/group 名称。

### 与 tracked 模板变更的先后顺序

若 `template-sync` 改动了 tracked 公共模板：先提交并 push，服务器**只**
执行 `clash-sub update` 拉取代码——在匹配的 `private/home.yaml` 就位前，
不要运行组合的 update+sync。随后 SFTP 覆盖 home，最后执行一次
`clash-sub sync`，使公共模板与家庭覆盖层在同一批校验中生效。若本次只有
`private/home.yaml` 变化而 tracked 模板未变，无需 `update`，直接覆盖后
`sync`。

## 机场更新（手机 SSH 完整流程）

机场入口只在本机手机浏览器里保持登录（Cookie 留在手机上，**从不导出到
VPS**，服务器不保存任何机场登录状态或机场 URL）：

1. 手机浏览器登录机场后台，生成一条**短时效临时订阅 URL**（约 5 分钟）。
2. 手机 SSH 连接服务器，执行 `clash-sub`，选 `1`（或直接用菜单），在
   隐藏输入提示「请输入机场订阅地址：」处粘贴 URL 回车。隐藏输入不回显，
   URL 不进入 argv、shell history、环境变量、日志或任何持久化文件。
3. 输入框仅接受 https:// 开头的地址；下载有固定超时与响应体上限。成功后
   服务器把响应字节**逐字节不改写**地存入当次 owner release（私有与公共
   两份同摘要拷贝，注释/格式/顺序原样），并立即重新生成、校验、发布
   owner 的三个 variant：机场节点不再内联展开，而是通过稳定地址
   `/s/<owner-token>/AmyTelecom.yaml` 以 `AmyTelecom` HTTP provider 引用
   （`interval: 0`，不引入任何后台轮询；缓存 `path` 随机场内容摘要变化，
   客户端刷新主配置即拿到新机场内容）。该稳定地址同时也是一个可独立
   使用的机场订阅/Provider 源，仅 owner 令牌可访问。
4. 失败（URL 过期、非 YAML、proxies 为空、任一 owner variant 校验不过）
   时保留旧机场原件与旧发布，`clash-sub status` 会显示错误代码。
5. 此后的 `clash-sub sync` / 令牌轮换 / 版本回退**绝不重新访问**已过期的
   上游机场 URL：sync 复用当前已验证 release 里的机场原件；轮换用新令牌
   重建 owner release（旧令牌的机场端点立即失效）；回退把稳定地址切回
   该版本携带的匹配机场原件，不会出现旧配置配新机场的组合。

### 机场要求「生成链接与下载同出口」时

部分机场要求生成订阅链接与下载链接使用同一公网出口。此时在手机
Quantumult X 中**只为机场门户/API 域名**添加分流规则，把这些域名指到
owner 的 3x-ui REALITY 节点即可——不必切换全局代理：

```text
# Quantumult X [filter] 分流（示例占位域名）
HOST-SUFFIX,portal.example.com,Owner-REALITY
HOST-SUFFIX,api.portal.example.com,Owner-REALITY
HOST,cdn.portal.example.com,Owner-REALITY
FINAL,DIRECT
```

机场域名换成真实值，其余流量保持原状。手机保持 QX 开启时，机场看到的
出口即 VPS，临时链接即可正常生成。

## 客户端刷新是用户自己的责任

下载订阅**不会**触发任何生成或实时查询——Nginx 只回静态 YAML 与最近一次
保存的流量头。Clash 客户端是否自动拉取由各用户客户端的更新设置决定；
未设置更新间隔的客户端需要手动点击更新。

## 流量头

- 每日 systemd timer 运行一次 `clash-sub traffic-update`：从**只读 SQLite**
  读取 3x-ui 客户端配额，只更新 Nginx 路由里的 `Subscription-Userinfo` 头，不生成 YAML、
  不创建版本。
- 回环 Clash endpoint 只提供各客户端的 proxy YAML；它不承担流量查询。
- 菜单「重新生成所有配置」与机场更新也会刷新流量头。
- 流量任务失败时保留上一份响应头，订阅仍可下载。
- 已知限制：两次更新之间突然耗尽的配额会短暂滞后显示；3x-ui 仍按自身
  规则禁用客户端。本项目不为此增加实时查询或提醒。

## 历史与回退

- 每位用户只保留**最近五个成功版本**；第 6 次成功发布清理最旧版本。
  参考原件（`private/reference-configs/`）永久保留，不参与清理。
- `clash-sub history <user-id>` 列出可用版本；`clash-sub rollback
  <user-id> <release-id>` 原子切回该版本（重新校验哈希，不重新抓取来源、
  不重新渲染）。
- 任一失败（来源获取、结构校验、Mihomo 校验、Nginx 激活）都不改变当前
  版本；owner 的三个 variant 是一个原子集合，要么全部成功要么全部保留
  旧版本。Nginx 配置检查（`nginx -t`）失败时不替换 include、不 reload。
- 普通用户彼此隔离：一个用户失败只保留该用户旧版本，不阻塞其他用户。

## 令牌轮换与凭据撤销（泄漏响应）

订阅链接等于密码——任何持有者都能下载展开后的全部节点凭据。怀疑泄漏时：

1. `clash-sub rotate-link <user-id>`：随机核心与六位识别码同时更换；旧路径
   立即撤销（404）。新链接可随时由 root-only `clash-sub links` 重新查看，
   旧令牌不进状态、错误或历史日志。
2. 在 3x-ui 面板撤销/重建该用户的客户端（重置 UUID），因为旧配置里已经
   包含旧 UUID——轮换链接不能撤销已导入的节点凭据。
3. 把新订阅链接通过既有安全渠道发给该用户一次。

禁用或删除 3x-ui 客户端后，下次手动同步自动撤销其公开路径，但 root-only
历史版本保留用于审计与人工恢复；重新启用同一客户端恢复原令牌，删除后
重建则获得新令牌。

## acme.sh 版本维护

安装器当前固定使用经过校验的 **acme.sh 3.1.4** release，并在执行前核对
固定的 SHA-256。不要启用 `acme.sh --upgrade --auto-upgrade`：程序自动升级与
证书自动续期是两件事，证书仍由 acme.sh 自带 cron 自动续期，固定程序版本
不会阻止续期。

把 acme.sh 版本检查纳入**每季度**运维清单；出现 acme.sh 安全公告、Cloudflare
DNS API 变化、Let's Encrypt 兼容问题或续期失败时，不等季度检查，立即评估升级。
没有上述触发条件且续期正常时，不需要为了追随最新版而升级。

升级必须人工完成以下闭环：

1. 从 acme.sh 官方 release 选择明确版本，不使用 `master`、`latest` 或管道执行的
   在线安装脚本。
2. 在受信任环境下载 release 压缩包，计算 SHA-256；同时更新
   `clash_sub/installer.py` 中的版本 URL、解压目录和 SHA-256，三者必须对应同一版本。
3. 运行 `CertificatePhaseTests`、全量测试和 secret scan；随后在 Debian 测试机完成
   一次签发/续期与 Nginx reload 验证。
4. 验证通过后再部署，并在私有运维记录中写下版本、校验值、升级日期和下次季度检查日期。

## 3x-ui 升级流程

1. **备份**：`cp -a /etc/x-ui/x-ui.db /root/x-ui.db.pre-upgrade`（示例
   路径，以私有配置为准），并确认最近五个 release 完整。
2. **停止**：`systemctl stop x-ui`，避免升级期间数据库被写。
3. **在副本上核对结构**：对备份副本只读检查表与字段（客户端表、subId、
   启用状态、配额/到期、订阅服务设置）与 `clash-sub` 当前支持的结构一致；结构变化时
   **不要升级**，先在本仓库测试环境验证新 schema。
4. **升级** 3x-ui / Xray（人工执行官方稳定版本升级步骤，不使用 `dev-latest`）。升级后保持
   `webCertFile` 与 `webKeyFile` 为空；如果安装器重新启用了面板证书，通过面板清空，或在
   `x-ui` 菜单执行 **Revoke & Remove Certificate**。公网 TLS 仍由 Nginx 统一终止。
5. `clash-sub status` 然后 `clash-sub sync`：数据库结构不匹配时全局
   失败关闭、非零退出；此时客户端**继续使用旧 YAML 和旧路由**，直到人工
   确认兼容并成功同步。

### owner 数据库 ID 消失后的显式恢复

如果迁移/重建 3x-ui 后 `clash-sub status` 返回
`owner_reinitialization_required`，不要修改 state.json，也不要猜新 ID：先在
3x-ui 确认新客户端的 numeric ID 与 `service.yaml` 的 `owner-email` **完全一致**，
再执行一次非交互命令：

```bash
clash-sub reinitialize-owner 123
```

该命令会原子撤销失效 owner 的公开路由/令牌，保留未变化数据库 ID 的映射；新
owner 没有 release，状态会显示 pending。随后按手机流程更新机场快照并执行
`clash-sub sync`，才会重新发布 owner 三份配置。验证、Nginx 或恢复 journal
失败时命令不会替换旧 state/routes。

### 崩溃工件的只读盘点

正常成功发布只保留最近五个 release。断电时配对不完整或损坏的 staged/release
目录**不会自动删除**：自动清理需要同时证明它不被 state、current marker、routes
或 prepared activation journal 引用，当前版本刻意不做这个高风险判断。排障时 root
只读盘点，先备份再人工处理：

```bash
find /var/lib/clash-sub/private/staging /var/lib/clash-sub/private/releases /var/lib/clash-sub/public/releases -xdev -printf '%m %u:%g %p\n' 2>/dev/null | sort
```

运行时激活候选文件也只读盘点；下列匹配严格限定为 state、journal、
current marker 与 routes 候选名，不把其他隐藏文件当成可处理对象。命令只输出路径，
不删除任何文件：

```bash
while IFS= read -r -d '' candidate; do
  if [[ "$candidate" =~ ^/var/lib/clash-sub/private/\.state\.json\.[[:alnum:]_]+$ ||
        "$candidate" =~ ^/var/lib/clash-sub/private/\.\.activation-journal\.json\.[[:alnum:]_]+$ ||
        "$candidate" =~ ^/var/lib/clash-sub/private/current/\.[1-9][0-9]*\.[[:alnum:]_]+$ ||
        "$candidate" =~ ^/etc/nginx/clash-sub/\.routes\.conf\.[[:alnum:]_]+$ ]]; then
    printf '%s\n' "$candidate"
  fi
done < <(
  {
    find /var/lib/clash-sub/private -path /var/lib/clash-sub/private/current -prune -o -type f -print0
    find /var/lib/clash-sub/private/current -type f -print0
    find /etc/nginx/clash-sub -type f -print0
  } 2>/dev/null
) | sort
```

同时检查 `/var/lib/clash-sub/private/.activation-journal.json`；若存在，先执行
`clash-sub recover`，不得手动删除 journal 或 release。

## 更换域名 / 更换 VPS 或 IP

- **更换域名**：域名变更流程见 [recovery.md](recovery.md) 的「域名变更」一节
  （service.yaml 的 `subscription-authority` 与 `install-state.json` 的 domain →
  重签证书 → `clash-sub update` → `clash-sub sync`）；旧订阅 URL 随之失效，
  完成后把新链接发给每位用户一次。
- **更换 VPS / IP**：在新 VPS 上按 [DEPLOYMENT.md](../DEPLOYMENT.md) 重新
  部署；迁移 `<private-root>`（含 state.json、家庭覆盖层（`home.yaml`）与
  releases——owner release 里已含机场原件）后重新签发
  证书、重建 3x-ui 客户端凭据；DNS A 记录指向新 IP；订阅地址不变（域名
  未换时）。旧 VPS 上的令牌与节点凭据全部作废。
- IP 被封时订阅与 REALITY 同时失效（同机部署）；按预先保留的离线恢复
  清单（记录在管理员私有部署日志中）恢复，换 IP 后通过既有安全渠道向
  每位用户发送一次新订阅地址。

## 3x-ui 的 Limit IP

3x-ui 面板中每客户端的 `Limit IP` 限制的是**同时观察到的公网源 IP 数**，
不是可靠设备数或连接数：

- 同一 NAT 后多台设备可能只算一个 IP；单设备在 Wi-Fi 与移动网络间切换
  可能算两个；CDN / IP Tunnel 场景可能不准。
- 该限制只约束 3x-ui 节点，不能约束机场或家庭节点。
- 建议其他用户从 `Limit IP = 2` 起步；该值在 3x-ui 面板配置，本服务不
  保存面板凭据、不代改面板设置。

## 私有数据备份

`/opt/my-clash-config/private/config/service.yaml` 与配置的 `<private-root>` 是必须
一起备份的两项私有数据：后者包含 state.json、家庭覆盖层（`home.yaml`）、
releases（owner release 内含逐字节原样的机场原件）与参考
原件，前者包含服务设置且不位于 private root 内。备份与恢复只在管理员控制的
加密存储之间进行，两个副本均保持 root-only；恢复前保留当前副本，恢复后核对
service.yaml 为 `0600 root:root`、private root 为 `0700 root:root` 和内部私有
文件为 `0600 root:root`。Git 与普通备份介质不得携带它们（见
[private-data.md](private-data.md)）。
