# 重装恢复手册

目标：服务器重装后最快恢复（约 20 分钟）。

## 备份内容（clash-sub backup 产物，tar.gz 0600，含 token/uuid 等敏感信息，请异地保存）

- /etc/x-ui/x-ui.db 副本（全部入站与 client；注意为运行中拷贝，属崩溃一致快照——
  恢复前如需绝对一致，可在备份前 systemctl stop x-ui）
- private/（service.yaml）与运行时 private-root（state.json、订阅状态、releases）
- nginx clash-sub 配置 + 版本清单
- 不含证书私钥（可重签）

## 恢复步骤

1. Debian 12 安装 → 3x-ui 官方脚本安装。
2. `systemctl stop x-ui`；用备份内 x-ui.db 覆盖 /etc/x-ui/x-ui.db；`systemctl start x-ui`。
   —— 此时代理已恢复（公网 10443 直连）（若备份的 x-ui.db 来自已收口的服务器，需先把入站 listen 改回 0.0.0.0）。
3. git clone 本仓库到 /opt/my-clash-config → 恢复 private/config/service.yaml 与运行时
   private-root（state.json 等）→ `CLASH_SUB_DOMAIN=<域名> CLASH_SUB_OWNER_EMAIL=<owner> bash install.sh`
   （证书自动重签；幂等可重跑；否则 subscription_init 会以默认 owner-example 覆盖恢复的 service.yaml）。
   注意：rollback 后立即重装时，443 可能处于 TIME_WAIT（最长约 60 秒），preflight 的
   端口检查会暂时报 port_443_taken，等待后重跑即可。
4. 按部署手册 Phase 3 收口 listen=127.0.0.1，`clash-sub sync` 验证订阅。

## 回滚说明

`clash-sub rollback --install` 停用 nginx 并移除整合配置。若回滚前已收口（inbound
listen=127.0.0.1），需在 3x-ui 面板把 Reality 入站 listen 改回 0.0.0.0 才能恢复公网 10443 直连；
未收口状态下回滚则代理始终未中断。

## 域名变更（手动流程）

1. Cloudflare 为新域名配好 NS 与 sub A 记录、新 API Token。
2. 修改 private/config/service.yaml 的 subscription-authority 与 xui-public-endpoint
   （install-state.json 的 domain 决定 update 的渲染目标；直接重跑 install 换域名会被 domain_mismatch 拦截，需先改 journal 或 rollback --install 后全新安装）。
3. 修改 private/install-state.json 的 domain 字段为新域名（update 按此渲染 nginx），随后重签证书 → `clash-sub update` → `clash-sub sync`。
   旧订阅 URL 随之失效，重新分发 `clash-sub links` 输出。

## 预留扩展：Trojan 备用协议

- 现状：stream 分流已预置 `trojan.<域名>` → 127.0.0.1:20443 规则。
- 在 3x-ui 面板加 trojan inbound（listen 127.0.0.1:20443、TLS 证书引用 /etc/ssl/domain/）后，
  该协议经 443 立即可用（客户端手动配置）。
- 但订阅管线会 fail-closed：sources.normalize_xui_endpoints 的防漂移守卫拒绝非 10443 端口节点。
  要让订阅自动输出 trojan 节点，需扩展端点改写逻辑（按 inbound 端口映射公网入口）——这是
  后续迭代项，不是零代码。

## 预留扩展：第二台 VPS（真正的高可用）

订阅天然支持多节点；两台各自跑本方案，在 generator 层合并节点即可（后续迭代项）。
