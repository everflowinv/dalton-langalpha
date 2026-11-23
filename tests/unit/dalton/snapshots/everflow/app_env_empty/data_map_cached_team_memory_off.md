## Dalton 数据地图
### 1. 直接工具与对话归档
- `WebSearch`(全网新闻|未体检):公开媒体报道、突发新闻与监管动态，查盘前盘后催化与公开事件。
- `market_quotes`(行情与K线|未体检|`get_quote/get_daily_prices/get_market_overview`):美/港/A股及指数报价与日K；期货/大宗(NQ=F、ES=F、CL=F、GC=F)、^TNX与VIX(延迟15分)；extended_hours=True含美股盘外分时。
- `company_options_screener`(公司与期权选股|未体检|`get_company_overview/get_options_chain/screen_stocks`):公司基本面快照、美股期权链及选股，用于公司画像与期权波动。
- `sec_filings`(SEC XBRL与报表|未体检|`get_sec_filing`):美股报表原文直接查 `get_sec_filing`；归一化XBRL财务事实(含重述)经 `ExecuteCode` 调用 `tools.dalton_sec.get_company_facts`。

### 2. 研究库与金融/宏观/社区路由（经 `ExecuteCode` 调用 `researchdata` 的 `research`、`finance`、`macro` 或 `community`）
- `all`[research/跨库初筛|未体检]:四库+卖方/专家联搜，不确定资料在哪时初筛。(四库+AE+GP,不含transcript)
- `alphaengine`[research/卖方研报|可用]:文档、机构/个人会议、录制状态、词典。(search_library后get_document)
- `guidepoint`[research/专家访谈|可用]:供应链/渠道/竞品专家访谈，查产业景气与格局。(匿名引职务,原话≤20词)
- `transcript`[research/业绩会转写|未体检]:财报电话会与回访，清单定位后另读正文，核对管理层原话、指引与Q&A。(all不含,须单调)
- `company-wiki`[research/公司资料库|未体检]:已入库公司笔记与行业沉淀，调取标的背景。(只读;params.company=TICKER)
- `sales-notes`[research/销售邮件|未体检]:交易台反馈、bogey/whisper与早报，查催化与资金。(raw独立源/digest线索)
- `tmtb`[research/TMT总结|未体检]:TMT收盘总结与盘前盘后复盘，查科技股涨跌原因。(转述与原件计同源)
- `funda-ai`[research/FUNDA AI|未体检]:FUNDA AI拆解与高频图，查AI算力与科技跟踪。(先search再read)
- `sec`[finance/美股SEC|可用]:美股SEC公告原文与报表，核实法定披露与附注。(10-K/10-Q/8-K/20-F/6-K)
- `estimates`[finance/一致预期|未体检]:营收/EPS等一致预期快照，用于Beat/Miss对比。(operation=consensus)
- `cn-hk`[finance/港A股财务公告|未体检]:`filings` 覆盖 A 股和港股的法定公告（巨潮转载），港股约有 5% 的缺口，主要是仅英文版公告和股东大会投票结果，这类需要原文时，用 `WebSearch` 查 `site:hkexnews.hk`；港 A 股财务、估值、资金面（`ask`，公司和日期写进问题）；`quote` 仅作 `get_quote` 的备用报价。(operation=ask|filings|quote)
- `fred`[macro/FRED宏观|未体检]:美国通胀/就业/GDP/利率序列，查宏观与折现率。(series+start+end必填)
- `treasury`[macro/TGA与联邦债务|未体检]:财政部TGA余额与联邦债务，查流动性；收益率走fred。(operation=treasury, dataset=tga|debt)
- `market-regime`[macro/市场状态与拥挤度|未体检]:大盘过热/超卖、顶部底部阶段、拥挤度、两融杠杆；回答大盘和仓位问题时先读(operation=snapshot|us_topbottom|crowding|cn_leverage)
- `xueqiu`[community/雪球讨论|未体检]:港A股与中概散户及KOL观点。(posts/post/comments/search/user,pages≤3)
- `employee`[community/员工评论|未体检]:员工侧情绪与组织变化。(blind/summary免费;付费须allow_paid=True)
- `last30days`[community/跨平台社区情绪|未体检]:跨平台与StockTwits/Polymarket情绪。(query+days≤30)
- `x-timeline`[community/X固定账号时间线|未体检]:固定账号建不漏底表；跨账号观点与媒体补充用x_search。(tweets/tweet/thread/search,count≤100;与x_search按原帖URL去重)
- `gcal`[research/Richard 财报日历|未体检]:只读写 Richard 财报跟踪日历上的 tracker 事件。日历 ID 来自宿主机配置。(不并入 all;日历 ID 不由调用方传入;删除前核对 tracker 标记;写操作不用于健康探测)

### 3. 明确没有的数据（切勿以为查过）
- 期货与大宗商品实时(非延迟)行情:yfinance期货延迟约15分钟，注明延迟。
- 暗池/期权异动流:未接通(13F见dalton-13f-tracker;期权链用get_options_chain)。
- 高频另类数据(信用卡/SensorTower/卫星/海关):未接直连，仅从alphaengine/funda-ai/guidepoint查二手引述。