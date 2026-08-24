# 3x-ui 人工初始化清单（固定版本）

本清单描述服务器面板侧的最终状态，**全部步骤人工执行**：本仓库的任何脚本
都不会下载、安装或修改 3x-ui / Xray。文中非版本号的名称、地址、端口与
路径均为示例占位，真实值只存在于 3x-ui 本身与 root-only 私有配置中。

## 固定版本

| 组件 | 固定值 |
| --- | --- |
| 操作系统 | Debian 12（bookworm），amd64 |
| 3x-ui 面板 | 3.6.0（原生安装，systemd `x-ui` 单元） |
| Xray-core | 26.6.27（在 3x-ui 内选择，关闭自动升级） |
| 公网入站 | VLESS + RAW/TCP + REALITY，TCP 443，flow `xtls-rprx-vision` |

不要升级到更高版本：升级前必须先按
[docs/operations.md](operations.md) 的「3x-ui 升级流程」做数据库兼容检查。

## 步骤（按顺序）

1. **准备干净主机**：重装 Debian 12 amd64 后，先完整执行
   [DEPLOYMENT.md](../DEPLOYMENT.md) 的「检查主机并安装轻量前置包」与 UFW
   步骤。不要复用跑过其他代理栈的主机。
2. **下载 3.6.0 官方安装脚本到本地文件并先审阅再执行**，绝不把网络响应
   直接管道给 shell。先只读确认下载工具和临时目标：

   ```bash
   curl --version; test ! -e /tmp/3x-ui-install-v3.6.0.sh
   ```

   确认后单独下载：

   ```bash
   curl --fail --show-error --location \
     --output /tmp/3x-ui-install-v3.6.0.sh \
     https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.6.0/install.sh
   ```

   下载后先进行只读校验与审阅；将 Debian `sha256sum` 输出和来源写进管理员
   私有部署日志，再决定是否执行：

   ```bash
   sha256sum /tmp/3x-ui-install-v3.6.0.sh
   less /tmp/3x-ui-install-v3.6.0.sh
   ```

   审阅通过后，最后一条才是独立的人工安装操作：

   ```bash
   bash /tmp/3x-ui-install-v3.6.0.sh v3.6.0
   ```

   最后的 `bash` 是有意保留的人工步骤。安装后保留来源与校验和在管理员
   私有部署日志（同样不进本仓库）。
3. **面板设置**：强唯一用户名与密码、启用 2FA、生成随机 Web Base Path
   （示例形状 `/x7Hq2mVt`），面板监听 `127.0.0.1:<面板端口>`（示例
   `2053`）。该回环地址稍后填入 Nginx 模板的 `{{PANEL_UPSTREAM}}`。
4. **原始订阅服务**：监听 `127.0.0.1:<订阅端口>`（示例 `2096`），并在
   面板中**启用 Clash 输出**——`clash-sub` 只从该回环 Clash 接口获取节点
   与流量，不使用 `/sub/` 或 `/json/` 原始输出。
5. **固定 Xray 版本**：在 3x-ui 中选择 Xray-core 26.6.27，用
   `<xray-二进制路径> version` 核对，并**关闭自动核心升级**。
6. **创建唯一公网入站**：协议 VLESS、传输 RAW/TCP、安全 REALITY、端口
   TCP 443、flow `xtls-rprx-vision`；REALITY dest/SNI 必须先通过本仓库
   的只读检测脚本：

   ```bash
   /opt/clash-sub/.venv/bin/python /opt/clash-sub/scripts/check_reality_target.py \
     --host <候选目标域名>
   ```

   预期 `reachable`、`tls13`、`alpn_h2`、`x25519`、`certificate_name`
   全部通过。密钥对由 3x-ui 生成，至少一个非空 short ID。除它之外任何
   进程不得监听公网 443（TCP 或 UDP）。
7. **每人一个独立客户端**：绝不共享 UUID 或订阅 ID。owner 客户端的
   email 标识必须与私有 `service.yaml` 的 `owner-email` 一致（首次同步
   时唯一匹配并持久化其数据库主键）；其他用户各建一个客户端，`clash-sub`
   首次同步时自动为每个客户端生成独立订阅令牌。

## 完成后的核对表

| 检查 | 预期 |
| --- | --- |
| `x-ui --version`（或面板关于页） | 3.6.0 |
| Xray 二进制 `version` | 26.6.27 |
| 面板监听 | 仅 `127.0.0.1:<面板端口>` |
| 订阅服务监听 | 仅 `127.0.0.1:<订阅端口>`，Clash 输出已启用 |
| `ss -H -lntp | grep ':443\b'` | 仅 Xray 进程，无 UDP 443 |
| 入站协议 / 传输 / 安全 | VLESS / tcp / reality |
| 每客户端 flow | `xtls-rprx-vision` |
| 客户端数量 | 每人一个，无共享 UUID |
| short ID | 至少一个非空值 |
| SQLite 位置 | 与 `service.yaml` 的 `xui-database` 一致（示例 `/etc/x-ui/x-ui.db`） |

## 与本仓库的数据接口

`clash-sub` 对 3x-ui 只有两类**只读**访问，代码中不存在任何写 SQL：

1. 以只读模式打开 SQLite（示例 `/etc/x-ui/x-ui.db`），仅查询客户端主键、
   email 标识、subId、启用状态、配额/到期与订阅服务设置；表或字段结构与
   固定预期不符时全局失败关闭，不修改任何已发布配置。
2. 通过已验证的回环地址 `http://127.0.0.1:<订阅端口>/<订阅路径>/<subId>`
   获取 Clash YAML 与 `Subscription-Userinfo` 流量头。

## 秘密边界

- **REALITY 私钥只留在 Xray/3x-ui 中**：不进本仓库、不进私有
  `service.yaml`、不进部署日志明文、不进任何生成的 Clash 文件；发布的
  Clash 配置只携带公钥与 short ID。
- 面板凭据、2FA 密钥、Web Base Path、订阅路径、客户端 UUID 与管理员
  私有部署日志同样不进仓库，也不出现在任何脚本输出里。
- `scripts/check_reality_target.py` 只读：仅做 TLS 1.3 / ALPN / X25519 /
   可达性 / 证书名探测，输出限于布尔值与稳定代码。
