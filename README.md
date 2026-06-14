# Surface Wi-Fi Calling Surge Rules

这个仓库整理的是一套可直接给 Surge 5 使用的 Wi-Fi Calling / IMS 外部规则集，核心目标是：

- 每个国家单独一个规则集
- 每个国家可以绑定不同的节点组
- 避免把汇总规则和国家规则一起导入，造成重复命中

## 推荐使用方式

如果你已经有自己的主配置，不要直接导入完整 `main.conf`，而是把下面这两类内容接到你的主配置里：

- `[Proxy Group]` 里的国家组
- `[Rule]` 里的 `RULE-SET` 引用

Surge 是自上而下首命中优先，所以这些 Wi-Fi Calling 规则必须放在你自己的更宽泛规则之前，尤其是：

- 任何 `GEOIP`
- 任何泛化的 `DOMAIN-SUFFIX`
- 任何兜底 `FINAL`

如果两个国家的规则有重叠，排在更上面的国家会先命中；所以建议把你最常用、最优先的国家放前面。

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

## 导入顺序建议

建议顺序是：

1. 系统/本地网络放行规则
2. Wi-Fi Calling 国家规则
3. 你自己的通用代理/分流规则
4. 兜底 `FINAL`

这样国家级规则不会被更粗的规则盖掉。
