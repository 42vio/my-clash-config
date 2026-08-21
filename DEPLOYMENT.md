# 部署指南

本仓库部署一套固定版本的回环服务栈（Docker Compose）：subconverter（订阅转换）、
publisher（令牌化订阅发布），以及由 `bin/clash-sub` 按需运行的一次性 manager /
validator 容器。所有 HTTP 监听只绑定 `127.0.0.1`，不发布任何 Docker 端口；
对外访问一律通过反向代理提供。

## Start

本仓库的开发机未安装 Docker，无法在本地预检 Compose 配置；请在部署主机上先执行
`docker compose config` 校验，通过后再按以下步骤启动：

```bash
# 一次性准备：私有目录属主必须是容器内的应用用户 10001
# （Docker 代建的目录属于 root，manager/publisher/validator 将无法读写）
install -d -o 10001 -g 10001 -m 700 \
  private private/config private/staging private/releases \
  private/current private/logs private/sources
# private/config/service.yaml、users.yaml 参照 config/*.example.yaml 填写，权限 0600

docker compose up -d                  # 仅启动 subconverter 与 publisher
curl http://127.0.0.1:25500/version   # subconverter 健康检查
curl http://127.0.0.1:25501/healthz   # publisher 健康检查
```

## Reverse proxy

用一个独立的 converter 域名反向代理到 `http://127.0.0.1:25500`，另将订阅域名
反向代理到 `http://127.0.0.1:25501`。两个域名都必须启用 HTTPS，并配合 Basic Auth
或 IP 白名单保护；不要把 25500/25501 端口直接暴露到公网。

## 日常操作（bin/clash-sub）

manager 与 validator 是一次性容器（compose profile `manual`），不随
`docker compose up` 启动，只由主机命令按需运行：

```bash
bin/clash-sub status            # 服务可达性、各用户发布状态、证书健康
bin/clash-sub refresh           # 重建、校验并发布全部用户配置
bin/clash-sub logs --limit 50   # 最近的脱敏操作日志
```

## 安全提醒

- 真实订阅 URL、令牌哈希与节点凭据只存在于被 gitignore 的 `private/`
  （以及本机回环的 3x-ui），绝不写进本仓库的任何文件。
- 生成的三份配置只导入自己的 Clash 客户端；不得上传公共仓库、短链接服务
  或转发给其他人。
