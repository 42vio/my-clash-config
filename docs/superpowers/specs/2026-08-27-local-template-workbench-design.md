# 本地 Clash 工作稿与单一模板同步设计

- 日期：2026-08-27
- 状态：已与用户确认，交由其他 Agent 实现；本 Agent 负责最终代码审查
- 实现基线：当前工作区（保留已有未提交的代码审查修复，不得回退或覆盖）

## 1. 背景

当前仓库把公共 Clash 骨架放在 `templates/clash.yaml.j2`，但 `dns`、
`proxy-groups` 和 `rules` 又完整复制在 `templates/variants/balanced.yaml`、
`standard.yaml`、`privacy.yaml`。修改一条公共规则或策略组时需要同步多份文件，
容易遗漏。

用户实际习惯是在当前 Mac 上维护、导入并测试一份包含真实节点的完整
`balanced.yaml`。测试通过后，希望用一条本地命令把其中的公共策略提升到仓库模板，
再提交代码；不希望先上传到服务器调试，也不要求把完整私密配置提交到 Git。

## 2. 目标

1. 在 Mac 上直接编辑并用 Clash 测试一份完整、私密的 balanced 工作稿。
2. 一条本地命令把工作稿中的公共设置安全同步到 `templates/`。
3. 公共 DNS、策略组、rule-provider 和 rules 只保留一个事实来源，默认进入所有用户和
   `balanced`、`standard`、`privacy` 三种输出。
4. 家庭节点结构与 privacy DNS 等真实差异只保存一次，不再复制完整 variant。
5. 真实节点、节点服务器地址、UUID、密码、密钥和订阅凭据不得进入 Git、日志或错误输出。
6. 命令行子命令与完整终端菜单同时存在，并调用同一套业务实现。
7. `clash-sub update` 保持原语义，不自动同步；成功输出明确提醒
   `clash-sub update && clash-sub sync`。

## 3. 非目标

- 不实现从任意第三方 Clash 配置进行智能迁移。
- 不实现仓库模板与工作稿的双向同步。
- 不自动 commit、push 或修改服务器。
- 不把 `home.yaml`、机场快照或完整 balanced 工作稿加密后提交 Git。
- 不改变 owner/member 的节点授权边界。
- 不让 `template-sync` 自动下载 Mihomo 或其他二进制。

## 4. 最终文件结构

```text
templates/
├── clash.yaml                    # 唯一公共 Clash 策略源
├── features/
│   └── home.yaml                 # balanced/privacy 共用的家庭功能差异
└── variants/
    ├── manifest.yaml             # variant 组合与节点注入元数据
    └── privacy-dns.yaml          # privacy 独有的最小 DNS 覆盖

private/                          # 已由 .gitignore 全量忽略
└── workbench/
    └── balanced.yaml             # 用户在 Mac 上维护和测试的完整私密工作稿
```

删除现有 `templates/clash.yaml.j2` 以及三份完整 variant 文件。迁移后的公共模板是
普通 YAML，不再通过 Jinja 文本占位符拼接；生成器读取 YAML 对象、组合差异并注入节点。

### 4.1 `templates/clash.yaml`

包含以下公共顶层内容：

- 端口、LAN、mode、日志、controller、profile 等公共设置；
- 公共 DNS；
- 不含真实节点的 `proxies: []`；
- 公共 `proxy-groups`；
- 全部公共 `rule-providers`；
- 全部公共 `rules`。

新增公共规则、修改其目标策略组、调整公共策略组或 DNS 时，只修改这一个文件。

### 4.2 `templates/features/home.yaml`

这是声明式差异文件，只描述家庭功能：

- 仅 balanced/privacy 存在的策略组，例如 `HomeServer`、`ProxyServer`；
- 向公共策略组追加 `ProxyServer` 的位置；
- 仅家庭功能需要的规则，例如家庭网段到 `HomeServer`；
- 哪些组只注入家庭节点。

文件使用明确操作，不使用通用深度合并猜测 list 语义：

```yaml
add-proxy-groups:
  - name: HomeServer
    type: fallback
    proxies: [🎯 Direct]
  - name: ProxyServer
    type: select
    proxies: [🎯 Direct, HomeServer]
extend-proxy-groups:
  BiliBili: [ProxyServer]
  PT站加速: [ProxyServer]
prepend-rules:
  - IP-CIDR,192.168.2.0/24,HomeServer,no-resolve
inject-node-groups: [ProxyServer]
inject-home-node-groups: [HomeServer]
```

`add-proxy-groups` 中的组即由 home feature 拥有。`extend-proxy-groups` 只允许向已存在的
公共组追加固定组名；重复、缺失或产生循环均 fail-closed。

家庭节点的真实 proxy 对象不在这里，仍来自服务器 root-only 的
`<private-root>/home.yaml`。

### 4.3 `templates/variants/privacy-dns.yaml`

只保存 privacy 与公共 DNS 真正不同的键，使用递归 mapping merge；未出现的键继续继承
`templates/clash.yaml`。列表按键整体替换，不做隐式拼接，以避免不可预测的顺序。

### 4.4 `templates/variants/manifest.yaml`

只声明组合关系和节点注入目标，不授予数据源权限。例如：

```yaml
variants:
  balanced:
    features: [home]
    overrides: []
  standard:
    features: []
    overrides: []
  privacy:
    features: [home]
    overrides: [privacy-dns]

inject-node-groups:
  - 加速线路
  - AI服务
```

公共 `inject-node-groups` 对所有 variant 生效；feature 可以额外声明自己的普通节点与家庭
节点注入组。因此 `ProxyServer`、`HomeServer` 只定义在 home feature 一处。

owner/member 的来源授权仍由代码固定并由测试锁定：owner balanced/privacy =
3x-ui + airport + home；owner standard = 3x-ui + airport；member standard = 仅自己的
3x-ui。manifest 不能扩大权限，出现不允许的组合必须 fail-closed。

## 5. 本地工作流

### 5.1 一次性准备

用户从本机 Clash 导出当前可用的完整 balanced 配置，保存为：

```text
private/workbench/balanced.yaml
```

文件要求：普通文件、非 symlink、单硬链接、当前用户所有、权限 `0600`、大小不超过现有
`max-source-bytes` 上限。命令不得输出文件内容、节点名或路径之外的私密值。

### 5.2 日常操作

```text
1. 编辑 private/workbench/balanced.yaml
2. 在本机 Clash 导入并实际测试
3. 运行 clash-sub template-sync
4. 查看 git diff
5. 运行测试后提交 templates/ 与相应代码
6. push
7. 服务器运行 clash-sub update && clash-sub sync
```

`template-sync` 使用固定路径，不要求把私密文件路径写进命令参数。

## 6. `clash-sub template-sync` 设计

### 6.1 入口与运行环境

- 新增公开子命令 `clash-sub template-sync`。
- 只操作当前仓库工作树，不构造服务器 `ServiceConfig`，不要求 root，不访问网络。
- 不进入服务器管理菜单；它是开发机工具，不是 VPS 运维动作。
- 不执行 git add、commit、push。

### 6.2 输入校验

在任何模板写入前完成：

1. 安全读取 `private/workbench/balanced.yaml`，拒绝 symlink、硬链接、非 `0600`、
   非当前用户、超限、无效 UTF-8 和无效 YAML。
2. 根必须是 mapping，且满足现有 Clash 结构检查。
3. `proxies` 必须是非空 list，每个 proxy 必须是 mapping 且有唯一非空 name。
4. `proxy-groups` 名称唯一，所有引用在输入工作稿中可解析。
5. 工作稿不得包含 Jinja 标记或未知的 template-sync 控制字段。

所有失败只返回稳定错误码，例如 `template_source_invalid`，不回显 YAML、节点名、域名或
凭据。

### 6.3 动态节点剥离

1. 收集 `proxies[*].name` 的精确集合，仅用于内存处理。
2. 公共候选中的 `proxies` 固定写成空 list。
3. 遍历每个策略组的 `proxies` list，只删除与真实 proxy name **完全相等**的成员；
   不做 substring、正则或模糊删除，避免误删同名策略组。
4. 哪些组曾包含动态节点用于更新注入元数据：
   - 当前 home feature 的 `inject-node-groups` / `inject-home-node-groups` 已标记的组继续由
     home feature 管理；
   - 新出现且含动态节点的公共组默认加入 manifest 的全局 `inject-node-groups`，进入所有
     包含该公共组的 variant；
   - 不自动把任何新组改成由 home feature 管理。
5. proxy 对象本身、provider 凭据、证书字段和节点服务器值均不得写入候选模板。

### 6.4 公共内容与家庭差异提取

- balanced 工作稿是公共策略的输入，因此其公共 DNS、公共策略组、rule-provider 和 rules
  默认更新所有 variant。
- 现有 home feature 的 `add-proxy-groups` 所拥有的组从公共候选移回
  `features/home.yaml`。
- 公共组中指向 home feature 所有组的成员作为显式 group extension 写入 home feature。
- 目标为 home feature 所有组的规则写入 home feature；其他规则进入公共模板。
- 新策略组和新规则默认是公共内容。若未来需要新增仅家庭 variant 使用的项目，必须在实现变更中明确加入
  home feature；`template-sync` 本期不猜测新内容的私密 scope。
- `privacy-dns.yaml` 不从 balanced 工作稿覆盖，只在组合时作为最小差异层应用。
- 工作稿中的 YAML 注释、锚点、引号样式和空白格式不属于同步数据；解析再输出会规范化格式。
  策略组和规则等有语义的列表顺序必须保持，需要长期保留的说明写入仓库文档。

### 6.5 候选生成与验证

所有目标先写到临时目录，按以下顺序验证，任一步失败均保持工作树原样：

1. 重新加载候选模板、feature、override 和 manifest。
2. 使用合成的 xui/airport/home 节点生成 owner 三种 variant 和 member standard。
3. 运行现有结构校验：重复 proxy/group、未知组目标、未知 rule-provider、未解析规则目标、
   Reality 字段与授权边界。
4. 使用现有 Mihomo validator 校验四份候选输出。开发机必须通过 `MIHOMO_BIN` 提供当前固定
   Mihomo；缺失时 `template-sync` 返回 `mihomo_binary_missing`，不得降级为成功。
5. 对候选 tracked 内容运行 secret scan，并把工作稿中所有非结构性私密标量作为禁止值比对；
   任一命中返回 `template_secret_leak`。
6. 验证输出不包含工作稿中的 proxy name、server、uuid、password、token、private key、
   public key、short-id 等节点值。

全部通过后才逐文件原子替换目标。捕获到的多文件替换失败应尽力恢复调用开始前的字节与 mode，
并返回 `template_write_failed`；进程或操作系统在替换序列中崩溃时允许工作树暂时出现混合状态，
但服务器没有任何副作用，下一次执行必须从私密工作稿确定性收敛。成功只打印变更文件路径和
下一步提示，不打印 diff 内容。

## 7. 生成器改造

`clash_sub/generator.py` 改为对象级组合：

```text
load public clash.yaml
→ apply declared feature(s)
→ recursive-merge declared override(s)
→ enforce hard-coded source authorization
→ merge authorized proxies
→ inject exact proxy names into declared groups
→ safe_dump
→ existing structural/Mihomo validation
```

组合必须是确定性的：

- mapping 递归合并；
- scalar 替换；
- 普通 list 默认整体替换；
- feature 的 add-groups、extend-groups、prepend-rules 使用明确操作，不使用通用 list 猜测；
- 保持规则顺序；
- 不允许 feature 覆盖未声明拥有的公共组；
- 不允许 override 修改 `proxies`、数据源授权或注入权限。

## 8. 命令与终端菜单

### 8.1 双入口原则

- 所有服务器能力保留现有非交互子命令，适合 SSH、脚本和 systemd。
- 无参数 `clash-sub` 显示循环式终端菜单，操作结束后返回菜单，`0`、EOF 或 Ctrl-C 退出。
- 菜单项调用同一 dispatcher/业务函数，不复制实现。
- 内部命令 `update --post-update` 和 systemd 专用 `traffic-update` 不显示在菜单。
- 本地开发命令 `template-sync` 不显示在服务器菜单。

### 8.2 菜单

```text
================================
      clash-sub 管理菜单
================================
配置管理
  1. 更新机场订阅
  2. 重新生成所有配置（不更新代码）
  3. 查看订阅链接
  4. 查看状态和历史版本

程序维护
  5. 更新仓库代码
  6. 更新仓库代码并同步配置（推荐）

证书与备份
  7. 查看证书状态
  8. 强制续期证书
  9. 创建完整备份

故障与用户管理
 10. 恢复中断的配置发布
 11. 用户历史/回退
 12. 轮换用户订阅链接
 13. 重新初始化 owner
 14. 回滚整合安装

  0. 退出
================================
```

需要 user ID、release ID 等参数的操作由菜单逐项提示。轮换链接、证书强制续期、用户回退、
owner 重新初始化和安装回滚必须二次确认；安装回滚显示影响并要求输入明确确认文本。

### 8.3 update 与 sync

`clash-sub update` 保持：快照 → git pull → pip → 新进程 post-update。成功输出：

```text
代码更新完成。
如果本次修改涉及模板或生成逻辑，请继续执行：
clash-sub sync

也可以以后直接使用：
clash-sub update && clash-sub sync
```

菜单选项 6 等价于 shell 的 `clash-sub update && clash-sub sync`，但 sync 必须由 pull 后磁盘入口
启动的**新进程**执行，禁止在已加载旧模块的菜单进程中调用旧 service 对象。update 后菜单也必须
退出或 re-exec 新磁盘入口，不能继续用旧代码处理后续选项。

## 9. 安全与失败语义

- 私密工作稿永远位于 `.gitignore` 覆盖的 `private/`。
- private Git 仓库不视为秘密加密机制；任何真实节点值都禁止进入 tracked history。
- template-sync 无网络、无服务器副作用、无 git 写操作。
- 所有 YAML 使用 `yaml.safe_load`/`safe_dump`，不加载对象标签。
- 输入、候选、输出均有大小上限；错误只用稳定代码。
- 模板同步失败保持 `templates/` 原字节；服务器 sync 失败保持各用户旧 release。
- owner 三份 variant 继续原子发布；不同 member 继续彼此隔离。
- 菜单不能把机场 URL、订阅 token、UUID、节点名或私密文件内容写到终端历史/日志。

## 10. 测试要求

### 10.1 template-sync

- 安全文件矩阵：缺失、symlink/悬空 symlink、硬链接、错误 owner/mode、超限、坏 YAML。
- 真实 proxy 对象和所有动态 proxy name 被剥离。
- 名称相似但非完全相等的固定策略组不被误删。
- 新公共 DNS、组、provider、rule 同时出现在三种 owner 输出与 member standard。
- home feature 只进入 balanced/privacy；standard 不包含家庭节点、家庭组和家庭规则。
- privacy 继承公共修改，同时保持明确 DNS override。
- 新含节点组默认加入公共注入；home feature 的所有权不靠猜测扩张。
- source 授权不能由 manifest/工作稿扩大。
- 候选结构/Mihomo/secret 任一失败时 tracked 模板零改动。
- 多文件替换的可捕获注入失败时恢复旧字节与 mode；崩溃后重跑可确定性收敛。
- 错误输出不包含输入路径以外的私密值；建议连路径也只输出仓库相对固定名。

### 10.2 generator

- 四种授权输出：owner 三 variant + member standard。
- feature/override 合并顺序确定，list 行为明确，规则顺序稳定。
- 重复组、未知组、未知 provider、未解析 rule target fail-closed。
- 相同输入产生字节稳定输出；内容不变时不创建新 release。

### 10.3 CLI/menu

- 原有全部非交互命令行为与退出码保持兼容。
- 菜单循环、EOF/Ctrl-C、安全隐藏输入和无效选择。
- 菜单每项只分发一次到对应业务动作。
- 高风险操作取消时零副作用。
- update 成功提示精确包含 `clash-sub update && clash-sub sync`。
- 组合更新必须以新进程执行 sync；pull/update 失败时绝不 sync。
- update 后不得继续使用旧模块对象。

### 10.4 全量验收

- 平台无关测试全绿。
- `compileall`、secret scan（含 private-root 对比）、`git diff --check`、`pip check` 通过。
- 在 Debian 12 运行完整 discover，覆盖真实 setgid 与 socket bind。
- 使用一份专门的脱敏工作稿完成一次本地 template-sync 红绿验收。

## 11. 文档变更

- README：解释公共模板、variant 差异和本地工作稿数据流。
- `docs/operations.md`：增加 template-sync 本地流程、完整菜单和 update 后 sync 提醒。
- `docs/private-data.md`：记录 `private/workbench/balanced.yaml` 的敏感性、0600 要求和备份范围。
- DEPLOYMENT：服务器仍只执行 `clash-sub update && clash-sub sync`，不上传工作稿。

## 12. 实施边界与交接

实现 Agent 必须：

1. 保留当前工作区已有审查修复，不 reset、不覆盖无关修改。
2. 先写回归测试，再迁移模板与生成器。
3. 不提交或打印任何 `private/` 内容。
4. 不自动 commit/push；由用户决定集成方式。
5. 完成后报告精确测试命令、通过数、环境性失败清单和所有残余限制。

实现完成后，本 Agent 负责从安全边界、模板语义、数据源授权、事务失败窗口、CLI 新旧进程边界和
回归测试有效性六个方面做最终代码审查。
