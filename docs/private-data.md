# 私有数据布局与边界

真实凭据只存在于服务器上的 root-only 私有目录，绝不进入 Git（`private/`
整体被忽略）。以下路径均为示例（以私有 `service.yaml` 的 `private-root` /
`public-root` 为准，示例取 `/var/lib/clash-sub/…`）。

## 布局

| 路径 | 内容 | 权限 |
| --- | --- | --- |
| `/opt/my-clash-config/private/config/service.yaml` | 全局私有设置（owner email、订阅主机名、数据库与二进制路径） | `0600` root:root |
| `<private-root>/state.json` | 用户映射与**明文令牌**（静态 Nginx 架构的有意取舍，见下） | `0600` root:root |
| `<private-root>/home.yaml` | owner 家庭覆盖层（六个顶层字段的私有 overlay，SFTP 直接覆盖的正式源文件） | `0600` root:root |
| `<private-root>/releases/<user>/…` | 每用户最近五个成功版本（manifest + 来源哈希；owner 版本另含逐字节原样的 `AmyTelecom.yaml` 机场原件） | 目录 `0700` |
| `<private-root>/operation.lock` | 同步互斥锁 | root:root |
| `<private-root>/.activation-journal.json` | 仅在运行时激活被中断时存在的旧工件快照；必须先由 `clash-sub recover` 处理 | `0600` root:root |
| `<private-root>/reference-configs/…` | 三份原始参考配置，**永久记录**，永不参与版本清理 | `0600` root:root |
| `<public-root>/releases/<user>/…` | 当前静态发布 YAML（Nginx 直接读取；owner 版本含 `AmyTelecom.yaml` 稳定机场端点的 alias 目标） | 目录 `2750` root:www-data，文件 `0640` |
| `private/workbench/balanced.yaml`（开发机仓库内） | **本地模板工作稿**：含真实节点的完整 balanced 配置，仅存在于维护它的 Mac 上 | `0600` 当前用户 |
| `private/home.yaml`（开发机仓库内） | `template-sync` 从工作稿提取的私有家庭覆盖层，SFTP 上传的源文件 | `0600` 当前用户 |

机场原件不再有独立的可变快照文件：`update-airport` 下载的响应字节
**逐字节不改写**地存入当次 owner release（私有 `0600` 与公共 `0640`
两份同 digest 拷贝），短期上游 URL 与机场登录状态永不落盘。稳定端点
`/s/<owner-token>/AmyTelecom.yaml` 始终 alias 到**当前** owner release
里的那份，因此轮换令牌、回滚版本都会连同机场原件一起切换。

`<private-root>` 全树 `0700`；`<public-root>` 归组 www-data 且 setgid，
Nginx worker 恰好可读发布文件、无法进入私有树。

## 本地工作稿（`private/workbench/balanced.yaml`）

这是**开发机上**维护的完整、私密 balanced 工作稿（含真实节点、服务器
地址、UUID、密码与 REALITY 密钥），是 `clash-sub template-sync` 的唯一
完整配置输入（另一个输入是现有 `private/home.yaml` 家庭 scope）。它不是
永久原稿，而是**滚动的本地工作副本**：每轮修改前先从服务器
下载最新已发布的 `clash-balanced.yaml`，保存为
`private/workbench/balanced.yaml`（`chmod 600`），再修改并在本机 Clash
实测。

- 敏感性与服务器 `<private-root>/home.yaml` 相同：链接即密码级。永远不
  进入 Git（`private/` 被全量忽略）、本身不上传服务器、不进入任何备份
  介质；`template-sync` 的校验失败与错误输出也绝不回显其内容。
- 权限要求：普通文件（非 symlink、单硬链接）、当前用户所有、`0600`、
  不超过 5 MiB；不满足时 `template-sync` 直接拒绝。
- 备份范围：它**不在**服务器备份清单里（服务器没有这份文件）；需要备份
  时随开发机自身的加密备份策略处理，与服务器私有数据备份互不相关。
- 数据流：公共模板经由 Git 分发（服务器 `clash-sub update` 拉取）；家庭
  覆盖层由 `template-sync` 生成为 `private/home.yaml` 后，经 SFTP 覆盖
  服务器正式文件，再由 `clash-sub sync` 校验并发布。`template-sync` 在
  本机完成结构、隔离与泄漏校验——本机不需要安装 Mihomo，最终 Mihomo
  校验固定由服务器 `clash-sub sync` 执行。

## 家庭覆盖层（`private/home.yaml` 与 `<private-root>/home.yaml`）

家庭配置以私有覆盖层形式维护，顶层只允许且必须包含六个顶层字段
（`proxies`、`proxy-groups`、`extend-proxy-groups`、`inject-node-groups`、
`inject-home-node-groups`、`rules`）；它不是可独立导入客户端的完整配置。
两份副本——开发机 `private/home.yaml`（`template-sync` 的输出之一）与
服务器 `<private-root>/home.yaml`——敏感性相同，均永不进入 Git。

首次引导：`template-sync` 把现有 `private/home.yaml` 同时当作家庭 scope
输入，全新环境没有这份文件（或权限不符）时会以 `home_source_invalid`
失败。初次可手工编写一份最小的合法 scope——六个字段齐全、`proxies` 与
`proxy-groups` 非空、injection 列表只引用其中存在的组——或从服务器备份
中的 `<private-root>/home.yaml` 恢复一份（保持 `0600`），再重新运行
`./bin/clash-sub template-sync`。

- 隔离保证：家庭覆盖层只进入 owner 的 `balanced` 与 `privacy`；owner
  standard 与 member standard 不含任何家庭节点、家庭组、家庭规则或
  相关名称痕迹。
- 上传方式唯一：用支持 SFTP 的客户端把开发机 `private/home.yaml` 直接
  覆盖服务器固定正式文件
  （`private/home.yaml → /var/lib/clash-sub/private/home.yaml`），随后在
  服务器执行 `clash-sub sync` 完成校验与发布；不存在其他文档化的传输
  方式或上传入口，程序也不管理 SFTP 凭据。`sync` 会把安全到达的文件
  规范为 `0600 root:root`。
- 失败不对称：覆盖中断或坏 YAML 可能让正式源文件失效——旧 owner release
  继续服务，但已被覆盖的正式源文件不会恢复，后续 owner 同步持续失败，
  直到重新上传修正文件；旧内容只能从覆盖前另行保留的备份恢复，运行时
  release 不是源文件备份。错误输出只有稳定脱敏代码，不含内容或名称。
- 备份边界：服务器副本包含在 `<private-root>` 全树备份里（见下）；开发
  机副本（`private/home.yaml` 与工作稿）由用户自身的加密备份策略负责。

## 为什么令牌是明文

公开路径是静态精确 location，Nginx 需要按完整令牌寻址；令牌保存在
root-only 的 `state.json` 中即等价于「文件系统权限保护」， Git、普通日志
与 Nginx access log 均不包含它。这是 2026-08-23 设计确认的取舍。

## 备份与恢复

- 必须备份两个独立来源：`/opt/my-clash-config/private/config/service.yaml` 与配置的
  `<private-root>` 全树（含 `state.json`、`home.yaml`（家庭覆盖层）、
  releases——owner release 里已含机场原件——与 `reference-configs/` 原件）。前者不在
  `<private-root>` 内，漏掉它将无法恢复服务设置。
- 备份和恢复只在管理员控制的加密存储之间进行，且备份副本同样 root-only；不得把
  这两项写入 Git、普通备份介质或公开云盘。恢复前先保留当前两项的只读副本，恢复后
  核对 `service.yaml` 为 `0600 root:root`、`<private-root>` 为 `0700 root:root`，
  并逐项核对其私有文件为 `0600 root:root`。
- Git 与普通备份介质不得携带私有数据；若凭据曾被推送到远程仓库，仅删除
  文件无法撤回历史，必须轮换凭据（令牌用 `rotate-link`，节点凭据在
  3x-ui 重建）。
- release 的正常 pruning 仅针对可验证且成对的成功版本；异常断电留下的
  损坏/未配对工件不自动删除。按 [operations.md](operations.md) 的只读盘点流程
  备份、恢复 prepared journal 后再人工判断，避免删除仍被 state 或路由引用的文件。
