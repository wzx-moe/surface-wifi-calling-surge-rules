# Wi‑Fi Calling V5 for Surge

V5 独立于旧版地址，旧配置继续使用原来的规则。使用 V5 时，在现有配置中参考 [合并片段](merge_snippet.conf)，将全部 19 个国家精确表置于 19 个 GeoIP 兼容表之前。配置的策略组需要可用的节点。`main.conf` 是与本目录相配的模板，随本目录使用并配置代理成员。

- [规则表](rulesets/) 包含国家域名和已核验的固定网段。启用各国表后无需再引用 `WiFiCalling-All.list`。
- [兼容表](legacy_geoip/) 用于仅携带目的 IP 的 UDP 500/4500。GeoIP 判断的是目的地国家，同样符合条件的其他 IKE 流量也会命中。
- 在 iOS 上启用 `include-all-networks` 与 `include-cellular-services`，并验证实际出口支持 IKE 所需的 UDP 转发。运营商注册和通话以设备实测为准。

参考 [Surge Rule Set](https://manual.nssurge.com/rules/ruleset.html)。
