# Private home overlay and upload design

## Goal

把 owner 的家庭节点、家庭策略组、节点注入声明、公共组扩展和家庭专用规则集中到开发机
上的一份私有文件：

```text
private/home.yaml
```

该文件由本地 `template-sync` 从完整、实测可用的
`private/workbench/balanced.yaml` 中安全提取，再通过唯一受支持的上传入口：

```bash
./scripts/upload-home.sh root@<server>
```

发送到服务器。服务器必须先验证候选家庭覆盖层和最终 owner 配置，全部通过
后才把家庭源文件与新 release 一起激活。用户不需要全局安装 `clash-sub`，
不需要知道服务器私有目录，也不需要执行第二条远程命令。

家庭覆盖层只进入 owner 的 `balanced` 和 `privacy`。owner `standard` 与
所有 member 配置不得包含家庭节点、家庭组、家庭规则、家庭组扩展或相关
名称痕迹。

## Non-goals

- 不提供 `scp`、SFTP、FTP、inbox 或直接覆盖服务器目标文件的用户流程。
- 不把家庭配置、加密后的家庭配置或完整工作稿提交 Git。
- 不增加家庭配置定时刷新、文件监听器、常驻服务或 Web 上传界面。
- 不让 `template-sync` 下载 Mihomo；继续要求 `MIHOMO_BIN` 指向维护者选择的
  固定本地二进制。
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
  symlink、硬链接、错误 owner、非 `0600` 权限和超过 5 MiB 的文件。

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
   `balanced`、`standard`、`privacy` 以及 member `standard`。
3. 验证 owner balanced/privacy 含家庭覆盖层，而两个 standard 输出均不含
   任何家庭对象或名称。
4. 对所有候选运行结构校验、固定 `MIHOMO_BIN` 校验和 tracked secret scan。
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

## Upload command and server transaction

新增 tracked executable `scripts/upload-home.sh`。唯一公开用法是：

```bash
./scripts/upload-home.sh root@<server>
```

脚本必须：

- 从脚本位置解析仓库根目录，因此可从仓库内任意当前目录调用。
- 只读固定的 `private/home.yaml`；不接受自定义源路径或远程目标路径。
- 要求一个严格验证的 `root@host` SSH 目标，拒绝选项注入、空白、shell
  元字符和额外参数。
- 在发送前拒绝非普通文件、symlink、硬链接、非当前用户、非 `0600` 和
  超过 5 MiB 的本地源。
- 通过 SSH stdin 把字节发送给服务器绝对路径命令
  `/usr/local/bin/clash-sub home-import`；不得把私密内容放进 argv、环境
  变量、日志或远程临时文件。SSH 密码提示继续由 SSH 自己通过控制终端
  处理，不得占用传输 stdin。
- 透传安全错误代码并保留远程退出码；成功只打印家庭配置已上传并同步。

服务器增加 `clash-sub home-import`：这是非菜单、非用户文档直用的
root-only stdin 导入命令，不接受文件路径、URL 或其他参数。它必须在现有
operation lock 和 recovery 之后：

1. 有上限地读取 stdin，拒绝 TTY、空输入和超限输入。
2. 在内存中解析候选 home overlay，不写 live `home.yaml`。
3. 读取当前 3x-ui snapshot、state 和当前 owner release 中已验证的
   `AmyTelecom.yaml`。
4. 用候选 home 生成并验证新的 owner release。
5. 把候选 `<private-root>/home.yaml` 作为 `0600 root:root` 私有工件加入现有
   activation transaction，与 owner release、state/current marker 和 Nginx
   routes 一起切换。
6. 激活成功后写安全状态日志并执行正常 pruning；失败时恢复旧 home、旧
   release、旧 state/current 和旧 routes，并清理候选。

服务器不存在可验证的当前 owner/airport release 时，导入返回
`home_owner_not_ready`，不单独保存未经完整组合验证的 home 文件。

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
home_owner_not_ready
home_mihomo_validation_failed
home_activation_failed
```

错误输出不包含 YAML 内容、proxy/group 名称、服务器地址、UUID、密码、密钥、
token 或工作稿标量。上传中断或 SSH 失败不改变服务器任何文件。所有失败均
返回非零码；脚本不得把失败包装成成功。

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
- 删除 public home feature、手动 SFTP/scp、服务器目录直传和 inbox 说明。
- 只展示两条本地日常命令：

  ```bash
  MIHOMO_BIN="<absolute-path>" ./bin/clash-sub template-sync
  ./scripts/upload-home.sh root@<server>
  ```

- 明确上传成功已经完成服务器同步，不需要再进入服务器菜单刷新。
- 备份边界继续包含服务器 `<private-root>/home.yaml`；开发机副本由用户的
  本机加密备份策略负责，永不进入 Git。
- 说明 owner standard/member standard 的家庭隔离保证和安全错误行为。

文档不得把服务器 private-root 路径描述成用户上传接口。现有未提交文档
修改必须保留并进行最小范围合并，尤其不得覆盖与本设计无关的
`DEPLOYMENT.md` 用户改动。

## Required code boundaries

- `clash_sub/template_sync.py`：实现 home scope 提取、双输出、候选组合校验、
  私密泄漏校验和失败恢复。
- `clash_sub/sources.py`：把 home loader 从 proxy 列表升级为严格的六字段
  overlay loader，并提供 bytes/path 两种安全入口。
- `clash_sub/generator.py`：实现 owner-only home overlay 的 groups、extensions
  与 prepended rules 组合；移除 tracked home feature 依赖。
- `clash_sub/service.py`、`clash_sub/runtime.py` 与 activation/release 边界：实现
  stdin home 导入、候选 owner 生成及 home + release 原子激活。
- `clash_sub/cli.py`：增加脚本调用的非菜单 root-only `home-import` stdin
  命令，保持普通菜单不新增家庭上传选项。
- `scripts/upload-home.sh`：实现唯一用户上传入口。
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
7. 上传脚本只接受一个安全 `root@host`，固定读取 `private/home.yaml`，通过
   stdin 发送，不泄漏内容，并准确传播 SSH/服务器退出码。
8. 上传候选解析、Mihomo、Nginx、activation 或写入任一步失败时，旧 home、
   owner release、state/current、routes 和状态时间保持一致，候选不可达。
9. 上传成功同时切换 home 与 owner release；随后普通 `sync` 使用已安装的
   home overlay，机场更新和 owner token rotation 继续正常工作。
10. 迁移测试证明旧 fragments 被正确合并后才删除，reference configs 和其他
    保留数据不受影响。
11. README/operations/private-data 的命令与文件布局断言更新；不再出现支持
    手动上传或 tracked home feature 的现行说明。
12. 完整 unittest、Mihomo 候选验证、repository safety 和两种 secret scan
    全部通过。若基线存在无关失败，必须单独复现并报告，不得以本设计为由
    修改无关代码。

## Acceptance criteria

维护者在 Mac 仓库内只需运行：

```bash
MIHOMO_BIN="<absolute-path>" ./bin/clash-sub template-sync
./scripts/upload-home.sh root@<server>
```

第一条从一份实测 balanced 工作稿安全产生 public templates 与私有 home
overlay；第二条在不暴露远程目录、不保存传输临时文件的情况下验证并发布
家庭配置。成功后 owner balanced/privacy 使用家庭节点、策略组、扩展和家庭
网段规则，owner/member standard 保持完全隔离；任何失败都不改变当前线上
结果。
