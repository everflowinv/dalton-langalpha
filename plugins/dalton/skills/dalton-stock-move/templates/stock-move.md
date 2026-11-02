## {TICKER} {窗口}涨跌分析
事件结论：{五词}——{一句话：对哪个支柱/debate 有什么含义；市场定价对还是错}

### 价量数据
- 收盘价：{close} 美元（未复权）｜区间涨跌：{change_pct}%（复权）｜成交量：{last_day_volume} 股（20 日 ADTV 的 {volume_vs_adtv} 倍）
- 逐日明细：
  - {YYYY-MM-DD}：收盘 {close} 美元｜日涨跌 {change_pct}%｜{基准标签} {benchmark_pct}%｜日超额 {excess_pct}pp（日 z={day_z}）｜量比 {volume_vs_adtv}x
  - （`{基准标签}` 按 `benchmark` 填写：`benchmark="peers"` 时写“同组中位数”；`benchmark="sector_etf"` 时写“板块 ETF（{sector_etf}）”）

### 同组与基准对比
- 同组中位数：{peer_median}%（成员：{peer_1} {p1_pct}%、{peer_2} {p2_pct}%、……）（若 `benchmark="sector_etf"`，即 `peer_median` 为 `null`，本行改写为：“同组中位数：无同组公司数据，仅与板块 ETF 对比”）
- 板块 ETF：{sector_etf} {etf_change}%
- 超额收益（按 `benchmark` 二选一）：
  - `benchmark="peers"` 时写：对同组 {alpha_vs_peers}pp（窗口 z={window_z}，60 日日超额 σ={sigma_60d_pct}%）｜对板块 ETF {alpha_vs_sector}pp
  - `benchmark="sector_etf"` 时写：对板块 ETF（{sector_etf}）{alpha_vs_sector}pp（窗口 z={window_z}，60 日日超额 σ={sigma_60d_pct}%）
  - （注：`benchmark="sector_etf"` 时 σ、z 都按板块 ETF 计算，不能写成“对同组超额”）
- 量化判断：{个股 alpha (stock_specific) / 板块 beta (sector_beta) / 两者都有 (mixed)}

### Filing / 公告
- {日期}｜{Form 类型及 Item 号}｜{核心内容摘要}（accession: `{accession}`，[原文链接]({url})）
- （若窗口内无公告则写：“窗口内无新 filing”）

### 新闻催化剂
1. {标题} — {来源媒体}｜{YYYY-MM-DD}｜[原文链接]({url})（{一手报道 / 二手转载}）
   - 摘要：{核心事实与带期间的数字}

### 卖方观点
- {机构}（{YYYY-MM-DD}，`{doc_id}`）：{研报/点评标题}——{评级/目标价变化及卖方说法摘要}
- （或明确区分写：“窗口内无新研报” / “AlphaEngine Desktop 未登录” / “工具调用失败：{原因}”）

### 社交媒体
- X @{handle}（{作者身份}，{YYYY-MM-DD}，赞 {likes}/转 {retweets}）：{有实质内容的基本面观点或散户情绪方向}（[原帖链接]({url})）
- （若无实质讨论则写：“讨论量不大”）

### 综合归因
- **主要驱动与分类**：{个股事件 / 板块共振 / 宏观 / 资金面 / 原因暂缺}
- **催化剂排序**：1. {主驱动}；2. {次驱动}
- **竞争解释判定**：对第 0 步冻结的 2–3 个竞争解释逐条判定——{解释 1：成立 / 排除 / 存疑（还差什么证据）}；{解释 2：…}
- **对 Thesis / Debate 的含义**：{对照 thesis.md 支柱与 debate.md，说明本次变动是否改变某条支柱状态或 debate 倾向，市场定价是过度反应、反应不足还是合理定价}
- **收尾三问**：经营变了吗？{是 / 否，依据}｜预期变了吗？{是 / 否，当前定价水平}｜赔率变了吗？{变好 / 变差 / 没变——基准是上一份带日期、锚价和期限的报告里的赔率卡；尚无历史赔率卡时写"本轮建立基准"，不得声称已经比较出了变化}
- **线性还是切换**：{沿旧叙事线性演进；或叙事 / 倍数 regime（市场给这类资产的定价规则）可能在切换——若是，点名触发器、方向（升级 / 降级）与确认证据}
- **下一项验证**：{具体观察指标或事件}（预期验证时间：{YYYY-MM-DD 或窗口}）
