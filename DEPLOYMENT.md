# 部署指南

本仓库包含两部分：subconverter + sub-web 订阅转换服务（Docker Compose 部署），以及个人三份 Clash 配置的本地生成脚本。所有服务端口只绑定 `127.0.0.1`，对外访问一律通过反向代理提供。

## Start

本仓库的开发机未安装 Docker，无法在本地预检 Compose 配置；请在部署主机上先执行 `docker compose --env-file .env.example config` 校验，通过后再按以下步骤启动，并用 `curl http://127.0.0.1:25500/version` 做健康检查：

cp .env.example .env
docker compose up -d
curl http://127.0.0.1:25500/version

## Reverse proxy

将 sub-web 的域名反向代理到 `http://127.0.0.1:58080`，另用一个独立的 converter 域名反向代理到 `http://127.0.0.1:25500`。两个域名都必须启用 HTTPS，并配合 Basic Auth 或 IP 白名单保护；不要把 25500 端口直接暴露到公网。

## sub-web 使用说明

打开 sub-web 页面后切换到高级模式（Advanced Mode），在后端地址一栏填写 `https://convert.example.com/sub?`（即自建 subconverter 的 `/sub?` 接口）。从其他设备打开该网页时，后端地址不能填 `localhost` / `127.0.0.1`，必须填写部署主机的外部可达域名，否则浏览器侧无法访问后端。

## Personal generated configurations

先从示例文件创建三份私有片段（已被 .gitignore 忽略），再运行生成脚本：

cp private/proxies.yaml.example private/proxies.yaml
cp private/proxy-groups.yaml.example private/proxy-groups.yaml
cp private/rules.yaml.example private/rules.yaml
python3 scripts/generate_configs.py --source-url 'https://3x-ui.example/subscription' --converter-base-url 'https://convert.example.com' --private

注意事项：

- 私有片段中的列表项必须顶格（column 0，不带前导空格）。模板锚点位于行首，片段带缩进会破坏生成结果的 YAML 结构。
- 生成结果保存在 `generated/` 目录（已被 .gitignore 忽略），其中包含个人节点凭据，不得提交到仓库或分享给他人。
- 不带 `--private` 时脚本仍会生成三份公共配置，并在输出中明确提示“未注入私有节点”。

## 安全提醒

- 真实订阅 URL 只通过 `--source-url` 命令行参数传入，绝不写进本仓库的任何文件。
- 生成的三份配置只导入自己的 Clash 客户端；不得上传公共仓库、短链接服务或转发给其他人。
