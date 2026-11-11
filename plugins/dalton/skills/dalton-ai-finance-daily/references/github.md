# GitHub AI 项目动量

GitHub 材料是 `ai_finance_read` 的 `github` 资源。`version` 必须是 3。星数、门槛、降温和聚类都已经写在这份输出里；正文使用这些字段，不另行改判，也不另加静默天数或“比上次多 50% 才再报”。

官方数字来自 `https://api.github.com/repos/{owner}/{repo}/stargazers/history`。`momentum.daily_gain` 是 `complete_through` 那一日的新增星，`momentum.gain_7d` 是截至该日的 7 日合计。GitHub 的日期桶与 UTC 不一定对齐，采集已留 12 小时完整性缓冲；未结束的日期不参与降温。这些数字写成该官方完整日，不写成实时过去 24 小时。`daily_velocity_3d` 是最近 3 个完整日的平均新增，`prior_daily_velocity_3d` 是此前 3 日平均，`velocity_change_pct` 比较这两段，衡量的是新增速度。

只有 `history_verified` 为 true 且 `report_eligible` 为 true 的仓库可以作为当日热点。`stars_gained_daily` 和 Trendshift 的周期增量只用于发现。第三方榜单增量不写入正文数字。

## 1. 输出里要核对的名单

读取 `coverage`、`data_gaps`、`candidates`、`continuing_hotspots`、`below_significance`、`cooled`、`upstream_clusters`、`report_limit` 和 `ranking_policy`。

`data_gaps` 是单仓库核验缺口。缺口写成来源说明里的缺口，不写成降温。`cooled` 里 `momentum.phase` 为 `cooling` 的已报仓库，才是本轮确认降温。同一天再次准备不会把同一次回落再数一遍。

`candidates` 含全部仍有资格的持续热点，以及最多 20 个新候选。名额见 `report_limit`，值为 8。

## 2. 门槛与阶段，供理解字段

采集已经按下面的规则写入 `significance` 和 `momentum`。`significance.meaningful` 与 `report_eligible` 是入选结果。

初次门槛：完整日新增至少 1,000，或 7 日新增至少 5,000 且最近 3 日平均至少 300。同时还要过绝对量与相对体量：日增至少 1,000 且占当前总 stars 至少 1%，或 7 日新增至少 5,000 且占当前总 stars 至少 5%。

已报项目的续报门槛：日增至少 300 且占当前总 stars 至少 0.5%，或 7 日新增至少 2,000 且占当前总 stars 至少 3%。重新爆发使用初次门槛。低于门槛的仓库留在 `below_significance`，不称作曲线降温。

相对量是官方完整期间增量除以当前已核验总 stars，称作增量占比。两个时点不同，不写成精确的期初涨幅。`breakout_score` 同时看绝对量和相对体量；增速对分数的调整最多 20%，低增量不靠高百分比进入。这些百分比、增速、评分和阈值只留在研究记录里。

`momentum.phase` 的含义：`new_breakout` 初次入选，`accelerating` 加速，`sustained` 持续，`slowing_unconfirmed` 回落待确认并仍保留资格，`cooling` 最近 3 日平均比此前 3 日平均低过 20% 且连续两个完整日如此，停止常规跟报，`reaccelerating` 降温后重新达到爆发门槛且速度至少提高 10%。判断按日期，不按本轮执行次数。

`continuing_hotspots` 与首次候选合并排序。排进前 8 的持续热点照常写入栏目，包括昨日已经报过、已经离开日榜，以及本轮没有产品新闻的项目。名额外的合格项目 `displayed` 为 false，次日重新比较；因名额未展示不算降温。

## 3. README 与每条 decision

`candidates` 里的每一个仓库都有一条 decision。首次候选逐个读取 GitHub 仓库页面和 README，确认真实用途。持续热点的产品未变时，沿用已经核实的用途和 TAM，`readme_url` 仍填该 README 的 https 地址。没有可沿用的核实记录，或产品有变化时，读取 README。

排除纯教程或书单、简单 prompt 合集、明显抄袭或无代码壳、与 AI 只有营销关系的项目，以及已归档项目。扩散本身构成重要生态信号时可以保留，并在 `reason` 写明这个例外。

`github_decisions` 的形状是 `{"records":[...]}`。每条只使用这些字段：

- `repo`：`owner/repo`，与 `candidates[].full_name` 一致。
- `decision`：`include` 或 `exclude`。`include` 表示通过相关性复核，且该仓库 `report_eligible` 为 true。
- `reason`：复核理由。
- `readme_url`：README 的 https 地址。
- `displayed`：布尔值。true 表示进入当日展示名单。
- `upstream`：README 写明的模型、SDK、API 或运行时，用小写标识。README 没写清楚时填 `未标明`。
- `upstream_evidence`：出处；未标明时写明 README 未写上游。

`include` 与 `displayed` 分开。通过复核但排不进前 8 的仓库是 `include` 且 `displayed` 为 false。每个 `candidates` 条目恰好一条，不写输出里没有的仓库。

展示名单只从 `decision` 为 `include` 的仓库里产生。排序键依次是 `significance.breakout_score`、`momentum.daily_velocity_3d`、`momentum.gain_7d`，都是降序。不分新旧配额，不随机抽取，不用低质量项目凑满 8 条。展示合计 0 到 8 条。

## 4. 上游

每个候选的上游写进 decision 的 `upstream`。进入 signal record 时，`tags` 含同一条 `upstream:` 加这个标识，例如 `upstream:jev`、`upstream:未标明`，`evidence` 写同一出处。单个仓库可能只是壳；多个仓库同时包住的上游才是投资标的。

`upstream_clusters` 是过去 48 小时内、至少两个不同 owner 的 trending repo 共用同一个非通用 token 的线索。token 本身不直接写进日报。用 README 或上游官方页面核实后，才放进 `#### 跨日共性与新趋势`。48 小时内至少两个不同 owner 的 trending repo 共用一个此前没有独立播报过的上游，且 README 或上游官方页面已经独立确认时，在对应 signal 的 `signal_types` 加上 `upstream_primitive_adoption`。趋势小节的写法见 `signals.md`。这只增加趋势分析，不增加热点展示名额，也不把名额外仓库另列成清单。

确认不了的聚类不写入趋势。来源说明写“cluster X 未能确认上游”。X 用该聚类在 `upstream_clusters` 里的 token。

## 5. 栏目里的每一条

展示 0 到 8 条，允许只有 1 到 2 条。没有新项目也没有持续热点时，写“今日没有达到阈值的新项目或持续热点”。全部项目共用一张编号名单，编号从 1 开始。不追加持续跟踪、候补或其他项目清单。

每条严格 3 行：

```markdown
1. **[owner/repo](https://github.com/owner/repo)**（总 stars X；日增 +Y）— 一句话说明用途。
TAM：它直接所属的现有市场或支出池。
直接相关：直接竞品、替代品或配套服务，以及最直接相关的上市公司（公司名 + ticker）。
```

X 是 `github.stars`，Y 是 `momentum.daily_gain`。可以使用千分位逗号。TAM 只写直接支出池，定性即可；没有可靠来源时不写市场规模数字。没有高置信度上市公司时写“暂无直接上市公司映射”。不展开传播原因、买方画像、货币化链条或泛泛的受益承压，也不扩大到泛 AI 产业链。

每个项目最多 220 个字符，链接 URL 不计。GitHub 全栏不超过 3,000 字符。栏目里只出现当前总 stars 和完整日新增。百分比、7 日增量、3 日均速、斜率、评分、排名、语言和 velocity bucket 留在 decision、signal 的 `metrics` 或 `evidence` 里。Trendshift 只在来源说明交代一次，项目下不附它的链接。

只有最终展示、并且有明确投资含义的仓库才写入 signal record。纯动量续报留在观察池和播报历史。signal 的日期、键和后台指标见 `signals.md`。
