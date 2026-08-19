# Clash 订阅转换服务与私有配置生成设计

**日期：** 2026-08-19  
**状态：** 待用户 review  
**范围：** `subconverter` + `sub-web` 在线转换服务、三套 Clash 模板、本地私有节点叠加生成

## 1. 目标

本项目交付两条互不混淆的链路：

1. 用 Docker Compose 提供个人/小范围使用的在线订阅转换服务，将 3x-ui 导出的订阅转换为 Clash 可读取的订阅。
2. 维护 Balanced、Balanced_Win、Privacy 三套公共 Clash 模板，并用本地脚本把指定的 3x-ui 订阅地址分别生成三份配置。

自建节点只允许本人使用。在线服务和可分享的公共模板不得包含自建节点的服务器、UUID、密码、订阅令牌或其他凭据。

## 2. 非目标

- 不在本仓库内保存真实 3x-ui 订阅地址。
- 不把自建节点凭据放进 Compose、公共模板、Docker 镜像或在线转换返回值。
- 第一阶段不实现账号系统、短链接服务、配置托管服务或多租户权限系统。
- 不替换当前 DNS、规则集和分组策略；只将其整理为可复用的模板输入。
- 不承诺仅靠“隐藏 URL”保护节点；任何包含节点凭据的配置都视为个人私有文件。

## 3. 方案与边界

### 3.1 在线服务

Compose 运行两个服务：

- `subconverter`：监听容器内 `25500`，提供 `/version` 和 `/sub`。
- `subweb`：监听容器内 `80`，作为订阅转换网页界面。

两个容器加入同一个 Compose 网络。宿主机端口默认只绑定到 `127.0.0.1`，由用户自行配置 Nginx、Caddy 或其他反向代理接入域名和 HTTPS。`subconverter` 不直接暴露公网端口。该网络保留出站访问，因为 subconverter 需要拉取 3x-ui 源订阅和规则资源；入站安全由回环端口和反向代理控制，而不是 Docker `internal` 网络。

`sub-web` 使用官方镜像。它支持在高级模式中填写自定义后端地址，因此不把域名写死在镜像里；部署文档会说明将后端域名指向 `subconverter` 的 `/sub?` 接口。第一阶段不引入自定义前端构建链，避免把上游前端源码、Node 构建环境和域名配置绑定到本仓库。

在线服务只负责转换用户输入的 3x-ui 订阅。它不读取 `private/`，也不加载本仓库的私有节点片段。

### 3.2 本地配置生成

本地脚本接收两个输入：

- 3x-ui 订阅 URL：通过命令参数或环境变量传入，不写入文件。
- subconverter 外部地址：通过环境变量传入，例如 `https://convert.example.com`。

脚本为每个模板生成一个 Clash Proxy Provider 地址：

```text
<converter>/sub?target=clash&list=true&url=<urlencoded-3x-ui-url>
```

模板中的 `proxy-providers.Subscribe` 使用这个地址获取节点。`list=true` 让 subconverter 返回可作为 Clash Proxy Provider 使用的节点列表，而不是把一整份独立 Clash 配置嵌套进 provider。

公共模板只包含订阅 provider、DNS、策略组、规则集和规则。个人生成时，脚本从被 `.gitignore` 忽略的 `private/` 目录读取私有片段，将私有节点、私有策略组和私有规则插入对应锚点；生成后的三份个人配置也默认放在被忽略的输出目录中。

### 3.3 三套模板

三套模板保留当前仓库已有的意图和差异：

- `Balanced`：策略分流 DNS，开启 `respect-rules`，最后通过 `GEOIP,CN` 进行混合判断。
- `Balanced_Win`：保留当前 Windows/游戏侧规则和结构差异。
- `Privacy`：以 Fake-IP 隐私为优先，不开启 `respect-rules`，最后的 `GEOIP,CN` 使用 `no-resolve`。

模板不再硬编码个人节点。涉及 `HomeServer`、`ProxyServer` 或具体自建节点名称的内容进入私有片段；公共模板中的策略组必须只引用公共节点/provider 或 `DIRECT`、`REJECT` 等内置目标。

## 4. 文件布局

计划新增或整理为以下结构：

```text
compose.yaml
.env.example
.gitignore
docker/
  subconverter/
    pref.ini                 # 只保留服务安全和基础行为配置
templates/
  My-Clash_Balanced.yaml.tmpl
  My-Clash_Balanced_Win.yaml.tmpl
  My-Clash_Privacy.yaml.tmpl
private/
  proxies.yaml.example       # 可提交的无凭据示例
  proxy-groups.yaml.example
  rules.yaml.example
scripts/
  generate_configs.py
tests/
  test_generate_configs.py
generated/
  .gitkeep
README.md
```

`private/proxies.yaml`、`private/proxy-groups.yaml`、`private/rules.yaml` 和 `generated/*.yaml` 均不提交。现有根目录三份 YAML 将被整理为模板来源或个人生成结果，具体迁移在实现计划中保持可恢复，不直接删除用户现有文件。

## 5. 安全设计

1. Compose 的宿主机端口只监听回环地址；反向代理层负责 HTTPS、Basic Auth 或 IP allowlist。
2. `subconverter` 启用 API 模式，避免匿名访问其本地文件/本地订阅能力；第一阶段不部署 `/getprofile` 配置档案。未来如需启用 profile，其访问 token 只能保存在未跟踪的本地配置中。
3. 不配置 `default_url`，避免服务端持有默认 3x-ui 订阅。
4. 不启用 Gist 自动上传、短链接和文件托管，避免订阅 URL 或节点信息进入第三方服务。
5. 生成脚本日志不得打印完整订阅 URL；错误信息只显示脱敏后的 URL 主机或状态。
6. 公共模板、Compose、示例文件和测试 fixture 扫描时不得出现 `password:`、真实 `uuid:`、订阅 token 或 3x-ui URL。
7. 如果自建节点凭据曾经提交到远程仓库，部署前必须轮换凭据；从 Git 工作树移除明文并不能撤回历史泄露。

## 6. 错误处理与可诊断性

- 缺少 3x-ui URL：脚本立即退出，提示命令格式，不生成半成品。
- 缺少转换后端地址：脚本立即退出，提示设置 `CONVERTER_BASE_URL`。
- 模板缺少私有片段锚点：脚本失败并指出模板文件和锚点名，避免生成结构不完整的 YAML。
- 私有片段不存在：允许生成公共配置，但命令输出明确提示“未注入私有节点”；提供 `--private` 时则将其视为错误。
- URL 编码失败或源 URL 为空：退出并不写入输出文件。
- 输出目录创建或写入失败：退出并保留已有输出文件，不做清空操作。
- Compose 健康检查使用 subconverter `/version`；部署文档提供 `docker compose config` 与 curl 检查命令。

## 7. 测试与验收标准

### 7.1 生成脚本

- 测试三种模板分别得到三个不同文件名。
- 测试 3x-ui URL 被正确 URL encode，且输出中包含 `target=clash` 和 `list=true`。
- 测试模板中的 `Subscribe` provider 被替换为 HTTP URL，而不是继续使用本地 `path`。
- 测试启用私有片段时，私有 proxy/group/rule 只出现在本地输出，不会修改公共模板。
- 测试缺少输入、缺少锚点、缺少强制私有文件时返回非零退出码且不生成半成品。
- 测试敏感值不会出现在脚本正常日志中。

### 7.2 YAML 与服务

- 三个模板和三个生成结果均能被 YAML 解析器读取。
- 三个配置均存在 `dns`、`proxy-providers.Subscribe`、`proxy-groups`、`rules`。
- `Balanced`、`Balanced_Win`、`Privacy` 的 DNS 和最终 GEOIP 行保持各自设计差异。
- `docker compose config` 成功。
- 启动后 `curl http://127.0.0.1:<converter-port>/version` 返回 subconverter 版本信息。
- `subweb` 页面能打开；高级模式填写转换后端后可生成 Clash 转换链接。

## 8. 交付后的使用方式

在线转换：打开 `subweb`，填入 3x-ui 订阅地址，客户端选择 Clash，在高级模式填写自建 subconverter 后端地址，生成在线转换链接。

个人三份配置：

```bash
CONVERTER_BASE_URL="https://convert.example.com" \
  ./scripts/generate_configs.py \
  --source-url "https://3x-ui.example/subscribe/..." \
  --private
```

脚本分别输出 Balanced、Balanced_Win、Privacy 三份配置到 `generated/`。这些文件包含个人节点凭据，只能导入自己的 Clash 客户端，不得上传公共仓库、短链接服务或发给其他人。
