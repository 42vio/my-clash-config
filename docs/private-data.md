# 私有数据布局与边界

真实凭据只存在于服务器上的 root-only 私有目录，绝不进入 Git（`private/`
整体被忽略）。以下路径均为示例（以私有 `service.yaml` 的 `private-root` /
`public-root` 为准，示例取 `/var/lib/clash-sub/…`）。

## 布局

| 路径 | 内容 | 权限 |
| --- | --- | --- |
| `/opt/clash-sub/private/config/service.yaml` | 全局私有设置（owner email、订阅主机名、数据库与二进制路径） | `0600` root:root |
| `<private-root>/state.json` | 用户映射与**明文令牌**（静态 Nginx 架构的有意取舍，见下） | `0600` root:root |
| `<private-root>/airport.yaml` | 最新机场节点快照（规范化 proxies，不含机场 URL） | `0600` root:root |
| `<private-root>/home.yaml` | owner 自维护家庭节点 | `0600` root:root |
| `<private-root>/releases/<user>/…` | 每用户最近五个成功版本（manifest + 来源哈希） | 目录 `0700` |
| `<private-root>/operation.lock` | 同步互斥锁 | root:root |
| `<private-root>/reference-configs/…` | 三份原始参考配置，**永久记录**，永不参与版本清理 | `0600` root:root |
| `<public-root>/releases/<user>/…` | 当前静态发布 YAML（Nginx 直接读取） | 目录 `2750` root:www-data，文件 `0640` |

`<private-root>` 全树 `0700`；`<public-root>` 归组 www-data 且 setgid，
Nginx worker 恰好可读发布文件、无法进入私有树。

## 为什么令牌是明文

公开路径是静态精确 location，Nginx 需要按完整令牌寻址；令牌保存在
root-only 的 `state.json` 中即等价于「文件系统权限保护」， Git、普通日志
与 Nginx access log 均不包含它。这是 2026-08-23 设计确认的取舍。

## 备份与恢复

- 只备份 `<private-root>` 全树（含 `reference-configs/` 原件）。
- 备份与恢复只在管理员控制的加密存储之间进行；恢复后核对目录 `0700`、
  文件 `0600`、属主 root:root。
- Git 与普通备份介质不得携带私有数据；若凭据曾被推送到远程仓库，仅删除
  文件无法撤回历史，必须轮换凭据（令牌用 `rotate-link`，节点凭据在
  3x-ui 重建）。
