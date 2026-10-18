## {TICKER} {窗口}涨跌分析
事件结论：{五词}——{一句话：对哪个支柱/debate 有什么含义；市场定价对还是错}

### 价量数据
- 收盘价：{close} 美元（未复权）｜区间涨跌：{change_pct}%（复权）｜成交量：{last_day_volume} 股（20 日 ADTV 的 {volume_vs_adtv} 倍）
- 逐日明细：
  - {YYYY-MM-DD}：收盘 {close} 美元｜日涨跌 {change_pct}%｜同组中位数 {peer_median_pct}%｜日超额 {excess_pct}pp（日 z={day_z}）｜量比 {volume_vs_adtv}x

### 同组对比
- 同组中位数：{peer_median}%（成员：{peer_1} {p1_pct}%、{peer_2} {p2_pct}%、……）
- 板块 ETF：{sector_etf} {etf_change}%
- 超额收益：对同组 {alpha_vs_peers}pp（窗口 z={window_z}，60 日日超额 σ={sigma_60d_pct}%）｜对板块 ETF {alpha_vs_sector}pp
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
- **对 Thesis / Debate 的含义**：{对照 thesis.md 支柱与 debate.md，说明本次变动是否改变某条支柱状态或 debate 倾向，市场定价是过度反应、反应不足还是合理定价}
- **下一项验证**：{具体观察指标或事件}（预期验证时间：{YYYY-MM-DD 或窗口}）
