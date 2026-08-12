# Surface Wi-Fi Calling Surge Rules

这个仓库整理的是一套可直接给 Surge 5 使用的 Wi-Fi Calling / IMS 外部规则集，核心目标是：

- 每个国家单独一个规则集
- 每个国家可以绑定不同的节点组
- 避免把汇总规则和国家规则一起导入，造成重复命中

> 规则主体按公开 MCC/MNC 数据生成，用于覆盖各国 PLMN 对应的 IMS/ePDG 候选域名；它不代表列表中的每一家运营商都已确认开通 Wi-Fi Calling。少量运营商专用 FQDN/IP 与 eSIM/SM-DP+ 地址另有公开来源，见规则注释。

## 推荐使用方式

如果你已经有自己的主配置，不要直接导入完整 `main.conf`，而是把下面这两类内容接到你的主配置里：

- `[Proxy Group]` 里的国家组
- `[Rule]` 里的 `RULE-SET` 引用

Surge 是自上而下首命中优先，所以这些 Wi-Fi Calling 规则必须放在你自己的更宽泛规则之前，尤其是：

- 任何 `GEOIP`
- 任何泛化的 `DOMAIN-SUFFIX`
- 任何兜底 `FINAL`

如果两个国家的规则有重叠，排在更上面的国家会先命中；所以建议把你最常用、最优先的国家放前面。

当前顺序已经把更依赖运营商要求或市场认证的国家尽量排在前面。

## 当前推荐入口

- 规则片段参考：`outputs/surge_wifi_calling_country_carriers/merge_snippet.conf`
- 独立完整配置：`outputs/surge_wifi_calling_country_carriers/main.conf`
- 国家规则集目录：`outputs/surge_wifi_calling_country_carriers/rulesets/`

## 直接可用的 raw URL

完整配置：

- `https://raw.githubusercontent.com/wzx-moe/surface-wifi-calling-surge-rules/main/outputs/surge_wifi_calling_country_carriers/main.conf`

国家规则集模板：

- `https://raw.githubusercontent.com/wzx-moe/surface-wifi-calling-surge-rules/main/outputs/surge_wifi_calling_country_carriers/rulesets/WiFiCalling-国家代码.list`

示例：

- 美国：`https://raw.githubusercontent.com/wzx-moe/surface-wifi-calling-surge-rules/main/outputs/surge_wifi_calling_country_carriers/rulesets/WiFiCalling-US.list`
- 日本：`https://raw.githubusercontent.com/wzx-moe/surface-wifi-calling-surge-rules/main/outputs/surge_wifi_calling_country_carriers/rulesets/WiFiCalling-JP.list`
- 乌克兰：`https://raw.githubusercontent.com/wzx-moe/surface-wifi-calling-surge-rules/main/outputs/surge_wifi_calling_country_carriers/rulesets/WiFiCalling-UA.list`

## 重复规则怎么处理

仓库里有一个汇总文件 `WiFiCalling-All.list`，它会把所有国家的规则都合在一起，便于查看覆盖范围，但**不建议和各国家单独规则同时导入**。

原因很简单：Surge 先命中的规则会直接生效。如果你同时导入汇总规则和国家规则，或者把更泛的规则放在前面，就会出现你说的“上面的规则先拦截了后面的规则”。

最稳妥的做法是：

1. 只保留“国家单独规则集”
2. 把这些 `RULE-SET` 放在主配置靠前位置
3. 不要再导入 `WiFiCalling-All.list`

## 仓库结构说明

- `outputs/surge_wifi_calling_country_carriers/`：当前推荐版本，按国家拆分
- `outputs/surge_wifi_calling/`：旧版，偏地理分流思路
- `outputs/surge_wifi_calling_rules.conf`：早期草案
- `outputs/surface_wifi_calling_rules.md`：需求说明文档
- `outputs/surge_wifi_calling_country_carriers/rulesets/WiFiCalling-*.list`：国家级规则集

## eSIM / SM-DP+ 地址

部分国家级规则集还收录了公开可核实的 carrier eSIM / SM-DP+ 地址，例如 Verizon、Bell、EE、Truphone、Metro by T-Mobile、Ultra Mobile。

这类规则仍然按国家和运营商归类，目的是让 Surge 在处理 eSIM 下载、eSIM 切换和 Wi-Fi Calling 相关流量时，尽量命中对应国家的节点组，而不是落到全局兜底。

## iPhone 接管要求

主配置的 `[General]` 需要同时启用：

```ini
include-all-networks = true
include-cellular-services = true
```

这两个参数只在 Surge iOS 生效。Wi-Fi Calling 通常依赖 UDP 500/4500，因此目标策略也必须真正支持 UDP Relay；仅有域名规则并不能保证注册或通话成功。

## 真实 IP 模式

使用 `always-real-ip = *` 时，IKEv2/IPsec 的 UDP 500/4500 没有 TLS SNI 或 HTTP Host，`extended-matching` 无法单独恢复域名。各国家文件因此包含两条受协议、目标端口和目标国家共同限制的 GEOIP 兜底；引用国家规则集时应同时使用：

```ini
RULE-SET,<国家规则 URL>,<国家策略>,no-resolve,extended-matching
```

`no-resolve` 避免规则匹配阶段额外触发 DNS，`extended-matching` 继续覆盖 HTTPS entitlement 等流量。GEOIP 兜底可能接管目标位于同一国家的其他 IKE/IPsec 流量；这是关闭 Fake IP 后的明确取舍，后续可用实测 ePDG IP-CIDR 进一步收窄。

## 数据来源与校验

- 3GPP 域名按 MCC/MNC 三位补零生成，同时覆盖 IMS 与公开 ePDG 后缀。
- MCC/MNC 候选来自公开数据库，建议结合各国监管机构或运营商资料复核。
- 运营商专用规则优先采用官方支持文档；美国规则中的 AT&T FQDN 与 T-Mobile 网段即来自各自官方 Wi-Fi Calling 文档。
- 对公开 3GPP ePDG 名称做 CNAME 校验，并保留已观察到的 Vodafone、KDDI、AT&T 与 Liberty Latin America 窄范围 ePDG 目标。
- 更新后应校验各国家文件、`WiFiCalling-All.list` 和 `SUMMARY.txt` 一致，并用 Surge 配置检查器验证完整配置。

## 导入顺序建议

建议顺序是：

1. 系统/本地网络放行规则
2. Wi-Fi Calling 国家规则
3. 你自己的通用代理/分流规则
4. 兜底 `FINAL`

这样国家级规则不会被更粗的规则盖掉。
