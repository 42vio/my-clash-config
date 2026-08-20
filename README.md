# My Mihomo Config

个人 Clash / Mihomo 配置仓库：自建订阅转换服务 + 三套可复用的无凭据配置模板 + 本地生成脚本。

> **安全原则：** 仓库内不保存任何真实订阅地址、节点 UUID / Password、Reality 密钥等凭据。
> 自建节点只通过被 gitignore 的私有片段注入，生成结果同样只留在本地。

## 仓库组成

| 路径 | 说明 |
| --- | --- |
| `templates/` | 共享底版 `_base.yaml.tmpl` + `templates/parts/` 差异件（DNS / GEOIP），组合生成三套无凭据配置 |
| `private/` | 私有片段示例（`.example` 可提交；`proxies.yaml` 等真实文件被 gitignore） |
| `scripts/generate_configs.py` | 本地生成脚本（Python 3.9+，仅标准库） |
| `generated/` | 个人配置输出目录（内容被 gitignore） |
| `compose.yaml` | subconverter + sub-web 订阅转换服务（端口仅绑定 `127.0.0.1`） |
| `docker/subconverter/pref.ini` | subconverter 安全配置（API 模式、无默认订阅） |
| `tests/` | 标准库单元测试（21 个，含模板结构与敏感信息扫描） |
| `docs/dns-design.md` | DNS 架构与 no-resolve 策略设计方案 |
| `DEPLOYMENT.md` | 服务部署与个人配置生成完整指南 |

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
# 1. 从示例创建三份私有片段，填入自建节点 / 分组 / 规则
cp private/proxies.yaml.example private/proxies.yaml
cp private/proxy-groups.yaml.example private/proxy-groups.yaml
cp private/rules.yaml.example private/rules.yaml

# 2. 生成三份配置到 generated/
python3 scripts/generate_configs.py \
  --source-url 'https://3x-ui.example/subscription' \
  --converter-base-url 'https://convert.example.com' \
  --private
```

私有片段的列表项必须**顶格**（column 0，不带前导空格），否则会破坏生成结果的 YAML 结构。
不带 `--private` 时生成公共配置（订阅 provider 生效，无私有节点）。

## 快速开始：订阅转换服务

```bash
cp .env.example .env
docker compose up -d
curl http://127.0.0.1:25500/version   # 健康检查
```

服务端口只绑定回环地址，对外通过反向代理（HTTPS + Basic Auth / IP 白名单）暴露。
sub-web 高级模式的后端地址填 `https://convert.example.com/sub?`。
完整步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 开发

```bash
python3 -m unittest discover -s tests -v   # 21 个测试
```

底版中的占位符为 `{{ SUBSCRIPTION_PROVIDER_URL }}`、`{{ PRIVATE_PROXIES }}`、
`{{ PRIVATE_PROXY_GROUPS }}`、`{{ PRIVATE_RULES }}`、`{{ DNS_VARIANT }}`、`{{ GEOIP_VARIANT }}`，
由生成脚本替换（后两者按 `parts/` 差异件注入，输出与差异件的映射见 `scripts/generate_configs.py` 的 `TEMPLATES`）。
测试会校验：所有占位符齐全、各差异件内容差异保持（Balanced 与 Win 输出一致、Privacy 不同）、
以及公共文件中不出现个人域名 / IP / 节点名 / 凭据。

## 安全约定

- 真实订阅 URL 只通过 `--source-url` 命令行参数传入，绝不写进仓库。
- `private/*.yaml`（真实片段）与 `generated/*.yaml`（生成结果）均已 gitignore。
- 生成的配置包含个人节点凭据，只导入自己的 Clash 客户端，不得分享。
- 若凭据曾被推送到远程仓库，仅删除文件无法撤回历史，必须轮换凭据。
