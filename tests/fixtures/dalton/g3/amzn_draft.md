# AMZN 跟踪底稿

这份底稿是跟踪任务判断"新不新、重不重要"的基准。每轮读完新材料后在原位更新数字和倾向，在末尾「变更记录」追加一行。观点池只记已知说法，新材料和这里重复的不算更新。

- 建立：2026-10-15 13:05 EDT（Lumos 在 Discord DM 要求，底稿来自同日 Amazon 多空与盈利预测修正研究；研究包 `temp/stock-tracker/AMZN/research-20261015-1250/excerpts.md`，窗口 9/1 至 10/15，内部库 115 篇）
- 最近更新：2026-10-15 22:15 EDT（run 20261015-2215；TMTB 传 Yipit 跟踪 Q3 AWS 约 44%，越过 42% 看多阈值；一致预期与上轮相同）
- 口径：日历年财年。Yahoo 的 0y = FY2026、+1y = FY2027。一致 EPS 是 GAAP：FY2026 含上半年其他收入净额 690.6 亿美元（税前），其中私有股权向上调整 628 亿、可转债转换重分类收益 45 亿，主要来自 Anthropic；Q3、Q4 和 FY2027 的一致预期基本不含投资收益。以上拆分本轮经 7/31 披露的 Q2 10-Q 核对。Q3 财报日 Yahoo 和 MarketBeat 列 10/29 盘后，公司公告日期待确认。

## 核心判断

- 10/29 Q3 财报偏正面，置信度中低。决定股价的是 AWS 增速：卖方模型约 37%（TD Cowen 10/5 称其 41.2% 比一致预期高 3%，倒推一致预期约 452 亿美元），买方至少 40%（Jefferies 9/30）。M-sci 10/9 称三家大云 Q3 比卖方高 2 至 3 个点、AWS B300 价格大涨，AWS 预留 GPU 价格年内第四次上调（Wells 9/30），Anthropic Q3 收入 168 亿美元、环比 +46%（10/15 报道），这些都指向 40% 至 42%。10/15 TMTB 传 Yipit 跟踪 Q3 AWS 约 44%（卖方 37%），另一 X 账号同日称 Yipit 数字高于一致预期、季内逐月加速；第三方跟踪首次越过 42% 看多阈值，同时把买方 bogey 往 42% 以上推，财报"超预期"的门槛随之抬高。股价比 3 个月高点低 10.8%，Jefferies 9/23 听到"战术上偏空"、9/30 称亚马逊是近几周情绪恶化最多的大票，仓位比 MSFT 干净。
- 风险在零售和 2027 capex。M-sci 10/7 称北美和国际固定汇率增速持平到低于预期、份额偏弱；Prime Day 挪到 Q2，Q3 总收入指引只有 +9% 至 12%（剔除后高约 4 个点）。TD Cowen 估 2027 年 capex 约 3,400 亿美元，比一致预期高 22%（倒推一致预期约 2,780 亿），电话会如果把 2027 capex 说到 3,000 亿以上，自由现金流和发债会再被拿出来讨论。
- FY2027 EPS 未来 3 到 6 个月：经营利润上修 3% 至 5%（AWS 利润和零售自动化），EPS 上修 0 至 3%，折旧和利息吃掉一部分。TD Cowen 10/5 上调 AWS 的同时把 FY27 EPS 从 11.39 下调到 11.13，就是这个机制。
- 账面 EPS 会被 Anthropic 打乱：FY2026 一致 EPS 12.90 里约 5 美元来自上半年其他收入（21% 税率粗算，主要由 Anthropic 投资相关收益贡献），剔除后 FY2026 核心 EPS 约 8 美元，FY2027 一致 10.54 美元相当于核心增长约三成。Anthropic 若 11 月上市且定价高于 Q2 入账价，Q4 还会再记一笔以百亿美元计的重估收益。这部分从修正判断里剔除。
- 转多条件：Q3 AWS 不低于 42%、Q4 指引隐含继续加速；北美利润率剔除一次性项目不低于 8%；管理层给出 Trainium 外销时间表。
- 转空条件：Q3 AWS 低于 39%；2027 capex 说到 3,200 亿以上且没有对应的 backlog 或定价说明；北美增速剔除 Prime Day 后低于 10% 且广告减速；Anthropic 上市推迟或定价低于 Q2 入账价。

## Debates

### D1 AWS 增速能不能继续加速
- 分歧：Q3 AWS 是 37% 左右（卖方模型），还是 40% 以上（买方 bogey）；Q4 和 2027 年产能能不能撑住加速。
- 事实：Q2 AWS 422.3 亿美元、+36.7%，连续第五个季度加速，环比增量 46 亿美元；年化 1,690 亿美元；backlog 4,960 亿美元（三位数增长，加权剩余期限 6.4 年）；芯片业务、AI 业务年化各超过 250 亿美元。Jassy 7/30 称 2026、2027 年产能都满足不了需求，2027 年产能大部分已被预订，2028 年需求"惊人"，2027 年底电力容量比 2025 年翻倍。Q3'25 AWS 基数 330.1 亿、Q4'25 355.7 亿。
- 看多：TD Cowen 10/5 Q3 AWS +41.2%、AI 收入约 101 亿美元（+540%）、非 AI 365 亿（+16%），目标价 350；Wells 9/30 GPU 预留价 10/7 再涨 15%、年内 H100/H200/B200/B300 累计涨 40% 至 75%，加速持续到 2027 年初，目标价 338；Evercore 10/8 把亚马逊列为大盘股首选、目标价 355；GS 10/1 加入确信买入名单、目标价 375；Truist 9/11 称 Q2 后 AWS 新签 18 笔合作；Garman（a16z 播客，TMTB 10/8）称 2026 capex 约 2,200 亿、"短期不会放缓"，只能满足约 60% 的初创公司 GPU 需求，Trainium 产能售罄到明年底；Yipit（TMTB 10/15 "hearing"）Q3 AWS 跟踪约 44%、季内逐月加速。
- 看空：Bernstein 9/15 称 AWS 网络指标到 9 月初仍偏软，Q3 非 AI 只加速约 0.5 个点，Q4 非 AI 还有约 1 个点的下行风险；M-sci 9/24 称 9 月中旬前 AWS、Azure 产能同比增速放缓；Truist 9/11 预计 Q3 AWS 在 30% 中段。
- 当前倾向：Q3 偏多（中高，10/15 由中上调），AWS 41% 至 44%。上调原因：Yipit 第三方跟踪约 44%，和 M-sci 10/9、TD Cowen 41.2% 同向。代价是买方 bogey 抬到 42% 以上，40% 至 41% 的结果在股价上会被当成持平。
- 改判：Q3 AWS 实际值对 42%（买方新门槛），以及 Q4 指引隐含的 AWS 增速。

### D2 capex、折旧与自由现金流
- 分歧：2027 capex 落在一致预期约 2,780 亿还是 TD Cowen 的约 3,400 亿；折旧和利息会吃掉多少 AWS 利润增量；靠什么融资。
- 事实：2026 年现金 capex 指引约 2,200 亿（7/30 从约 2,000 亿上调，原因是内存涨价）；Q2 现金 capex 531 亿；过去 12 个月自由现金流 -76 亿；Q2 折旧摊销 200 亿（+31%），过去 12 个月 752 亿；上半年长期债发行 670 亿，9/14 再发 42.5 亿英镑（5.200% 至 6.650%）；Q2 利息支出 13.1 亿（上年同期 5.2 亿）。服务器回本不到 3 年、使用寿命 5 至 6 年（Jassy 7/30）。
- 看多：Jassy 称 AI 的利润率和回报跟踪核心云同期、略快；AI 产能多签 5 年合同。
- 看空：TD Cowen 10/5 估 2027 capex 约 3,370 亿（AWS 3,050 亿，2026 年 1,870 亿），折旧摊销 2026 年 859 亿→2027 年 1,178 亿，2026 至 2028 年无杠杆自由现金流 -302 亿、-747 亿、-226 亿；BofA 信用电话会（10/15 wiki）把亚马逊的信用可信度排在 Google、微软之后；FT（TMTB 10/2）称亚马逊在谈约 80 亿美元英伟达 GPU 售后回租；Business Insider（TMTB 10/1）称 2027 至 2028 年末端配送建筑支出 176 亿，比原计划多 70 亿。
- 当前倾向：偏空（中）。2027 capex 一致预期大概率上调，修正压力落在 2027 年下半年和 2028 年 EPS。
- 改判：Q3 电话会对 2027 capex 的说法；1 月 Q4 财报的正式框架。

### D3 前沿实验室集中度与 Trainium
- 分歧：backlog 里 OpenAI、Anthropic 两家占多少、质量如何；Trainium 能不能变成独立的利润来源。
- 事实：10-Q 披露 Q1 OpenAI 合同从 380 亿扩大 1,000 亿（8 年），Q2 Anthropic 合同扩大超过 1,000 亿（10 年），都含 AWS 芯片性能条款；Q2 亚马逊投 Anthropic Series G、Series H 各 50 亿美元，另给 Anthropic 最高 200 亿美元的融资额度（按算力交付里程碑开放，上市后可换普通股）。Anthropic S-1（路透 9/29）列出对亚马逊最低支出 1,100 亿美元；Anthropic 47% 的销售经云伙伴（亚马逊、Google）完成。
- 看多：Anthropic Q3 收入 168 亿美元、环比 +46%、首次调整后盈利（10/15 财联社，经东北证券转述）；Garman 称单客户收入集中度个位数百分比；Trainium 驱动 Bedrock 大部分推理流量；Jassy 称"很有可能"把 Trainium 卖给云外客户；高通协议最高 600 亿美元（到 2036 年，RBC/JPM 9/9）。
- 看空：两家实验室的承诺合计超过 2,400 亿美元，约占 4,960 亿 backlog 的一半（粗算，部分已确认收入）；OpenAI 算力负责人 10/14 离职、ARR 净额口径从约 700 亿重设到约 500 亿（TMTB 10/8）；Jefferies（TMTB 9/29）称亚马逊在定制芯片上多供应商、压价，限制博通/Marvell 份额，也说明 Trainium 生态仍在建。
- 两个口径要分开：Garman 说的是当前收入（单家实验室个位数百分比），上面的粗算是未来合同（backlog 约一半）。集中度风险在 2027 年以后的收入确认里。
- 当前倾向：中性偏多（中）。Anthropic 增长和上市让集中度短期是加分项。
- 改判：Anthropic 招股书定价和上市后对 AWS 的支出指引；OpenAI 融资是否落空。

### D4 零售利润率
- 分歧：北美利润率能否从 Q2 的 7.9% 继续扩张；Q3 份额是否转弱。
- 事实：Q2 北美收入 1,161.8 亿（+16%）、经营利润 91.2 亿（7.9%，含约 6 亿关税退款）；国际 421.97 亿（+15% 固定汇率）、利润率 4.1%；2026 年机械臂规模翻倍（Olsavsky 7/30）；Q3'25 北美经营利润只有 47.9 亿，含 FTC 25 亿和解与遣散费 [训练数据，未实时验证]，Q3 同比会显得很高。
- 看多：Wolfe 9/18 上调 FY26 经营利润 3.5%（比一致预期高 6.8%，FY27 高 3.4%），自动化每年省 20 亿、老仓改造再省 10 亿；BofA 9/17 Project Mercury 投 68 亿把当日达网点扩大 10 倍、覆盖 80% Prime 会员 10 英里内；Truist 9/11 北美核心收入季内符合到略超；Edge（TMTB 9/10）称收入走向指引上沿、零售更好。
- 看空：M-sci（TMTB 10/7）称北美和国际固定汇率增速持平到低于预期、份额偏弱；Wolfe 10/7 千人调查里亚马逊"打算多花"比例 38%、在电商平台里最低，"打算少花"20% 最高；TD Cowen 估 Q3 北美收入 +7.1%、北美电商约 +5%（Prime Day 错位）；燃油和干线运费上涨（Olsavsky 7/30）。
- 当前倾向：中性偏多（中）。利润率扩张靠自动化，增速在 Q3 有份额噪音。
- 改判：Q3 北美利润率剔除一次性项目；Q4 指引里的零售口径。

### D5 广告与代理式购物
- 分歧：代理式购物会不会绕开 Sponsored Products；亚马逊封堵 Muse 是护城河还是让份额流向 Walmart、Shopify。
- 事实：Q2 广告收入 198.1 亿（+26%）；Alexa for Shopping 过去 12 个月 3.5 亿用户、Q2 活跃用户近翻倍（7/30 电话会）；亚马逊 9/20 封堵 Meta Muse 在站内下单；亚马逊接入 Google UCP，库存在 AI Mode 里展示，付款仍跳回亚马逊（10/10 Google 搜索专家）。
- 看多：DB 10/5 广告检查称亚马逊 Q3 比计划高约 1 个点，尽管 Sponsored Products 减速；Susquehanna 10/6 unBoxed 24 家伙伴检查称代理式购物比担心的慢，目标价 325；代理商负责人 Lee（wiki 10/8）称客户正在把预算从 The Trade Desk 快速转到 Amazon DSP。
- 看空：TMTB 9/28 实测 ChatGPT 代理在亚马逊 58 次购物里首次点击全部落在自然结果，广告占首位的 57 次一次没点；Lee 称三家里亚马逊 CPC 业务在 2027 年风险最大；Jefferies 10/12 广告专家同样认为亚马逊风险最大；Stratechery（TMTB 10/1）称亚马逊最终可能要向大代理开放；大摩（wiki 10/8）称假日季 Muse 若靠 Walmart 等跑通，谈判筹码会向 Meta 倾斜。
- 当前倾向：近 6 个月偏多（中），2027 年起偏空（低）。现在的收入数据还没受影响，风险在明年。
- 改判：Q3 广告增速和 Sponsored Products 口径；亚马逊与 Meta 或 OpenAI 是否签代理接入协议；假日季代理下单数据。

### D6 Anthropic 投资收益与 GAAP 口径
- 分歧：市场按 GAAP EPS 还是核心 EPS 看修正；Anthropic 上市对亚马逊账面和 AWS 的影响。
- 事实：Q2 其他收入 534.2 亿，其中私有股权向上调整 505 亿，主要来自 Anthropic 无投票权优先股；上半年 628 亿，另有可转债转换重分类收益 45 亿；Q1 2026 其他收入 156.5 亿。Anthropic 据报道 11/9 前后开始路演、感恩节前定价（多家媒体，未核到一手）。CNBC Kate Rooney（经 TMTB 10/15 播客摘要）称 Anthropic 向投资人披露 Q3 收入接近 170 亿美元、环比 +45%（年初为 +140%）、调整后利润率 80% 以上，上市估值被放风在 2 万亿美元左右；DA Davidson 认为市场反应偏负面（预期太高）。按 BofA 15% 至 21% 持股粗算，2 万亿估值对应持仓 3,000 亿至 4,200 亿美元，高于 6/30 账面合计 1,904 亿（可转债部分的增值走 OCI，只有优先股部分进损益）。
- 账面持仓核实（10/15 读取 Q2 10-Q，2026-07-31 披露，accession 0001018724-26-000026）：6/30 Anthropic 无投票权优先股账面 925 亿美元、可转债公允价值 979 亿，合计 1,904 亿；可转债相关未实现收益 920 亿记入累计其他综合收益（OCI）。这是期末持仓账面值；本期损益按上面的损益表拆分。持股比例目前采用 BofA 9/7 的 15% 至 21% 估计区间，精确比例继续核实。
- 当前倾向：口径项，不判多空。上市定价越高，Q4 GAAP EPS 越高，同时对 AWS backlog 的信心越强。
- 改判：Anthropic 招股书定价区间和实际定价。

### D7 监管、并购与其他投入
- 事实：据 PYMNTS/CPI 10/11 引述 Law360 和 MLex，FTC 反垄断案法官 10/9 驳回延期至 2027 年 6 月的申请，沿用 3/29/2027 开庭安排（10/15 已读报道原文，法院文件待核）；Globalstar 116.8 亿美元收购预计 2027 年完成；WSJ 10/12 称在谈约 70 亿美元收购 Decart（帮客户在芯片间迁移负载）；Amazon Leo 近 400 颗卫星，年内开始初始服务。
- 当前倾向：3 到 6 个月内影响小。

## Earnings revision 模型

- 一致预期基线（Yahoo，2026-10-15 22:15 EDT 读取，与 13:08 相同；Q3/Q4 EPS 近 30 天上调 2 家）：Q3 EPS 1.96（1.72 至 2.23，45 家）、收入 2,021 亿（指引 1,970 亿至 2,020 亿，一致预期贴着上沿）；Q4 EPS 2.44、收入 2,443 亿；FY2026 EPS 12.90、收入 8,286 亿；FY2027 EPS 10.54（8.73 至 15.04）、收入 9,493 亿。FY2027 EPS 90 天前 10.00、30 天前 10.50，近 7 天上调 1 家、下调 0 家，近 30 天上调 5 家、下调 0 家。平均目标价 331.46、中位 329.5（230 至 405）；强买 16、买入 42、持有 2。
- 隐含经营利润（20% 税率、109.5 亿股粗算）：FY2026 约 1,100 亿（上半年 513 亿 + Q3 约 260 亿 + Q4 约 330 亿），FY2027 约 1,430 亿，增长约三成。按 AWS 2027 年 +40%、利润率 37% 算，AWS 贡献约 240 亿增量，其余约 80 亿靠零售和广告，零售利润率约从 6.7% 升到 7.5%。
- 敏感度（FY2027，粗算）：零售利润率每 1 个点 ≈ EPS 0.51 美元（约 5%）；AWS 利润率每 1 个点 ≈ 0.18；AWS 增速每 1 个点 ≈ 0.05；若 H2 2026 AWS 比模型高 4 个点并延续到 2027 年 ≈ +0.24（约 2%）；2027 capex 每多 100 亿 ≈ 2027 年 -0.06、2028 年 -0.13（服务器 6 年折旧、年中投产）；每多 500 亿债券（5% 利率）≈ -0.18；Anthropic 每 100 亿税前重估收益 ≈ +0.73（只影响 GAAP）。
- 结论：EPS 杠杆在零售利润率和折旧，估值杠杆在 AWS 增速。
- 卖方锚点：JPM 10/15 上调 2027/2028 经营利润 3% 至 5%、GAAP EPS 2% 至 3%（AWS 2027 年 +40%），和本底稿"经营利润上修多、EPS 上修少"的判断一致；TD Cowen 10/5 FY26/FY27 GAAP EPS 13.31/11.13（FY27 从 11.39 下调）、收入 8,338 亿/9,921 亿、2027 capex 约 3,370 亿；Wolfe 9/18 FY26/FY27 经营利润比一致预期高 6.8%/3.4%。

## KPI 与阈值

- 行情快照（Yahoo，10/15 收盘）：254.76 美元，单日 +0.27%（QQQ -0.13%）、5 日 +0.28%（QQQ -2.62%），距 3 个月高点 -10.3%；本轮自动阈值触发项为空。其余经营 KPI 沿用最近披露值。
- Q3 AWS 增速：卖方约 37%、买方至少 40%、TD Cowen 41.2%、Yipit 跟踪约 44%（TMTB 10/15 传闻口径）；42% 以上转多信号，39% 以下转空信号。第三方跟踪已越过 42%，实际值的检验线改为 42%。
- Q3 AWS 利润率：一致预期 37.5%（TD Cowen 引用）、TD Cowen 39.6%、Q2 39.4%。
- Q3 经营利润：指引 225 亿至 265 亿；卖方约 260 亿（由 TD Cowen 276 亿高 6% 倒推）。
- Q4 指引：收入对一致预期 2,443 亿；经营利润对约 330 亿（粗算）。
- 2027 capex：一致预期约 2,780 亿（倒推）、TD Cowen 约 3,400 亿；3,200 亿以上为看空阈值。
- 北美利润率：Q2 7.9%（含关税退款）；8% 以上为看多阈值。
- 广告增速：Q2 +26%；低于 22% 视为代理式购物开始显现。
- backlog：4,960 亿；Q3 环比增量。
- AWS GPU 定价：第 5 次上调或 Trainium2 首次调价算新信号。

## 催化剂日历

- 10/20 13:00 ET：Jefferies 亚马逊物流专家电话会（MWPVL 的 Marc Wulfraat）。
- 10/29 盘后（Yahoo、MarketBeat，待公司确认）：Q3 财报。重点按重要性：AWS 增速与利润率、Q4 收入和经营利润指引、2027 capex 说法、北美利润率与份额、广告增速和 Sponsored Products、Trainium 外销、Anthropic 重估收益。
- 10 月下旬至 11 月：Decart 收购是否落地（约 70 亿美元）；亚马逊可能继续发债或做 GPU 售后回租。
- 11/9 前后路演、感恩节前定价（据报道）：Anthropic 上市。
- 11 月下旬至 12 月：假日季，黑五网一数据；Muse 封堵僵局是否破局。
- 12 月初：re:Invent（Caylent CTO 10 月初称"约两个月后"），看 Trainium 3/4、Trainium 外销、Bedrock。
- 年底前：Amazon Leo 初始服务。
- 1 月底至 2 月初：Q4 财报，2027 capex 正式数字（往年 Q4 电话会给数：2026-02 给 2026 约 2,000 亿；见「指引节奏」）。
- 3/29/2027：FTC 反垄断案开庭（据 PYMNTS/CPI 10/11 报道，10/15 已读原文；日期沿用基线，法院文件待核）。

## 指引节奏（往年 Q3 与本次预期，10/15 核对原文）
- 往年：Q3 给当年 capex 数字加一句明年方向，只给 Q4 收入和经营利润区间，从不给下一年费用或利润率框架。2023-10 全年约 500 亿（2022 年 590 亿），没讲 2024；2024-10 约 750 亿，Jassy "I suspect we'll spend more on that in 2025"；2025-10 约 1,250 亿，"we expect that amount will increase in 2026"，次日 +9.6%；2026-02 给 2026 约 2,000 亿，次日 -5.6%。
- 今年：7/30 把 2026 上调到约 2,200 亿（内存涨价）；Jassy 称 2027 产能大部分已被预订、2028 也有不少，没讲 2027 capex。
- 本次预期：2026 约 2,200 亿维持或因内存小幅上调；"2027 会增加"，大概率不给数；Q4 收入和经营利润区间是 EPS 关键（一致经营利润约 330 亿，粗算）。"会增加"同时兼容一致约 2,780 亿（+26%）和 TD Cowen 3,400 亿（+55%）；用 "significantly" 或给增幅则偏 TD Cowen 一侧。

## 专家来源可靠性

- Caylent CTO Randall（wiki 10/8，AWS 顶级咨询伙伴、Anthropic 伙伴；Jefferies The Download）：Bedrock 是他见过增长最快的 AWS 服务；OpenAI 5 月上 Bedrock 后客户把负载从 Azure 迁回 AWS；Opus 输出速度在 Bedrock 最快。利益相关（AWS 伙伴），方向可信、数字没有。
- 广告代理商负责人 Lee（wiki 10/8，150 人代理商）：亚马逊 CPC 2027 年风险最大；Amazon DSP 从 The Trade Desk 拿份额。和 Jefferies 10/12 广告专家方向一致。
- Horizon Media Adam Gerber（wiki 10/8）：自述客户不在亚马逊卖货，对亚马逊的判断是同行转述，权重低。
- 大摩 TMT 与消费团队电话会（wiki 10/8，Nowak、Gutman 等）：卖方观点，归入观点池。
- 本窗口 wiki 没有亚马逊在职或前员工的专家访谈。

## 已知观点池（重复出现不算更新）

- JPM Anmuth 10/15：AWS 2027 年增速上调到 +40%（"可能仍偏保守"，驱动是 OpenAI 合作放量、2027 至 2028 年新增 200 万块 GPU、定价权），最大客户只占个位数百分比；2027/2028 收入 +1% 至 2%、经营利润 +3% 至 5%、GAAP EPS +2% 至 3%，计入 42.5 亿英镑新债；目标价 365（2028 年 GAAP EPS 13.34 的 27.5 倍）。
- TD Cowen 10/5：Q3 收入 2,022 亿、AWS +41.2%、经营利润 276 亿（高于一致预期 6%）、Q4 收入/经营利润高于一致预期 2.3%/3.0%、2027 capex 约 3,400 亿、FY27 EPS 11.39→11.13、目标价 350。
- Wells 9/2：AWS 2026 至 2028 年经营利润上调 4% 至 6%，核心 AWS 利润率首次超 40%，Bedrock 2028 年收入 350 亿，目标价 328→338；9/30：GPU 预留价 10/7 +15%，连续第四个季度。
- Truist 9/11：北美核心收入季内符合到略超，AWS 新签 18 笔，backlog +154%，Q3 AWS 30% 中段，目标价 350。
- GS 10/1：加入确信买入名单，目标价 375（AWS、物理 AI 撑利润率）。
- Evercore 10/8：大盘股首选第一，AWS +37%、利润率约 39%、backlog 约 5,000 亿，目标价 355。
- Susquehanna 10/6：unBoxed 检查正面，代理式购物渐进，目标价 325。
- Wolfe 9/18：FY26 经营利润 +3.5%，自动化每年省 20 亿至 30 亿；10/7：消费者调查亚马逊多花意愿最低，中性偏正面。
- BofA Justin Post 9/14：重申买入（AI 产能多年需求、自研芯片）；9/17：Project Mercury 当日达扩 10 倍、投 68 亿。BofA 信用电话会 10/15：信用可信度 Google > 微软 > 亚马逊 > Meta > Oracle。
- Bernstein 9/15：AWS 网络指标偏软，Q4 非 AI 有约 1 个点下行风险。
- DB 10/5：亚马逊广告 Q3 比计划高约 1 个点，Sponsored Products 减速。
- M-sci：9/10 大云高于预期 2 至 3 个点（Edge 称收入走向指引上沿）；9/24 产能增速 9 月中旬放缓；10/7 北美/国际持平到低于预期、份额偏弱；10/9 大云高 2 至 3 个点、AWS B300 价格大涨。
- Jefferies：9/23 战术偏空；9/30 亚马逊情绪恶化最多，多数人预期 AWS 下季至少 40%；10/8 AWS、Azure 预期"在合理位置"；10/12 广告专家称亚马逊 CPC 风险最大；10/15 列为推荐（AWS 加速、零售稳、估值打折）。
- TMTB 9/28：ChatGPT 代理 58 次购物 0 次点广告。Stratechery（TMTB 10/1）：亚马逊护广告封堵 Muse，长期可能要开放。
- 大摩 TMT/消费电话会（wiki 10/8）：亚马逊与 Meta 在谈 merchant of record、Prime 接入、零售媒体；Meta 的 Watermelon API 需要 Bedrock；假日季是筹码检验。
- Garman a16z 播客（TMTB 10/8）：capex 约 2,200 亿不放缓，200 万块英伟达 GPU，初创需求只满足约 60%，Trainium 售罄到明年底，单客户集中度个位数。
- SemiAnalysis Patel（TMTB 10/7）：亚马逊 AI 基础设施已盈利，Bedrock 拉高毛利。
- FT（TMTB 10/2）：约 80 亿美元 GPU 售后回租在谈。
- WSJ 10/12：约 70 亿美元收购 Decart 在谈。
- X @PrefShares（基本面投资人）10/10，赞 331：2027 年零售（北美+国际）EBIT 一致预期约 560 亿（单一来源），零售被 Alexa、供应链服务、生鲜、Leo、国际拖累、利润在低位；不同意 borrowed_ideas 对 AWS AI capex 只赚资本成本的判断。支持 D4 看多、D2 看多。
- X @borrowed_ideas（股票分析师，发布 AWS 模型）10/10，赞 87：AWS 基准估值约 1.3 万亿、AI capex 回报约等于资本成本；零售利润引擎是广告，受代理式购物、FTC 案、竞价机制三重风险。支持 D2 看空、D5 看空。
- X @RihardJarc（科技研究）10/9，赞 555：转述 Garman，AWS 偏推理、对单一前沿实验室的收入敞口个位数百分比。支持 D3 看多。
- X @kashyap286（宏观通讯作者）10/2，赞 654：亚马逊 GPU 按 6 年折旧、实际 3 年内贬值，用 SPV 和私募信贷藏资产。D2 看空，论据偏情绪，权重低。
- Wells 10/12：目标价 338→329，维持 Overweight；Stocktwits 经 TradingView 的正文与 MarketScreener/MT Newswires 标题交叉确认，降幅约 2.7%。据 Stocktwits 引 The Fly，仍预计 Q3 超一致预期、Q4 AWS 加速，2026 capex 维持 2,200 亿美元。与 D1 看多方向一致；本轮读到的材料仅列目标价与定性预期。来源：https://www.tradingview.com/news/stocktwits:34c56c9f4094b:0-amazon-analysts-slash-price-targets-ahead-of-q3-wells-fargo-sees-aws-acceleration-to-continue-in-q4/ ；https://www.marketscreener.com/news/wells-fargo-adjusts-amazon-com-price-target-to-329-from-338-ce785dddd988f620 。
- Cantor Fitzgerald 10/12：目标价 320→310，维持 Overweight，降幅约 3.1%；预期云业务加速、EBIT 利润率较强。Stocktwits 引 The Fly 与 MarketBeat 引 Benzinga 交叉确认。支持 D1 看多，观点方向沿用基线。来源：https://www.marketbeat.com/instant-alerts/analyst-amazoncom-nasdaq-amzn-stock-price-target-cut-by-cantor-fitzgerald-2026-10-12/ 及上条 TradingView 原文。
- PYMNTS/CPI 10/11：引述 Law360、MLex，法官 10/9 驳回 Amazon 延期申请，FTC 反垄断案沿用 2027/3/29 开庭；10/15 已核报道原文。D7 日历维持原安排。来源：https://www.pymnts.com/cpi-posts/amazon-loses-bid-to-delay-ftc-antitrust-trial-as-california-case-looms/ 。
- BofA Justin Post（卖方，9/7 销售邮件 mail:119110）：估亚马逊可能持有 Anthropic 15% 至 21%，上市带来估值参照和持仓流动性；10/15 复核原邮件摘段。D6 采用卖方估计区间；Q2 10-Q 独立核得 6/30 优先股与可转债合计账面 1,904 亿美元。
- X @RihardJarc（科技独立研究、UncoverAlpha）10/14，赞 237：融资趋紧可能先压慢 neocloud 扩张，给大型云厂商减轻短期 capex 与竞争压力，并支撑算力价格。支持 D2 看多，是定性情景推演，权重低；各 Debate 倾向保持原值。原帖：https://x.com/RihardJarc/status/2110421525471908178 。
- TMTB Morning Wrap 10/15（doc 190000041724172）："Hearing Yipit saying AWS tracked to a ~44% estimate vs. street at 37%"。X 蓝标账号 10/15 同称 Yipit AWS 高于一致预期、季内加速（https://x.com/i/status/2110714912125358210 ）。两条都是传闻口径。D1 看多；倾向上调到中高。
- CNBC Kate Rooney（经 TMTB 10/15 播客摘要 doc 190000041736546）：Anthropic Q3 收入接近 170 亿美元、环比 +45%，调整后利润率 80% 以上，感恩节前上市、估值放风约 2 万亿美元；DA Davidson 称反应偏负面；Gerstner 认为环比放缓部分来自主动限制蒸馏。D6 口径项（Q4 重估收益上限抬高），D3 看多。
- Jefferies Brent Thill 10/15 云厂商前瞻（销售邮件 mail:128373）：AWS "not yet done accelerating"、零售在消费情绪偏弱下持续超预期、估值打折；首选仍是 MSFT、GOOGL。与 10/15 已记的 Jefferies 推荐同源。
- X @RihardJarc 10/15，赞 162：英伟达把 SPCX 从 ACIE 重分类进 Hyperscale，剔除后四大云厂商对英伟达的采购占比低于账面口径。属于英伟达口径问题，与 AMZN Debate 关联弱，仅记录。

- TMTB 10/15 Morning Wrap（doc 190000041724172）：Yipit 跟踪 Q3 AWS 约 44%、卖方 37%。卖方邮件 10/15 无对应条目；X 一个蓝标账号（handle 未解析）10/15 12:50 UTC 同称"Yipit out with above cons numbers for AWS"、季内加速，方向一致、未给数字。已按两个传闻口径写入观点池；等 Yipit 原文或卖方引用确认 44% 这个数字。原帖：https://x.com/i/status/2110714912125358210 。已核 1 轮。
- 亚马逊持有 Anthropic 精确 21%（Gemini 摘要引 Fool/Blofin，10/15）：本轮读到 BofA 9/7 的 15% 至 21% 估计区间，精确比例待核；6/30 合计账面 1,904 亿美元已由 Q2 10-Q 核实并移入观点池。Blofin、TheStreet 原文均返回 403；10/15 晚内部库补搜仍只有区间。来源：https://blofin.com/academy/education/amznx/amazon-anthropic-stake-explained ；https://www.thestreet.com/technology/amazon-anthropic-ipo-stake-400-billion-concentration-risk 。已核 2 轮。
- 据 X @StockSavvyShay（FuturumEquities 首席市场策略师）10/14 转述 BofA，到 2030 年全球 AI 数据中心容量约 219 GW、AMZN 新增约 42 GW；10/15 晚 sales notes 补搜只找到 BofA 4 月"2030 年容量翻倍到约 200 GW"的旧说法，219 GW 和亚马逊 42 GW 仍缺原文。原帖：https://x.com/StockSavvyShay/status/2110345541322481973 。已核 1 轮。

## 排除来源

- 无。

## 数据备注

- 2026-10-15 研究：AlphaEngine 英文检索返回大量无关新文档，改用中文 query 加行业过滤后命中 TD Cowen、Wells、Truist；读了 Q2 电话会全文和 TD Cowen 前瞻全文。
- 2026-10-15 13:14 跟踪：内部库、AlphaEngine、SEC 新材料均为 0，excerpts 目录为空；新增 X 帖 10 条。AlphaEngine 全文读取 0 篇，补搜 Wells 无命中。Investing.com、Stocktwits 直链、Blofin、TheStreet 返回 403；券商动作改由 TradingView 正文、MarketBeat 正文及 MarketScreener/MT Newswires 公开标题核对，后者正文限会员。SEC search-text 首次按任务参数传 params 被入口拒绝，按当前 CLI 契约改用 query 后成功。行情时间为盘中，packet 的“收盘”标签按快照时间解读。X 的 George Koutalidis 长帖仅有截段，未据此调整 AMZN 判断；其他行业转述和泛论保留在 packet。核查回执：temp/stock-tracker/AMZN/20261015-1308/verification-sources.json。
- 2026-10-15 22:15 跟踪：内部库新文档 2 篇（均为 TMTB），AlphaEngine、SEC 新材料为 0，AlphaEngine 全文读取 0 篇；sales notes 读了 Jefferies 10/15 早报 mail:128373。Yipit 44% 只在 TMTB 和一个 X 账号出现，handle 未从 xreach 解析出来。X 检索 7 条：eBay/马斯克电商帖来自预测市场营销号，期权、个人买入帖跳过，未加账号池。

## 变更记录

- 2026-10-15 13:07 | 建立 | 来自同日多空研究，基线一致预期见「Earnings revision 模型」
- 2026-10-15 13:14 | material=no | 一致预期与 Debate 倾向保持；核实两家券商小幅降目标价、FTC 原定日期及 Anthropic 历史账面持仓，更新来源与待核线索。
- 2026-10-15 14:10 | material=no（手动）| 新增「指引节奏」：往年同季度下一年 capex/opex 说法（8-K 与电话会原文核对）和本次预期措辞。
- 2026-10-15 22:20 | material=yes | Yipit 跟踪 Q3 AWS 约 44% 越过 42% 看多阈值，D1 倾向上调到中高、买方门槛抬到 42%；Anthropic 估值放风 2 万亿美元，Q4 GAAP 重估收益上限抬高；一致预期未动。
