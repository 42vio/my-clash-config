# My Mihomo Config

个人 Clash / Mihomo 配置仓库：自建订阅转换 / 发布服务 + 三套可复用的无凭据配置模板 + `clash-sub` 管理命令。

> **安全原则：** 仓库内不保存任何真实订阅地址、节点 UUID / Password、Reality 密钥等凭据。
> 自建节点只通过被 gitignore 的私有片段注入，生成结果同样只留在本地。

## 仓库组成

| 路径 | 说明 |
| --- | --- |
| `templates/` | 共享底版 `_base.yaml.tmpl` + `templates/parts/` 差异件（DNS / GEOIP），组合生成三套无凭据配置 |
| `private/` | 私有片段示例（`.example` 可提交；`proxies.yaml` 等真实文件被 gitignore） |
| `bin/clash-sub` | 主机管理命令（`status` / `refresh` / `rollback` / `rotate-link` 等，实现见 `clash_sub/`） |
| `generated/` | 个人配置输出目录（内容被 gitignore） |
| `compose.yaml` | 固定版本回环服务栈（subconverter / publisher / manager / validator，不发布任何 Docker 端口） |
| `config/subconverter/pref.ini` | subconverter 安全配置（API 模式、仅监听 `127.0.0.1:25500`、无默认订阅） |
| `tests/` | 标准库单元测试（含 Compose 安全契约与敏感信息扫描） |
| `docs/dns-design.md` | DNS 架构与 no-resolve 策略设计方案 |
| `DEPLOYMENT.md` | 服务部署与日常操作指南 |

## 三套配置

三份输出（`My-Clash_Balanced` / `My-Clash_Balanced_Win` / `My-Clash_Privacy`）由共享底版
`templates/_base.yaml.tmpl` 与 `templates/parts/` 下的差异件组合生成，公共内容只需改一处：

| 输出 | DNS 差异件 | `respect-rules` | GEOIP 差异件 | 适用场景 |
| --- | --- | --- | --- | --- |
| `My-Clash_Balanced` | `dns-balanced.part`（策略分流：海外 DoH 默认 + 国内域名分流） | ✅ | `geoip-resolve.part`（允许解析） | 通用 / 游戏 Windows |
| `My-Clash_Balanced_Win` | 同 Balanced（共用相同差异件，输出一致） | ✅ | 同上 | Windows 桌面 |
| `My-Clash_Privacy` | `dns-privacy.part`（Fake-IP 隐私：国内 DoH、配置最简） | ❌ | `geoip-no-resolve.part` | 工作 Mac，隐私优先 |

三套配置的差异、设计动机与设备推荐详见 [docs/dns-design.md](docs/dns-design.md)。

## 快速开始：生成个人配置

```bash
# 1. 一次性准备：私有目录属主必须是容器内的应用用户 10001
install -d -o 10001 -g 10001 -m 700 \
  private private/config private/staging private/releases \
  private/current private/logs private/sources
#    private/config/service.yaml、users.yaml 参照 config/*.example.yaml，权限 0600

# 2. 启动服务并刷新订阅
docker compose up -d
bin/clash-sub refresh
```

## 快速开始：订阅转换服务

```bash
docker compose up -d                  # 仅启动 subconverter 与 publisher
curl http://127.0.0.1:25500/version   # subconverter 健康检查
curl http://127.0.0.1:25501/healthz   # publisher 健康检查
```

服务端口只绑定回环地址，对外通过反向代理（HTTPS + Basic Auth / IP 白名单）暴露。
manager / validator 为一次性服务（compose profile `manual`），由 `bin/clash-sub`
按需运行，不随 `up` 启动。
完整步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

配置模板由 `clash_sub/rendering.py` 渲染（`templates/clash.yaml.j2` +
`templates/variants/`）。测试会校验：渲染产物结构完整、三套变体差异保持、
以及公共文件中不出现个人域名 / IP / 节点名 / 凭据。

## 安全约定

- 真实订阅 URL 只存在于被 gitignore 的 `private/` 与本机回环的 3x-ui，绝不写进仓库。
- `private/*.yaml`（真实片段）与 `generated/*.yaml`（生成结果）均已 gitignore。
- 生成的配置包含个人节点凭据，只导入自己的 Clash 客户端，不得分享。
- 若凭据曾被推送到远程仓库，仅删除文件无法撤回历史，必须轮换凭据。
