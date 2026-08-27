# 私有数据布局与边界

真实凭据只存在于服务器上的 root-only 私有目录，绝不进入 Git（`private/`
整体被忽略）。以下路径均为示例（以私有 `service.yaml` 的 `private-root` /
`public-root` 为准，示例取 `/var/lib/clash-sub/…`）。

## 布局

| 路径 | 内容 | 权限 |
| --- | --- | --- |
| `/opt/my-clash-config/private/config/service.yaml` | 全局私有设置（owner email、订阅主机名、数据库与二进制路径） | `0600` root:root |
| `<private-root>/state.json` | 用户映射与**明文令牌**（静态 Nginx 架构的有意取舍，见下） | `0600` root:root |
| `<private-root>/airport.yaml` | 最新机场节点快照（规范化 proxies，不含机场 URL） | `0600` root:root |
| `<private-root>/home.yaml` | owner 自维护家庭节点 | `0600` root:root |
| `<private-root>/releases/<user>/…` | 每用户最近五个成功版本（manifest + 来源哈希） | 目录 `0700` |
| `<private-root>/operation.lock` | 同步互斥锁 | root:root |
| `<private-root>/.activation-journal.json` | 仅在运行时激活被中断时存在的旧工件快照；必须先由 `clash-sub recover` 处理 | `0600` root:root |
| `<private-root>/reference-configs/…` | 三份原始参考配置，**永久记录**，永不参与版本清理 | `0600` root:root |
| `<public-root>/releases/<user>/…` | 当前静态发布 YAML（Nginx 直接读取） | 目录 `2750` root:www-data，文件 `0640` |
| `private/workbench/balanced.yaml`（开发机仓库内） | **本地模板工作稿**：含真实节点的完整 balanced 配置，仅存在于维护它的 Mac 上 | `0600` 当前用户 |

`<private-root>` 全树 `0700`；`<public-root>` 归组 www-data 且 setgid，
Nginx worker 恰好可读发布文件、无法进入私有树。

## 本地工作稿（`private/workbench/balanced.yaml`）

这是**开发机上**维护的完整、私密 balanced 工作稿（含真实节点、服务器
地址、UUID、密码与 REALITY 密钥），是 `clash-sub template-sync` 的唯一
输入：

- 敏感性与 `<private-root>/home.yaml` 相同：链接即密码级。永远不进入
  Git（`private/` 被全量忽略）、不上传服务器、不进入任何备份介质；
  `template-sync` 的校验失败与错误输出也绝不回显其内容。
- 权限要求：普通文件（非 symlink、单硬链接）、当前用户所有、`0600`、
  不超过 5 MiB；不满足时 `template-sync` 直接拒绝。
- 备份范围：它**不在**服务器备份清单里（服务器没有这份文件）；需要备份
  时随开发机自身的加密备份策略处理，与服务器私有数据备份互不相关。
- 服务器侧的唯一数据流仍是 `clash-sub update && clash-sub sync`——工作稿
  提升为公共模板后经由 Git 分发，私密值在 `template-sync` 内被剥离并
  校验。

## 为什么令牌是明文

公开路径是静态精确 location，Nginx 需要按完整令牌寻址；令牌保存在
root-only 的 `state.json` 中即等价于「文件系统权限保护」， Git、普通日志
与 Nginx access log 均不包含它。这是 2026-08-23 设计确认的取舍。

## 备份与恢复

- 必须备份两个独立来源：`/opt/my-clash-config/private/config/service.yaml` 与配置的
  `<private-root>` 全树（含 `state.json`、`airport.yaml`、`home.yaml`、
  releases 与 `reference-configs/` 原件）。前者不在 `<private-root>` 内，漏掉
  它将无法恢复服务设置。
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
