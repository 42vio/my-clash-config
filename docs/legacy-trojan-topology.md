# 旧服务器拓扑（历史记录）

> **本文档只是历史记录，不是新服务器的安装步骤。** 文中任何端口、命令或
> fallback 机制都**不得在新服务器上执行**；新服务器（见
> [DEPLOYMENT.md](../DEPLOYMENT.md)）只运行 3x-ui/Xray REALITY 443 与
> Nginx 80/8443，不包含下述任何内容。

旧 VPS 曾运行 Jrohy 版的管理面板与核心，端口关系如下：

| 端口 | 旧归属 | 说明 |
| --- | --- | --- |
| 公网 443 | 核心 | 被独占；非本协议的 TLS 流量通过 `fallback` 机制分流 |
| fallback | `fallback_addr 127.0.0.1`、`fallback_port 1443` | 非 Trojan 的 TLS 握手被转发到本机 1443 |
| 公网 80 | `trojan-web` | 面板 Web 占用 80 |
| 8080 | Nginx | 为避开 `trojan-web` 的 80，Debian 默认 HTTP 站点被挪到 8080 |
| 1443 | Nginx | TLS fallback 站点，承接被转发来的非本协议 TLS |

要点：

- Trojan 独占公网 443，其余 TLS 流量依赖 fallback 转发到本机 1443。
- `trojan-web` 占用公网 80，导致 Nginx 的 Debian 默认 HTTP 监听只能改到
  8080。
- Nginx 在 1443 上充当 TLS fallback 站点。
- 这些端口与 fallback 机制**都不属于**新的 3x-ui 服务器：新架构中
  REALITY 独占 443（无 fallback 概念），Nginx 直接拥有 80 与 8443，
  也没有任何 8080 / 1443 监听。

旧服务器不做原地清理或迁移；本文件只为避免遗忘历史端口占用关系。
