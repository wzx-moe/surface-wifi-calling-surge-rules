# Surge AI 规则维护

核查日期：2026-11-25。三个服务的主要上游是 [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat) 的 `meta` 发布分支；此目录负责确定性格式转换和明确记录的必要补充。没有继续依赖此前小众 Claude 仓库。与节点订阅、节点名称、WARP、DNS 配置无关。

## 文件与发布地址

| 服务 | 原始分类 | 本次生成条数 | 发布路径 |
|---|---|---:|---|
| GPT / OpenAI | `geo/geosite/classical/openai.list` | 70 | `outputs/surge_ai/OpenAI.list` |
| Claude | `geo/geosite/classical/anthropic.list` | 25 | `outputs/surge_ai/Claude.list` |
| Antigravity / Google AI | `geo/geosite/classical/google-gemini.list` | 52 | `outputs/surge_ai/Antigravity.list` |

上传到现有 `wzx-moe/surface-wifi-calling-surge-rules` 仓库的 `main` 分支后，对应 URL 是：

```
https://raw.githubusercontent.com/wzx-moe/surface-wifi-calling-surge-rules/main/outputs/surge_ai/OpenAI.list
https://raw.githubusercontent.com/wzx-moe/surface-wifi-calling-surge-rules/main/outputs/surge_ai/Claude.list
https://raw.githubusercontent.com/wzx-moe/surface-wifi-calling-surge-rules/main/outputs/surge_ai/Antigravity.list
```

这些是本次包的部署目标；准备文件不等于已经向 GitHub 发布。替换主配置前，应先上传公共规则包并确认这三个地址返回规则正文。请保留仓库原有文件，只合并本次包中提供的文件；不要上传个人 Surge `.conf`、节点订阅或任何私密凭据。

## 为什么需要转换

MetaCubeX 提供的是 mihomo / sing-box 规则数据，目前没有原生 Surge 发布分支。普通 geosite 列表的 `+.domain` 不是 Surge `DOMAIN-SET` 格式；classical OpenAI 列表还含有 Surge 不支持的 `DOMAIN-REGEX`。因此不能把原始 URL 直接写进 Surge 的 `RULE-SET`。

适配器只输出 `DOMAIN`、`DOMAIN-SUFFIX`、明确保留的 `DOMAIN-KEYWORD,openai`，以及一条经过审核的原生 `DOMAIN-WILDCARD`。三个 AI 表只负责域名，不包含节点、策略、IP、ASN、端口或进程规则。其他职责由主配置明确承担。

当前唯一正则转换为：

```
上游：^chatgpt-async-webps-prod-\S+-\d+\.webpubsub\.azure\.com$
Surge：DOMAIN-WILDCARD,chatgpt-async-webps-prod-?*-[0-9]*.webpubsub.azure.com
```

Surge 官方文档支持 `*`、`?` 和 `[0-9]` 这样的字符类。此转换保留上游合法主机名的覆盖，但有一个明确的放宽：数字尾段在第一位数字之后还允许其他字符。它仍被限定在 `chatgpt-async-webps-prod-` 前缀和 `.webpubsub.azure.com` 后缀内；不等同于原正则，也没有把整个 Azure 域名空间纳入代理。新正则必须审核后才能发布。这里不使用 URL 正则或 HTTPS 解密。

## 必要补充与兼容性

`supplements.json` 记录补充规则、来源和已知范围；`requirements.json` 记录必须持续满足的覆盖基线。

- OpenAI：按本次核查的官方网络要求检查 32 个覆盖案例，保留旧配置的 31 条 DOMAIN / DOMAIN-SUFFIX 覆盖和原 `openai` 关键词。旧版共享 `auth0.com`、`sentry.io`、`stripe.com` 等较宽范围也被明确保留，避免静默缩减既有覆盖。
- Claude：检查 19 个官方功能域名案例，补充 `anthropic.auth0.com` 和原来的 `cdn.usefathom.com`，以及 CDN / 站点依赖。Claude 规则应放在 OpenAI 之前，避免旧 OpenAI 的 `auth0.com` 父域接走 Claude 登录。
- Antigravity：使用持续维护的 Google AI 分类，补充手机网页入口 `antigravity.google.com` 和明确属于 AI 的接口。`accounts.google.com` 和 `oauth2.googleapis.com` 归原通用 Google 分流。macOS 的 Antigravity 进程优先级由主配置提供。

ChatGPT 语音单独沿用 [Sukka 的 AI IP 数据](https://ruleset.skk.moe/List/ip/ai.conf)，由主配置限定 `UDP + 3478`，不混入三个域名文件。本次取到的 OpenAI 官方语音清单为 411 个前缀，Sukka 的 409 个合并 CIDR 与其地址并集一致。Sukka 额外含 Anthropic 的 AS399358，因此主配置在语音规则之前把该 ASN 分配给 Claude。更新后的网段仍由 Sukka 维护，本适配器不改写它。

## 自动更新与失败保留

合并时务必包括 `.github/workflows/update-surge-ai.yml`。在仓库 Actions 中启用 workflow，并允许 `GITHUB_TOKEN` 写入仓库内容；分支保护如禁止此类提交，需要在仓库设置中采用符合现有管理规则的发布方式。

Workflow 每天 UTC 22:13（北京时间次日 06:13）运行，并提供 `Run workflow` 手动入口。GitHub 的定时任务可能延迟；公共仓库长期无活动也可能被平台暂停定时任务，需要关注 Actions 状态。源码目录变更会触发一次运行。`push.paths` 只限制源码变更事件；每日和手动运行都会抓取 MetaCubeX **当前 meta 发布分支** 的三个公开 URL。

本任务仅使用 Python 标准库，runner 自带的 `python3` 即可执行，不安装 pip / Node 依赖。checkout 使用官方 v7.0.1 对应的完整提交 SHA。工作流先运行契约测试，再抓取、转换、验证全部服务，最后只暂存 `outputs/surge_ai/`，以一个普通 Git 提交发布，不强制推送。与其他更新任务同时提交造成普通推送冲突时，任务失败，已公开的上一版保持不变；下次运行会从新的仓库 HEAD 重试。

以下情况会拒绝更新：

- 公共 HTTPS 请求失败、重定向、响应过大、非 UTF-8、返回 HTML 错误页；
- 未审核的语法、正则、类型、额外策略字段或新增过宽共享云父域；
- 上游精确 `DOMAIN` 条目中的 `accounts.google.com` 与 `oauth2.googleapis.com` 已按现有分流归属过滤。覆盖这些域名的后缀或通配规则仍会阻止更新。
- 上游条数低于固定基线、一次降至上一版的 70% 以下，或增至 3 倍以上；
- 对上一版上游域名范围的语义覆盖低于 70%，即使新旧条数相等；
- 上游核心服务域名消失，或合并后官方 / 旧配置覆盖基线不满足。

三份上游内容全部通过检查后才修改输出。网络或验证失败不会改动旧输出；每个本地文件通过同文件系统的临时文件原子替换，普通替换异常会尝试回滚已改文件。整个公开文件集在一个 Git 提交中发布。不要把本地多文件替换理解为断电、磁盘损坏等所有情况下的事务保证。

`outputs/surge_ai/source-lock.json` 保存公开源 URL、完整内容 SHA256、适配器 / 配置 SHA256、转换记录和上次内容变化时间。`outputs/surge_ai/upstream/` 保存对应原始正文。未取到 GitHub API 的完整提交信息，因此 `commit_sha` 为 `null`，通过原始快照与内容哈希进行复现，不编造版本号。内容完全相同时不会生成无意义的每日提交。

## 本地维护命令

从仓库根目录运行：

```sh
python3 -m unittest discover -s maintenance/ai_source -p test_update_rules.py -v
python3 maintenance/ai_source/update_rules.py
python3 maintenance/ai_source/update_rules.py --verify-current
```

只验证最新公开源，不写文件：

```sh
python3 maintenance/ai_source/update_rules.py --check-only
```

使用随包快照离线复现：

```sh
python3 maintenance/ai_source/update_rules.py --source-dir maintenance/ai_source/fixtures
python3 maintenance/ai_source/update_rules.py --verify-current
```

基线是人工核对官方文档之后的可审计数据，不是自动理解未来官方文档的机制。新增功能 / 协议、上游分类变化以及被主动拒绝的更新，仍需维护者审核。静态契约测试不能代替用户当前 Surge 版本的导入测试、真实登录、语音或节点可达性测试。

## 来源认可度与授权

本次官方 GitHub 页面显示 MetaCubeX/meta-rules-dat 约 5.7k stars / 823 forks，SukkaW/Surge 约 4.7k stars / 333 forks，blackmatrix7/ios_rule_script 约 28.8k stars / 4.1k forks。它们均有实际社区引用，但 star / fork 不是活跃用户数或满意度评分。我们优先选择 MetaCubeX 的完整服务分类和近期持续构建；Blackmatrix 的部分 AI 独立列表在本次取到的正文中仍标注 2025-06-06，Claude 仅 3 条，单靠这个列表不能满足本次覆盖。详细核查见 `repository_review.json`。

MetaCubeX 的数据仓库采用 [GPL-3.0](https://github.com/MetaCubeX/meta-rules-dat/blob/master/LICENSE)；本包保留来源和 GPL 全文于 `licenses/MetaCubeX-GPL-3.0.txt`，并提供转换源码、配置与上游快照。引用的 Sukka 语音列表由其仓库按 AGPL-3.0 发布，本包只保留外部链接，不复制或重新发布语音列表。

技术依据：

- https://manual.nssurge.com/rules/domain.html
- https://manual.nssurge.com/rules/ruleset.html
- https://help.openai.com/en/articles/9247338-network-recommendations-for-chatgpt-errors-on-web-and-apps
- https://openai.com/chatgpt-voice.json
- https://code.claude.com/docs/en/network-config
- https://code.claude.com/docs/en/desktop
- https://developers.google.com/identity/protocols/oauth2/web-server
- https://github.com/MetaCubeX/meta-rules-dat/actions
- https://github.com/MetaCubeX/meta-rules-dat/releases
- https://github.com/actions/checkout/releases/tag/v7.0.1
- https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax

V5 同时按主配置表顺序 Claude → OpenAI → Antigravity 检查服务要求的域名归属。已有共享和宽后缀需要通过首条命中测试，特别是 Claude 的 `anthropic.auth0.com`。
