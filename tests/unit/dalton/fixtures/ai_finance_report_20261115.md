## 📊 AI + 金融投资日报

### 今日要闻

1. **安纳智芯披露模拟计算芯片 AX100 测试结果** — 据 IT之家转引科技日报，公司称 AX100 已于 10 月完成流片，单次矩阵方程求解低于 100 纳秒、功耗约 80 毫瓦，拟用于大模型训练和具身智能；这些测试针对特定计算任务，量产、应用适配及整体训练收益仍待验证。（[IT之家](https://www.ithome.com/1/027/430.htm)）
2. **中科曙光披露 scaleFabric 已支持 3 万卡集群运行** — 据 IT之家报道，中科曙光在中国超级算力大会表示，自研高速互连方案已在国家超算互联网郑州核心节点支持 3 套万卡级集群在线运行；这一披露支持其系统部署能力，集群实际利用率和客户工作负载表现仍待核实。（[IT之家](https://www.ithome.com/1/027/320.htm)）
3. **要求黄仁勋回归英伟达的公开请愿超过 13.5 万签名** — Benzinga 报道，Stand with Jensen 网站在美东时间 11 月 15 日凌晨列示 135,468 个签名，呼吁董事会保留黄仁勋回归选项；签署者身份属网站自报，董事会后续决定仍待正式披露。（[Benzinga](https://www.benzinga.com/markets/tech/26/11/63276610/nvidia-jensen-huang-reinstate-ceo-petition-elon-musk-investors)）
4. **意大利因 Facebook 相册访问问题对 Meta 罚款 8.05 亿欧元** — 据美联社报道，意大利数据保护机构认定 Facebook 安卓应用相册建议功能缺乏独立明确同意，要求限期整改；监管部门另保留对云端相册处理功能的评估，后续需跟踪 Meta 上诉与整改结果。（[Los Angeles Times／AP](https://www.latimes.com/world-nation/story/2026-11-14/meta-fined-887-million-in-italy-over-android-facebook-images)）

### GitHub AI 项目动量

1. **[GoogleChrome/modern-web-guidance](https://github.com/GoogleChrome/modern-web-guidance)**（总 stars 16,539；日增 +4,147）— Chrome、Edge、Firefox、MDN 联合维护的编码 agent 离线检索技能与 CLI。
TAM：编码 agent 用的 Web 平台文档。
直接相关：MDN。上市公司 Alphabet (GOOGL)、Microsoft (MSFT)。

2. **[GoogleChrome/modern-web-guidance-src](https://github.com/GoogleChrome/modern-web-guidance-src)**（总 stars 6,552；日增 +1,224）— 同一 Web 平台指南的源码、Playwright 评分器与多 agent 校准评测仓库。
TAM：编码 agent 用的 Web 平台文档。
直接相关：MDN。上市公司 Alphabet (GOOGL)、Microsoft (MSFT)。

3. **[Lee-xeo/Claude-Cage](https://github.com/Lee-xeo/Claude-Cage)**（总 stars 5,610；日增 +248）— Go 编写的轻量化隔离虚拟机桌面工作台，托管 Claude Code 与 Claude Desktop 并接管代理网络。
TAM：编码 agent 的本地隔离与多账号环境。
直接相关：agent-vm、E2B、Modal。暂无直接上市公司映射。

4. **[Ebony-Vinyl/dsh-our-free-model](https://github.com/Ebony-Vinyl/dsh-our-free-model)**（总 stars 75,241；日增 +1,777）— DeepSeek Harness 插件，通过公共免费网关免登录接入 DeepSeek V4.1 Flash、Kimi K3 并提供本地兼容转发。
TAM：编码 agent 模型免费聚合通道与本地转发网关。
直接相关：OpenRouter、LiteLLM。暂无直接上市公司映射。

5. **[h4ckf0r0day/obscura](https://github.com/h4ckf0r0day/obscura)**（总 stars 35,534；日增 +356）— Rust 无头浏览器，兼容 CDP/Playwright/Puppeteer 与 MCP。
TAM：AI agent 浏览器运行时。
直接相关：Kitesurf、Browser Run、Playwright。上市公司 Cloudflare (NET)。

6. **[morluto/rea](https://github.com/morluto/rea)**（总 stars 460,870；日增 +2,314）— 面向编码 agent 的本地逆向工程 MCP 与 CLI，可在无源码条件下分析二进制、Electron 与运行时行为。
TAM：AI 辅助逆向工程与代码安全分析工具。
直接相关：Ghidra MCP、IDA Pro MCP。暂无直接上市公司映射。

7. **[yetone/magpie](https://github.com/yetone/magpie)**（总 stars 36,746；日增 +413）— Go 编写的本地多 agent 模型路由网关，统一切换编码 agent 配置并提供协议转换、脱敏与账号故障转移。
TAM：编码 agent 本地模型网关与订阅路由工具。
直接相关：LiteLLM、OpenRouter、CLIProxyAPI。暂无直接上市公司映射。

8. **[stablyai/orca](https://github.com/stablyai/orca)**（总 stars 125,374；日增 +740）— 桌面、移动端与远程多编码 agent 并行编排工作台，以独立 git worktree 复用自有订阅。
TAM：多编码 agent 并行编排与开发环境。
直接相关：Cursor、Windsurf、Conductor。暂无直接上市公司映射。

### Earnings Calls：AI 的直接业务影响

本窗口没有找到可验证的 AI 直接影响披露。检索命中的 Patanjali Foods 业绩会全文获取失败，收入、成本及相关 Q&A 暂待核实。

#### 跨日共性与新趋势

**浏览器执行层的产品证据增加。** 作为历史类比，本轮 Obscura 与 moli（10 月 5 日）、browser-use（9 月 9 日）的记录相似，都在为智能体提供网页操作或浏览器运行环境。Obscura 自身在 9 月 1 日已有跟踪记录，本轮新增了官方完整日增星核验及竞品商业路径的补证。([Obscura](https://github.com/h4ckf0r0day/obscura)) ([moli](https://github.com/lexmount/moli)) ([browser-use](https://github.com/browser-use/browser-use))

Cloudflare 11 月 13 日公布的 Kitesurf 开源说明进一步明确了商业路径：自行部署需要 Workers 付费套餐，项目仍在持续开发。其 8 月文章曾说明 Obscura 提供初始灵感；当前架构与商业产品应分别看待。潜在机会在托管浏览器和云开发平台用量，最直接关联 Cloudflare（NET）；下一步看付费部署、Browser Run 使用量及 Obscura Cloud 正式上线。([Cloudflare 开源说明](https://blog.cloudflare.com/kitesurf-open-source/)) ([历史技术说明](https://blog.cloudflare.com/kitesurf/))

Claude、Codex、DeepSeek 与 DeepSeek Harness 的多项目兼容关系延续既有生态。本轮尚无开源动量向企业收入或降本传导的新证据。

### 来源说明

_由 Eve 自动生成；新闻与业绩会为发送前 24 小时，GitHub 总 stars 为抓取时点，日增为 2026-11-14 的官方完整日期桶_

新闻搜索成功 14/14 条 query。事实窗口为 2026-11-14 20:00:05 至 2026-11-15 20:00:05（香港时间）；采用 4 条，均已核对原文具体发布时间，并与近 7 日已播报事件去重。其余主要为旧闻、观点或缺少可核验时间的线索。美联储官员 AI 通胀讲话的网页核验因抓取额度耗尽未完成，未纳入要闻。

GitHub：日/周/月榜与持久观察池发现，官方增星历史核验 96/100 个仓库；本轮展示 8/8 个，其中首次 2 个、持续 6 个。核验缺口 4 个仓库（页面未找到）：NotRexed/Hookshot、HunYiSage/dsh-purge、YuJunZhiXue/dsh-purge、worldonlyu/Google_AI_Pro_2Year_ZeroCost_Auto；待核验 7 个，待复核新候选 0 个；12 个候选已全部复核，名额外观察 0 个。发现榜单来源失败：无。Trendshift 仅用于发现，正文数字均为官方增星历史。

GitHub 状态：本轮确认降温的已报项目为 meta/astryx、heydamir/unslop、expo/exact、debpalash/VoiceStudio、storytold/craft-anything、deepseek-ai/dsh-superagent-prototype、vercel-labs/agent-desktop、NVIDIA/auto-ontology、milind-soni/OpenMausBot、google/elephant-goldfish-model、cloudflare/security-audit-skill、anomalyco/rune、JuneYaooo/rea-video、patch-gg/pixelfx、FluentRead/FluentRead、HKUDS/DeepTutor、amosblomqvist/learnharness、anomalyco/hex。48 小时上游聚类检出 12 组，已确认 4 组，未确认 8 组。

业绩会：AlphaEngine Earnings Calls（type_id: IND_SECOND_5FA442AE），过去 24 小时无市场/行业/市值限制的 AI 关键词检索；命中 1 篇，按相关性排序后全文扫描 0 篇，80篇预算跳过 0 篇，失败 1 篇；AI candidates 0 篇中 signal 0 / watchlist 0 / reject 0。13 条检索均成功，失败发生在全文获取阶段。

历史共性：AI signal memory（post-ingest 2166/2166 signals 已用 gemini-embedding-2 向量化；历史检索模式 embedding），当前累计 signals 2166 条。本轮历史匹配均早于近 30 天的跨源传导窗口，按历史类比使用。“首次”沿用当前播报池分类，Obscura 在 9 月 1 日已有历史记录；两个 GoogleChrome 仓库按同一实体看待。
