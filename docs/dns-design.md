# Clash / Mihomo DNS 方案说明

> 记录 Clash/Mihomo DNS 架构、`no-resolve` 策略及不同设备的推荐配置。
>
> 核心目标：**尽量避免 AliDNS、DNSPod 等国内 DNS
> 获知敏感网站或未知国外域名的查询，同时兼顾国内直连、CDN 与游戏流量。**

## 1. DNS 架构

### A. 策略分流 DNS

核心：

- Fake-IP - `nameserver-policy` 区分 CN / 其他域名
- `respect-rules: true` - `proxy-server-nameserver`
- 可使用`direct-nameserver`
- DNS 查询本身参与路由

优化方向：

``` yaml
dns:
  enable: true
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16

  default-nameserver:
    - 223.5.5.5
    - 119.29.29.29

  # 海外dns需要开启，DNS 请求自身遵循 Clash 分流规则
  respect-rules: true

  # 默认：海外 DoH
  nameserver:
    - https://1.1.1.1/dns-query
    - https://8.8.8.8/dns-query
    # 机场 DoH 需要直连
    - https://<AIRPORT-DOH-1>/dns-query/<TOKEN>
    - https://<AIRPORT-DOH-2>/dns-query/<TOKEN>

  # 明确国内域名 → 国内 DNS
  nameserver-policy:
    "geosite:private,cn":
      - https://223.5.5.5/dns-query
      - https://doh.pub/dns-query

  # 代理节点域名解析
  proxy-server-nameserver:
    - https://223.5.5.5/dns-query
    - https://doh.pub/dns-query

  # DIRECT 出站域名解析
  direct-nameserver:
    - https://223.5.5.5/dns-query
    - https://doh.pub/dns-query

  direct-nameserver-follow-policy: true
```

### B. Fake-IP 隐私 DNS

核心：

- Fake-IP - 不开启 `respect-rules`
- DNS routing 尽量简单
- 主要依靠域名规则、Fake-IP 与 `no-resolve`
- Final 固定 Proxy

隐私优先版可保持国内 nameserver：

``` yaml
dns:
  enable: true
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16

  default-nameserver:
    - 223.5.5.5

  nameserver:
    - https://223.5.5.5/dns-query
    - https://doh.pub/dns-query
```

需要识别未知 CN 域名时，优化为：

``` yaml
dns:
  enable: true
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16

  default-nameserver:
    - 223.5.5.5

  # 未知 / 其他域名
  nameserver:
    - https://<AIRPORT-DOH-1>/dns-query/<TOKEN>
    - https://<AIRPORT-DOH-2>/dns-query/<TOKEN>

  # 明确 CN 域名
  nameserver-policy:
    "geosite:private,cn":
      - https://223.5.5.5/dns-query
      - https://doh.pub/dns-query
```

> **安全提示：**不要把机场 DoH 的真实 Token、订阅地址、节点
> UUID/Password 等提交到公开仓库。

## 2. Resolve 策略

### 全 No-resolve

``` yaml
- RULE-SET,SomeIPRule,Proxy,no-resolve
- GEOIP,CN,DIRECT,no-resolve
- MATCH,Proxy
```

前置 IP 类规则和最后 `GEOIP,CN`
都不允许为了匹配而主动解析域名。未知国内、未知国外最终都进入 Proxy。

### 全 Resolve

``` yaml
- RULE-SET,SomeIPRule,Proxy
- GEOIP,CN,DIRECT
- MATCH,Proxy
```

所有 IP 类规则都可能触发真实 IP 解析。未知 CN 可以正确 DIRECT，但 DNS
解析次数和暴露面更大。

### 混合模式

``` yaml
- RULE-SET,SomeIPRule,Proxy,no-resolve
- RULE-SET,AnotherIPRule,Proxy,no-resolve

# 仅最后 CN 判断允许 Resolve
- GEOIP,CN,DIRECT
- MATCH,Proxy
```

前置 IP 类规则不主动解析，只有真正一路未被识别的未知域名，最后才通过
`GEOIP,CN` 获取真实 IP 分类。

-   未知国内 → `GEOIP,CN` → DIRECT
-   未知国外 → `MATCH` → Proxy

这是兼顾 DNS 隐私与流量准确性的主要方案。

## 3. 方案对比

| #                | DNS 架构             | Resolve 策略      | `respect-rules`   | 未知国内域名                  | 未知国外域名             | DNS 隐私                                                     | 特点                               | 设备推荐                |
| ---------------- | -------------------- | ----------------- | ----------------- | ----------------------------- | ------------------------ | ------------------------------------------------------------ | ---------------------------------- | ----------------------- |
| **①**            | 策略分流 DNS         | **全 No-resolve** | ON                | ⚠️ Proxy                       | ✅ Proxy                  | 🟢 **高**：国外/其他域名走海外 DoH，不因 IP 规则额外 Resolve  | 隐私强，但未知国内可能误走代理     | 🟡 Mac 可用              |
| **②**            | 策略分流 DNS         | **全 Resolve**    | ON                | ✅ GEOIP → DIRECT              | ✅ 非CN → Proxy           | 🟢 **较高**：默认海外 DoH，但前置 IP 规则可能产生额外真实解析 | 分流准确，但存在不必要解析         | 🟡 Windows 可用          |
| **③ ⭐ Balanced** | **策略分流 DNS**     | **混合 Resolve**  | **ON / OFF 可选** | ✅ 海外DoH → GEOIP CN → DIRECT | ✅ 海外DoH → 非CN → Proxy | 🟢 **高**：明确 CN 才走国内 DNS；未知/国外由海外 DoH 解析     | **隐私、分流准确性、复杂度最平衡** | 🟢 **游戏 Windows 推荐** |
| **④ ⭐ Privacy**  | **Fake-IP 隐私 DNS** | **全 No-resolve** | OFF               | ⚠️ Proxy                       | ✅ Proxy                  | 🟢 **很高**：尽量不为 IP/GEOIP 判断解析未知域名               | **最简单；未知域名直接代理**       | 🟢 **工作 Mac 推荐**     |



## 4. 设备推荐

### 工作 Mac： Clash_Privacy

目标： - DNS 隐私优先 - 不在乎少量国内流量误走代理 - 配置简单 -
未知域名不需要为了判断 CN 而额外解析

``` text
已知国内 → DIRECT
已知国外 → Proxy
未知国内 → Proxy（可接受）
未知国外 → Proxy
```

### 游戏 Windows：

#### ③ Clash_2 Balanced

优势： - 结构更简单 - 默认海外 DoH，明确 CN 使用国内 DNS - 前置 IP 规则
`no-resolve` - 仅最后 `GEOIP,CN` 允许 Resolve - 未知国内仍可 DIRECT -
未知国外仍可 Proxy

## 5. DNS 隐私说明

本文的"DNS 隐私"主要指：

> **尽量不让 AliDNS、DNSPod 等国内 DNS resolver
> 获知敏感网站和未知国外域名的查询。**

### 本机解析不等于 DNS 泄漏

``` text
Mihomo
  ↓
海外 DoH
  ↓
查询 unknown.example
```

这是本机 Mihomo 发起真实 DNS 查询，但查询交给海外
DoH，并不等于把域名泄漏给国内 DNS。

### `no-resolve` 的准确含义

`no-resolve` 并不是"禁止 Mihomo 在任何情况下解析这个域名"。

它表示：

> **当前 IP/GEOIP 类规则不要为了判断是否命中，而主动将域名解析成真实
> IP。**

因此：

``` yaml
- GEOIP,CN,DIRECT,no-resolve
```

和：

``` yaml
- GEOIP,CN,DIRECT
```

的重要区别之一，就是未知域名是否允许在最后为了判断 CN IP
而触发真实解析。

### `respect-rules` 不等于代理服务器远端解析

`respect-rules: true` 表示：

> **Mihomo 自己发起的 DNS 查询连接也遵循 Clash 路由规则。**

例如：

``` text
Mihomo
  ↓
Proxy
  ↓
海外 DoH
  ↓
查询 github.com
```

真正回答 DNS 查询的是机场 DoH。

而下面才是另一种"代理节点远端解析"：

``` text
Mihomo
  ↓
DOMAIN=github.com
  ↓
代理节点
  ↓
代理节点自行解析 github.com
```

两者不要混淆。

## 6. 机场 DoH 使用原则

机场海外 DoH 可以替换 Cloudflare / Google DNS，前提是实际测试确认：

-   查询延迟更低
-   长时间稳定
-   无明显超时
-   国外常用服务解析正常
-   CDN 结果合理
-   DNS Leak 测试符合预期

建议重点测试：

-   GitHub
-   Google
-   Apple
-   Microsoft
-   Cloudflare
-   Steam

不要只比较 DNS Query Time，还要观察返回 IP/CDN 是否合理。

如果三个机场 DoH 性能差距明显，没有必要全部加入
`nameserver`，可保留性能最好且稳定的 1～2 个。
