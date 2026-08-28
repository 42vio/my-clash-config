# 3x-ui 人工初始化清单

本清单只记录接入本项目实际需要的 3x-ui 操作。3x-ui 与 Xray 不由本仓库
安装或修改，也不要求固定版本；兼容性由 `clash-sub` 对当前 SQLite 结构和
必要设置的只读检查决定。

## 安装 3x-ui

在 Debian 12 上直接执行 3x-ui 官方 Quick Start 命令，不传版本参数：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)
```

该命令会以 root 执行上游当前安装脚本，具体内容和默认版本由 3x-ui 项目维护。
安装时使用默认 SQLite 数据库，并记住安装器生成的面板端口、用户名、密码和
访问路径。当前官方安装器会生成随机登录信息与访问路径。

如果安装向导要求选择证书，可以像本次实际演练一样选择 IP 证书，直接通过
`https://<VPS-IP>:<面板端口>/<访问路径>/` 登录。该证书只用于整合前访问
3x-ui，不是 VLESS + REALITY 或 `clash-sub` 的依赖；后续订阅和面板的公网
TLS 证书由本项目 `install.sh` 通过 acme.sh 单独签发。

## 在面板中完成配置

1. 确认 Web Base Path 不是 `/`。官方安装器生成的随机访问路径可以直接使用。
2. 启用 3x-ui 订阅服务和 **Clash 输出**，订阅监听保持
   `127.0.0.1`。
3. 创建一个启用的 VLESS + RAW/TCP + REALITY 入站，端口使用 `10443`。
   初次验证代理时可监听 `0.0.0.0`，客户端连接 VPS 的 `10443`。
4. 添加客户端。每位用户使用独立 UUID、订阅 ID 和 email；记住 owner 的
   email，运行本项目安装器时需要输入同一个值。
5. 用 3x-ui 提供的客户端配置确认代理能够正常连接。

在运行本项目 `bash install.sh` 之前，必须让 3x-ui 面板回到回环 HTTP：

- 在面板设置中同时清空 `webCertFile`、`webKeyFile`，把面板监听改为
  `127.0.0.1` 后保存；或者
- 先在面板中把监听改为 `127.0.0.1`，再在服务器终端运行 `x-ui`，通过
  **Revoke & Remove Certificate** 删除证书。

操作后原来的 IP HTTPS 面板会立即断开，这是正常现象；接着运行本项目安装器
即可。preflight 如果发现面板证书仍然启用，会以 `panel_tls_unsupported`
停止，不会继续生成一个必然返回 502 的反向代理配置。

至此，本项目实际依赖的 3x-ui 状态只有：

- 默认 SQLite 数据库可由 `/etc/x-ui/x-ui.db` 只读访问；
- Web Base Path 非 `/`，面板监听为 `127.0.0.1`；
- `webCertFile` 与 `webKeyFile` 均为空，由 Nginx 统一终止 TLS；
- 订阅服务和 Clash 输出已启用，订阅监听为 `127.0.0.1`；
- 恰好一个已启用的 VLESS + REALITY 入站使用端口 `10443`；
- owner email 能唯一匹配一个已启用客户端。

然后继续执行 [DEPLOYMENT.md](../DEPLOYMENT.md) 的 Phase 2。安装完成后，把
Reality 入站监听从 `0.0.0.0` 改为 `127.0.0.1`，再运行：

```bash
clash-sub sync
clash-sub links
```

## 版本与升级

首次安装使用 3x-ui 官方脚本的当前默认版本，不在本仓库固定 3x-ui 或 Xray
版本。升级前仍应备份 `x-ui.db`；升级后运行 `clash-sub status` 和
`clash-sub sync`。如果数据库结构不兼容，程序会失败关闭并保留上一份已发布
配置。具体步骤见 [operations.md](operations.md) 的“3x-ui 升级流程”。

## 数据和秘密边界

`clash-sub` 只读查询 `x-ui.db` 中的客户端、流量与订阅设置，并通过回环
Clash 订阅地址获取节点 YAML；代码不会向 3x-ui 数据库写入 SQL。

REALITY 私钥、面板凭据、证书私钥、访问路径、客户端 UUID 与订阅 ID 都只
保留在服务器私有环境中，不得提交到本仓库或写入公开日志。
