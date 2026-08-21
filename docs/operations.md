# 运维手册（clash-sub）

服务器上唯一的管理命令是 `clash-sub`；无参数或 `help` 会打印速查表。
所有输出都是脱敏的：不显示订阅 URL、令牌、节点名称或凭据。唯一例外是
`rotate-link`，它的目的就是把新链接显示一次。

```text
clash-sub status                # 服务可达性、每用户当前版本/是否待刷新/流量/证书
clash-sub refresh [user-id]     # 重建+校验+发布；无参数 = 全部用户（按 ID 排序逐个）
clash-sub airport               # 隐藏输入导入机场临时订阅，成功后自动刷新 owner
clash-sub history <user-id>     # 列出该用户最近五个成功版本
clash-sub rollback <user-id> <release-id>
clash-sub rotate-link <user-id> # 轮换令牌，新链接只显示一次
clash-sub logs [--limit N]      # 脱敏操作日志（默认 50，最多 1000）
```

没有 `refresh-all` 别名：不带参数的 `refresh` 就是全量刷新，单个用户的失败
不会阻塞其余用户，但失败用户保持旧版本。

## 机场更新（手机操作）

机场入口只在本机手机浏览器里保持登录（HTTPS Cookie 留在手机上，
**从不导出到 VPS**，服务器也不保存任何机场登录状态）：

1. 在手机浏览器登录机场后台，生成一条**短时效临时订阅 URL**（约 5 分钟）。
2. 手机 SSH 连接服务器，执行 `clash-sub airport`，在隐藏提示
   `Temporary airport subscription URL:` 处粘贴 URL 回车。URL 不进入
   shell 历史、环境变量或进程参数，导入完成后即被丢弃、不落盘。
3. 服务器立即通过回环 subconverter 下载并转换；成功后原子替换
   `private/sources/owner/airport.yaml`，并自动刷新 owner 的三种配置。
4. 失败（URL 过期、转换为空、数据畸形）时保留旧快照与旧配置，
   `clash-sub status` 中 owner 会显示待刷新或错误码。

### 机场要求"生成链接与下载同出口"时

部分机场要求生成订阅链接与下载链接使用同一公网出口。此时在手机
Quantumult X 中**只为机场门户/API 域名**添加分流规则，把这些域名指到
owner 的 3x-ui REALITY 节点即可——不必切换全局代理，也不需要 Tailscale
或 WireGuard：

```text
# Quantumult X [filter] 分流（示例占位域名）
HOST-SUFFIX,portal.example.com,Owner-REALITY
HOST-SUFFIX,api.portal.example.com,Owner-REALITY
HOST,cdn.portal.example.com,Owner-REALITY
FINAL,DIRECT
```

规则形状如上：每行一条机场相关域名 + 指向 owner REALITY 节点策略；
机场域名换成真实值，其余流量保持原状。手机保持 QX 开启时，机场看到的
出口即 VPS，临时链接即可正常生成。

## 客户端刷新是用户自己的责任

服务器只在管理事件（首次部署、显式 refresh、机场导入成功、来源/模板修改）
时重新生成配置；下载订阅**不会**触发任何生成。Clash 客户端是否自动拉取由
各用户客户端的更新设置决定；未设置更新间隔的客户端需要手动点击更新。

## 令牌轮换与凭据撤销（泄漏响应）

订阅链接等于密码——任何持有者都能下载展开后的全部节点凭据。怀疑泄漏时：

1. `clash-sub rotate-link <user-id>`：生成新令牌（服务器只存哈希），
   新链接只显示一次；旧链接在 publisher 重载设置后即失效（404）。
2. 在 3x-ui 面板撤销/重建该用户的客户端（重置 UUID 与原始订阅 ID），
   因为旧配置里已经包含旧 UUID。
3. 把新订阅链接通过既有安全渠道发给该用户一次。

## 历史与回退

- 每位用户只保留**最近五个成功版本**（`private/releases/<user-id>/`）；
  第 6 次成功发布会清理最旧的版本。
- `clash-sub history <user-id>` 列出可用版本；`clash-sub rollback
  <user-id> <release-id>` 把当前指针原子切回该版本（重新校验哈希，不重新
  转换、不重新渲染）。
- 任一失败（来源转换、结构校验、Mihomo 校验、发布）都不会改变当前版本；
  owner 的三种配置只能整体成功切换。
- `private/reference-configs/` 下的参考原件是永久记录，永不参与五版本清理。

## 3x-ui 的 Limit IP

3x-ui 面板中每客户端的 `Limit IP` 限制的是**同时观察到的公网源 IP 数**，
不是可靠设备数或连接数，由 3x-ui/Fail2ban 执行：

- 同一 NAT 后多台设备可能只算一个 IP；单设备在 Wi-Fi 与移动网络间切换
  可能算两个；CDN / IP Tunnel 场景可能不准。
- 该限制只约束 3x-ui 节点，不能约束机场或家庭节点。
- 建议其他用户从 `Limit IP = 2` 起步；严格单出口场景可设为 1。该值在
  3x-ui 面板配置，本服务不保存面板凭据、不代改面板设置。

## 证书、服务与日志

- 证书：`clash-sub status` 显示证书剩余时间与续期状态。续期失败或临近
  到期会触发 `service.yaml` 中配置的 `alert-command`（12 小时内不重复）。
  systemd 只有证书续期（每 6 小时）与每日健康检查两个定时器，它们绝不
  触发配置生成。
- 服务重启：`docker compose up -d` 只启动 subconverter 与 publisher；
  manager / validator 由 `clash-sub` 按需一次性运行。publisher 崩溃重启
  后自动重新加载设置与当前版本。
- 日志：`clash-sub logs --limit 100` 查看脱敏操作日志（时间、操作、用户、
  版本、状态、错误码）；publisher 访问日志只含路由哈希与字节数。

## 私有数据备份与恢复

`private/` 是唯一需要备份的运行数据（`config/`、`sources/`、`releases/`、
`current/`、`logs/`；`reference-configs/` 永久保留）。备份与恢复只在
管理员控制的加密存储之间进行，保持目录 `0700`、文件 `0600`、属主
`10001:10001`；Git 与普通备份介质不得携带这些数据
（见 [private-data.md](private-data.md)）。

## 故障恢复

- **域名到期：** 订阅与面板入口失效，但 REALITY 节点（使用 VPS 公网 IP）
  与已导入的客户端配置继续可用。按 DEPLOYMENT.md 第 8 节切换到 IP 模式
  （IP 证书 + 强制告警/自动续期），每用户更换一次订阅 URL。
- **VPS IP 变化（如更换机器）：** 更新 DNS 记录即可，用户链接不变；
  同时更新 `service.yaml` 的 REALITY 公网地址与 authority 后重新预检、
  `--apply`、`refresh`。
- **VPS IP 被封：** 订阅与 REALITY 同时失效（同机部署）。按预先保留的
  离线恢复清单（记录在管理员私有部署日志中）恢复服务，换 IP 后通过既有
  安全渠道向每位用户发送一次新订阅地址。
- **错误配置版本：** 用 `clash-sub history` / `rollback` 回退；必要时
  `rotate-link` 轮换令牌。

## REALITY 的边界

REALITY 降低了显式协议特征与被动扫描的暴露面，但**不能保证**规避主动探测
或封锁。部署侧保持：公网 443 独占、SNI 匹配经实测的 Target、非空 short ID、
经验证的客户端指纹、全部组件固定版本。本项目的验收目标是配置正确、公开面
最小、泄漏可隔离——不承诺不被封锁。
