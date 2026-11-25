Wi-Fi Calling V5 增量维护

运行环境为 Python 3.10+，仅使用标准库。继续使用当前真实规则生成器，先让待发布
的国家表与 All 进入隔离工作目录下的 outputs/surge_wifi_calling_country_carriers/v5/rulesets/。
若生成器只能写原路径，在独立的生成临时目录运行，再将 19 个国家表和 All 复制进
隔离工作目录的 V5 目标。保留 V5 的公共模板和已分离的 GeoIP 目录。线上旧路径保留给旧客户端。

从公共规则仓库根目录执行：

  python3 maintenance/wifi_rules/postprocess_wifi.py --repo . --apply
  python3 maintenance/wifi_rules/postprocess_wifi.py --repo . --check

只有两步成功才继续原有发布流程。只收录 V5 子目录下的文件。
workflow_step.yml 提供可接在真实生成任务后的两步。仅复制此片段不会创建调度。

工具保留新生成的有效规则，补 13 个已核验精确域和 54 个国家 MCC 后缀，并把原有
38 条 GeoIP 归到独立表。`All` 与摘要按各国正式规则生成。国家表通过校验后，
空的 `All` 可以重建。`All` 中无法归属任何国家表的规则会在写入前报错。
缺失输入、丢失已验收覆盖、已知之外的 GeoIP、未知国家或有歧义的模板同样会报错。
每国正式规则至少保留已验收基准集合的 90%，且逐条保留登记的固定网段。
默认基准来自 `--repo` 下的 `maintenance/country_registry.json`。独立生成目录
使用 `--baseline` 明确指定已验收基准。模板引用保持全部精确规则在前，兼容表随后。

默认模式输出预览且只读。--check 退出码 0 表示后处理就绪，1 表示有待改字节，2 表示
输入无效或无法读取。--apply 预先计算全部变动，然后逐文件原子替换。

本包初始快照已通过 --check，无需先执行 --apply。SHA256SUMS 是交付快照的校验清单。
后续生成结果使用维护工具自己的校验。

官方 ITU-T E.212B 上游同步与工作流：

  python3 maintenance/wifi_rules/sync_itu_e212.py --repo . --apply
  python3 maintenance/wifi_rules/postprocess_wifi.py --repo . --apply
  python3 maintenance/wifi_rules/postprocess_wifi.py --repo . --check

`sync_itu_e212.py` 直接从 `https://www.itu.int/pub/T-SP-E.212B` 发现并解析最新版英文
ITU-T E.212B 附件（`.docx`），仅对已批准的 19 国 MCC 执行单调增量追加（不删除既有基线规则、
台湾 MCC 466、37 条运营商专用域名或 2 条固定 IP），并写入无时间戳的
`outputs/surge_wifi_calling_country_carriers/v5/itu_e212_lock.json`（相同上游下再次运行零 diff）。
注：19 国规则内已包含 54 条国家级 `mccXXX.(pub.)3gppnetwork.org` 后缀，已预先覆盖新运营商 MNC；
37 条非 3GPP 运营商专用端点无全球统一自动 API，仍随运营商文档或 Apple Carrier Bundle 维护。

GitHub Actions 工作流 `.github/workflows/update-wifi-calling-v5.yml` 每周一自动运行校验、
ITU 同步与后处理，且仅提交 `outputs/surge_wifi_calling_country_carriers/v5/` 变更。

回归测试：

  python3 maintenance/wifi_rules/test_postprocess_wifi.py --fixture .
  python3 -m unittest discover -s maintenance/wifi_rules -p 'test_sync_itu_e212.py' -v

测试在临时目录工作，覆盖重复生成、V5 文件范围、与上一次规则基线比较、ITU 解析与单调合并、
以及异常输入阻断。
