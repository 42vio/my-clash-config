# Private home overlay and upload design

## Goal

把 owner 的家庭节点、家庭策略组、节点注入声明、公共组扩展和家庭专用规则集中到开发机
上的一份私有文件：

```text
private/home.yaml
```

该文件由本地 `template-sync` 从完整、实测可用的
`private/workbench/balanced.yaml` 中安全提取。维护者使用 SFTP 直接覆盖服务器
正式文件 `/var/lib/clash-sub/private/home.yaml`，再在服务器执行：

```bash
clash-sub sync
```

服务器读取已经被替换的家庭源文件，用服务器现有 Mihomo 验证最终 owner
配置，验证通过后才发布新 release。本机不需要安装 Mihomo，也不需要全局
安装 `clash-sub`。

家庭覆盖层只进入 owner 的 `balanced` 和 `privacy`。owner `standard` 与
所有 member 配置不得包含家庭节点、家庭组、家庭规则、家庭组扩展或相关
名称痕迹。

## Non-goals

- 不提供上传脚本、`scp`、FTP、inbox、候选文件或自定义远程路径；唯一文档化
  的传输方式是 SFTP 直接覆盖固定正式路径。
- 不把家庭配置、加密后的家庭配置或完整工作稿提交 Git。
- 不增加家庭配置定时刷新、文件监听器、常驻服务或 Web 上传界面。
- 不在本机安装、下载或调用 Mihomo；本机 `template-sync` 只负责结构、隔离和
  私密泄漏校验，最终 Mihomo 校验固定由服务器 `clash-sub sync` 执行。
- 不承诺 SFTP 覆盖与 release 发布构成一个源文件事务。正式 `home.yaml` 一经
  覆盖，校验失败时不会自动恢复旧源文件。
- 本次不修改 YAML 注释或排版处理；`template-sync` 与运行时生成继续允许
  解析/导出过程丢弃注释并规范化格式。
- 不改变机场的 `AmyTelecom.yaml` 稳定 provider 设计、3x-ui 节点来源、用户
  variant 数量或服务器日常菜单结构。

## Private file format

`private/home.yaml` 是 Clash 配置的私有覆盖层，不是可独立导入客户端的完整
配置。顶层只允许且必须包含以下六个字段：

```yaml
proxies:
  - name: home-node-example
    type: vless
    # remaining private proxy fields

proxy-groups:
  - name: HomeServer
    type: fallback
    proxies:
      - home-node-example
  - name: ProxyServer
    type: select
    proxies:
      - 🎯 Direct
      - HomeServer

extend-proxy-groups:
  BiliBili:
    - ProxyServer
  国内流媒体:
    - ProxyServer

inject-node-groups:
  - ProxyServer

inject-home-node-groups:
  - HomeServer

rules:
  - IP-CIDR,192.168.2.0/24,HomeServer,no-resolve
```

格式规则如下：

- `proxies` 必须是非空 proxy mapping 列表，名称非空且唯一。
- `proxy-groups` 必须是非空 group mapping 列表，名称非空且唯一。
- `extend-proxy-groups` 必须把已存在的公共组名称映射到非空的家庭组名称
  列表；本期只保留 `BiliBili`、`国内流媒体` 到 `ProxyServer` 的两个
  扩展，明确移除 `PT站加速` 的 `ProxyServer` 成员。
- `inject-node-groups` 声明哪些家庭组在运行时接收该 owner variant 获准的
  全部 inline 节点，并在存在机场 provider 时接收 `use: [AmyTelecom]`；
  本期固定包含 `ProxyServer`。
- `inject-home-node-groups` 声明哪些家庭组只接收家庭 inline 节点；本期
  固定包含 `HomeServer`。
- 两类 injection 列表只能引用 `proxy-groups` 中唯一存在的组，且同一组
  不能在两个列表中重复声明。
- `rules` 是家庭专用规则列表。渲染时它整体置于公共规则之前；禁止
  `MATCH`、`FINAL` 或其他会提前截断公共规则的终结规则。
- 家庭组可以引用家庭节点、其他家庭组、存在的公共组或 Clash 内建策略，
  但所有引用在最终配置中必须唯一可解析。
- 家庭规则的策略目标必须在最终 owner 配置中存在。
- 拒绝未知顶层字段、Jinja 标记、坏 UTF-8/YAML、重复名称、悬空引用、
  symlink、硬链接、错误 owner 和超过 5 MiB 的文件。本机源必须已是 `0600`；
  服务器 `sync` 对 root 所有且位于 `0700` 私有根中的安全普通文件先规范为
  `0600` 再解析。

服务器副本固定为 `<private-root>/home.yaml`，权限 `0600 root:root`；开发机
副本权限为 `0600` 且归当前用户所有。两者都不得被 Git 跟踪。

## Local template-sync ownership model

`template-sync` 继续只读固定输入
`private/workbench/balanced.yaml`。该文件不是永久母版，而是维护者直接下载
服务器最新 `clash-balanced.yaml` 后保存、修改并实测的滚动本地工作副本。
输出从三个公开文件扩展为：

```text
templates/clash.yaml
templates/variants/manifest.yaml
private/home.yaml
```

`templates/features/home.yaml` 被删除，不再有公开的家庭 feature 文件。
manifest 中 balanced/privacy 的 `home` feature 声明随之删除；家庭覆盖层是否
参与渲染由固定 variant 授权决定，而不是由公开模板携带家庭内容。

### Scope declaration

`template-sync` 不得猜测哪些动态节点或新策略组属于家庭。现有
`private/home.yaml` 是下一次同步的家庭 scope 声明：

- 其 `proxy-groups` 名称集合声明哪些工作稿策略组属于家庭。
- `inject-home-node-groups` 声明从哪些组的工作稿成员识别家庭 inline proxy；
  这些 proxy 对象成为新的家庭 `proxies`。不得从 `inject-node-groups` 的
  全节点成员反推家庭来源。
- 工作稿中策略目标属于家庭组的规则成为新的家庭 `rules`。
- 公共组中对家庭组的引用成为新的 `extend-proxy-groups`。

首次迁移由实现 Agent 从现有私有 fragments 和当前 tracked home feature
建立一份已验证的初始 `private/home.yaml`。以后新增家庭组时，维护者必须先
在 `private/home.yaml` 中声明该组，再运行 `template-sync`；未声明的新组按
公共候选处理，且仍必须通过私密值泄漏检测。该约束避免把机场或 3x-ui
节点误归类为家庭节点。

### Extraction

对完整 balanced 工作稿执行以下确定性拆分：

1. 读取并验证工作稿、现有 `private/home.yaml` 和当前 tracked templates。
2. 从工作稿复制 scope 中声明的家庭组，保持其出现顺序。
3. 从 `inject-home-node-groups` 声明的工作稿组出发收集其家庭 inline
   proxies，保持 proxy 出现顺序；不得从 `ProxyServer` 等全节点注入组收集
   3x-ui 节点，也不收集 `AmyTelecom` provider。
4. 从复制出的家庭组删除将由运行时重新注入的 inline proxy 成员和 provider
   `use`；静态的公共组、家庭组和 Clash 内建策略成员保持不变。
5. 从公共组候选中移除家庭组成员，并把这些成员记录为家庭 group
   extensions；同时继续剥离所有动态 inline proxy 成员。
6. 把策略目标为家庭组的规则移到家庭 `rules`，其余规则留在公共模板并
   保持相对顺序。
7. 从公共候选删除家庭组、inline proxies 和运行时 provider mapping。
8. 生成新的公共模板、manifest 与 `private/home.yaml` 候选。

任何 scope 中的家庭组缺失、重复或无法解析时必须失败，不得静默删除家庭
行为。新家庭内容不得因“看起来像家庭配置”而被自动归类。

### Validation and writes

在替换任何目标前，`template-sync` 必须：

1. 验证 home overlay 的独立 schema 和引用图。
2. 使用合成 3x-ui、`AmyTelecom` provider 和 home sources 渲染 owner
   `balanced`、`standard`、`privacy` 以及 member `standard`，并运行内建
   Clash 结构检查。
3. 验证 owner balanced/privacy 含家庭覆盖层，而两个 standard 输出均不含
   任何家庭对象或名称。
4. 对所有候选运行结构校验和 tracked secret scan；本地不运行 Mihomo。
5. 把工作稿与新 `private/home.yaml` 中的私密 proxy 标量、家庭 proxy/group
   名称和完整家庭规则加入禁止泄漏集合；扫描器只能输出类别和 tracked
   路径，不能输出命中值。公共组扩展的目标名称本来就存在于公共模板，不
   单独视为泄漏。

全部通过后才替换目标。公开文件保持 `0644`；`private/home.yaml` 保持
`0600`。进程内任何写入失败必须恢复所有旧字节和 mode，不得留下公共模板
与家庭 scope 不匹配的半更新状态。命令无网络、无 Git 写操作、无服务器
副作用。

## Runtime composition

公共模板仍是策略、DNS、rule-provider 与普通策略组的唯一事实来源。
生成器按以下固定顺序组合：

1. 读取 `templates/clash.yaml` 并应用 variant 的公开 override。
2. 选择该用户和 variant 获准使用的 3x-ui、`AmyTelecom` 与 home sources。
3. 对 owner balanced/privacy 合并家庭 proxies，解决跨来源同名节点，并把
   重命名同步应用到家庭组成员引用。
4. 添加家庭策略组；拒绝与公共组同名。
5. 将 `extend-proxy-groups` 成员追加到目标公共组，拒绝缺失目标和重复成员。
6. 向 `inject-node-groups` 注入该 variant 的全部 inline 节点，并在存在机场
   provider 时添加 `use: [AmyTelecom]`；向 `inject-home-node-groups` 只注入
   经过重名处理后的家庭节点。
7. 把家庭 `rules` 整体置于公共 `rules` 之前。
8. 完成其他运行时注入、结构与 Mihomo 校验。

固定授权矩阵：

| 用户/variant | 3x-ui | AmyTelecom | home overlay |
| --- | --- | --- | --- |
| owner balanced | 是 | 是 | 是 |
| owner privacy | 是 | 是 | 是 |
| owner standard | 是 | 是 | 否 |
| member standard | 是 | 否 | 否 |

家庭配置加载失败时，owner 不创建或激活新 release；已经发布的 owner release
继续服务。member 同步沿用现有隔离语义，不得因为 owner 家庭源失败而获得
任何家庭内容。

## Manual SFTP overwrite and server validation

唯一文档化的上传方式是使用支持 SFTP 的客户端，把本机固定文件：

```text
private/home.yaml
```

直接覆盖服务器固定正式文件：

```text
/var/lib/clash-sub/private/home.yaml
```

不得建议普通明文 FTP、`scp`、上传脚本、inbox、候选文件或其他远程路径。
SFTP 连接、认证和覆盖动作由维护者自行完成，程序不管理 SFTP 凭据。

覆盖后维护者在服务器执行现有命令：

```bash
clash-sub sync
```

`sync` 在 operation lock 和 recovery 后处理正式 `home.yaml`：

1. 先以 `lstat` 检查私有根和路径，再用 `O_NOFOLLOW` 打开并以 `fstat` 确认
   同一个文件描述符是 root 所有的普通单链接文件，大小非零且不超过
   5 MiB；不安全文件立即失败。
2. 在私有根目录仍为 `0700 root:root` 的前提下，通过 `fchmod` 把已经打开的
   安全文件规范为 `0600`，避免 SFTP 客户端使用默认 `0644`。
3. 严格解析六字段 home overlay；不把内容或私密名称写入输出和状态日志。
4. owner balanced/privacy 使用新的 home；owner standard 与所有 member
   standard 不使用 home。读取当前 release 中已验证的 `AmyTelecom.yaml`，
   配合当前 3x-ui snapshot 生成候选 release。
5. 用服务器已有 Mihomo 验证候选 owner 完整配置；通过后沿用正常 activation
   transaction 发布 release、state/current 和 Nginx routes。
6. Mihomo、生成或 activation 失败时不发布新的 owner release，旧订阅继续
   服务；member 仍按现有逐用户隔离语义处理。

这个流程有意不回滚源文件：SFTP 已经在命令执行前覆盖正式 `home.yaml`。
若新文件坏 YAML、截断或语义无效，`sync` 返回脱敏错误，旧 owner release
继续服务，但服务器上的正式 `home.yaml` 保持错误版本，后续 owner 同步也会
继续失败，直到维护者修复本机文件并重新 SFTP 覆盖。旧 `home.yaml` 只能从
执行 SFTP 前制作的备份或既有外部备份恢复；运行时 release 不是源文件备份。

## Error contract

新增错误必须是稳定、可测试且脱敏的代码。至少区分：

```text
home_source_invalid
home_yaml_invalid
home_schema_invalid
home_proxy_invalid
home_group_invalid
home_group_reference_invalid
home_rule_invalid
home_extension_invalid
home_mihomo_validation_failed
```

Nginx/routes/current 切换失败继续使用现有全局 `sync_activation_failed`，因为
正式 source 已在 transaction 外由 SFTP 替换，不创建虚假的 home source
原子激活错误。

错误输出不包含 YAML 内容、proxy/group 名称、服务器地址、UUID、密码、密钥、
token 或工作稿标量。SFTP 中断可能留下缺失或截断的正式源文件；随后 `sync`
必须失败关闭并保留旧 release。所有命令失败均返回非零码，不得把失败包装
成成功。

## Migration and cleanup

一次性迁移顺序：

1. 从当前开发机私有 `proxies.yaml`、`proxy-groups.yaml` 与已实测 balanced
   工作稿取得家庭 proxies/groups；不得把其内容打印到终端或测试输出。
2. 从当前 `templates/features/home.yaml` 迁移两个 injection 声明、
   `BiliBili`/`国内流媒体` 两个公共组扩展和
   `IP-CIDR,192.168.2.0/24,HomeServer,no-resolve` 家庭规则；不迁移
   `PT站加速` 的 `ProxyServer` 扩展。
3. 不复制旧 `private/rules.yaml` 中已经存在于公共模板的重复规则。
4. 生成并验证 `private/home.yaml`，确认其 mode 为 `0600`。
5. 运行新版 `template-sync`，确认 public/private 候选与所有 variant 通过。
6. 仅在迁移成功后删除开发机顶层旧 fragments：
   `private/proxies.yaml`、`private/proxy-groups.yaml`、`private/rules.yaml` 和
   `private/.DS_Store`（存在时）。

必须保留 `private/workbench/balanced.yaml`、`private/reference-configs/`、
服务器配置和其他仍有文档保留要求的私有来源。不得扩大清理范围。

## Documentation changes

更新 README、operations、private-data 及受影响的部署/恢复说明：

- 把本地工作流说明改为最新服务器 balanced 下载形成的滚动 workbench +
  生成的私有 home overlay；不得再称 workbench 为永久原稿。
- 把 `template-sync` 输出改为公共模板与 `private/home.yaml` 双输出。
- 删除 public home feature、上传脚本、`scp`、FTP、inbox 和候选文件说明。
- 展示以下本地命令、固定 SFTP 路径和服务器命令：

  ```bash
  ./bin/clash-sub template-sync
  # 使用 SFTP：private/home.yaml → /var/lib/clash-sub/private/home.yaml
  clash-sub sync
  ```

- 明确 SFTP 只覆盖源文件，必须另外执行服务器 `clash-sub sync` 才会校验并
  发布；失败只保护当前 release，不恢复已覆盖的正式源文件。
- 若 `template-sync` 改动 tracked public templates，先提交/push，再在服务器
  只执行 `clash-sub update` 拉取代码；不要提前运行组合的 update+sync。随后
  SFTP 覆盖 home，最后执行一次 `clash-sub sync`，使 public/home 同批验证。
- 备份边界继续包含服务器 `<private-root>/home.yaml`；开发机副本由用户的
  本机加密备份策略负责，永不进入 Git。
- 说明 owner standard/member standard 的家庭隔离保证和安全错误行为。

固定 `/var/lib/clash-sub/private/home.yaml` 是唯一允许写入文档的 SFTP 目标；
不得把其他 private-root 路径描述成上传接口。现有未提交文档修改必须保留并
进行最小范围合并，尤其不得覆盖与本设计无关的 `DEPLOYMENT.md` 用户改动。

## Required code boundaries

- `clash_sub/template_sync.py`：实现 home scope 提取、双输出、本地结构/隔离
  校验、私密泄漏校验和失败恢复；移除本地 Mihomo 要求。
- `clash_sub/sources.py`：把 home loader 从 proxy 列表升级为严格的六字段
  overlay loader，并提供 bytes/path 两种安全入口。
- `clash_sub/generator.py`：实现 owner-only home overlay 的 groups、extensions
  与 prepended rules 组合；移除 tracked home feature 依赖。
- `clash_sub/service.py` 与运行时 source/release 边界：让现有 `sync` 安全读取
  已被 SFTP 覆盖的正式 home source，规范 mode，生成并验证 owner 候选；
  校验失败保留旧 release 但不恢复源文件。
- `clash_sub/cli.py`：保持现有 `sync` 命令和菜单结构，不增加 `home-import`
  或家庭上传入口。
- `scripts/scan_tracked_secrets.py`：把根级 `private/home.yaml` 标量纳入内存
  泄漏比对，但永不输出命中值。
- `templates/variants/manifest.yaml`：移除 home feature 声明。
- 删除 `templates/features/home.yaml`，清理仅由该 feature 使用的 composition
  代码与测试；不做无关重构。

## Verification plan

实现至少覆盖以下测试：

1. 有效 workbench + 既有 home scope 同时生成正确公共模板和 `0600`
   `private/home.yaml`，并保留顺序。
2. 家庭 proxies、groups、extensions、injection 声明和 rules 都不出现在
   tracked outputs；私密标量泄漏会原子拒绝全部输出。
3. 新家庭节点配置从 workbench 刷新；未声明的新家庭组不会被猜测成 home；
   scope 缺失或悬空会失败。
4. owner balanced/privacy 含家庭节点、组、两个扩展、全节点/home-only 注入
   和前置规则；`PT站加速` 不含 `ProxyServer`；owner standard 与 member
   standard 不含任何家庭对象或名称。
5. 跨来源 proxy 重名后，家庭组引用同步指向最终名称。
6. 未知字段、坏 YAML、重复名称、无效 proxy/group/rule/extension/injection、
   终结规则、不安全本地文件和超限文件均返回对应脱敏代码。
7. `template-sync` 在没有本机 Mihomo 和 `MIHOMO_BIN` 时正常运行，仍原子执行
   结构、variant 隔离和私密泄漏校验。
8. SFTP 后的正式 home 是安全普通文件时，`sync` 自动规范为 `0600`；symlink、
   hard link、错误 owner、空文件和超限文件均失败且不输出内容。
9. 新 home 解析、Mihomo、Nginx 或 activation 任一步失败时，正式 home 保持
   新上传字节，旧 owner release/state/current/routes 保持服务；修复并再次
   `sync` 后才发布。成功后普通同步、机场更新和 owner token rotation 继续
   使用已验证的 home overlay。
10. 迁移测试证明旧 fragments 被正确合并后才删除，reference configs 和其他
    保留数据不受影响。
11. README/operations/private-data 的命令与文件布局断言更新；只描述 SFTP
    直覆固定正式 home 路径，不再出现上传脚本、inbox 或 tracked home feature。
12. 完整 unittest、服务器 Mihomo 路径的合成/可选真实验证、repository safety
    和两种 secret scan 全部通过。若基线存在无关失败，必须单独复现并报告，
    不得以本设计为由修改无关代码。

## Acceptance criteria

维护者在 Mac 仓库内运行：

```bash
./bin/clash-sub template-sync
```

随后用 SFTP 把 `private/home.yaml` 直接覆盖到服务器
`/var/lib/clash-sub/private/home.yaml`，再在服务器执行：

```bash
clash-sub sync
```

本地命令在没有 Mihomo 时从实测 balanced 工作稿产生 public templates 与私有
home overlay；服务器命令用真实 Mihomo 校验并发布。成功后 owner
balanced/privacy 使用家庭节点、策略组、扩展和家庭网段规则，owner/member
standard 保持完全隔离。失败不改变当前线上 release，但不会撤销 SFTP 已经
完成的正式 `home.yaml` 覆盖。
