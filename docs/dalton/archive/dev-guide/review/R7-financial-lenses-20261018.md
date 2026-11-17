# R7（部分）财务镜头脚本 审查结果

审查范围：`plugins/dalton/skills/dalton-financial-lenses/`（scripts 与 references），对照基金原 skill `~/.openclaw/workspace/skills/findata-{netcash,hiddenasset,return}/`（只读）。
代码版本：`7670a03f`。日期：2026-10-18。R7 的其他部分（workflow、skill 内容、迁移脚本、覆盖名单）未审，按 `REVIEW-CHECKLIST.md` 另行安排。

## 结论
股东回报评分的移植忠实、测试充分；汇率锚点和净现金部分设计正确，但**按 SKILL.md 写的方式运行不安全**，需要先修下列问题。单元测试能通过，是因为测试总是显式传入币种和汇率，智能体实际会走的路径没有被测到。

测试：`tests/unit/dalton` 加 financial-lenses 脚本 75 项通过；full-coverage 脚本、`test_dalton_sec_periods.py` 等 141 项通过。

## 问题（按严重程度排序）

| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |
|---|---|---|---|---|---|
| 1 | 高 | `scripts/anchors.py:21-35, 127-128, 143-178` | 不传币种时汇率取错；离线或 yfinance 失败时用写死的汇率表，却标成当天的报价 | `--ticker BABA` 得到报表币种 USD、汇率 1.0，但 BABA 用人民币报告，资产负债表数字会大约放大 7 倍。`--ticker 0700.HK` 对财务报表用 HKDUSD=X 0.1282，正是 PS.2 要修的 bug。yfinance 出错被 `except Exception: pass` 吞掉 | 汇率只能来自显式传入或实时获取，否则非零退出；删除写死的表，或改为必须加 `--allow-fallback` 并在输出中标为未核实。非美股和 ADR 必须传 `--financial-currency`；SKILL.md 的命令加 `--allow-network`；补离线路径的测试 |
| 2 | 高 | `SKILL.md:40` 对 `scripts/netcash.py:270-303` | SKILL.md 写的命令跑不起来 | `netcash.py --input netcash_input.json --json`：脚本没有 `--input` 参数，以退出码 2 结束。智能体随后可能自己手算净现金或编造参数 | 支持 `--input` JSON，或在 SKILL.md 写真实的参数 |
| 3 | 高 | `SKILL.md:58-60` 对 `scripts/earnings_quality.py:232-248` | 同上 | SKILL.md 用 `--ni --assets-begin --assets-end`，脚本要求 `--net-income --avg-assets`，argparse 报错 | 统一参数；最好接受期初、期末资产，在脚本里求平均 |
| 4 | 中 | `scripts/earnings_quality.py:123-139` | 缺少输入时现金周转天数（CCC）会变成编造的数字 | `--revenue 500 --ar 100`，不传 COGS、存货、应付，返回 `ccc_days 73.0`，会作为真实值写进 facts.md 的 `qoe_ccc`，违反"缺失用 null"的规则。文档承诺的平均值计算也没有实现 | 缺少任一分项就返回 null；三项都有或标为 N/A 时才计算 CCC |
| 5 | 中 | `scripts/netcash.py:94-122` | 只要传了一项非自由现金，就不再扣除受限现金 | `--restricted-cash 500 --non-free-cash trapped:200`：严格口径只扣 200，保留了 500。违反"所有行业受限存款、质押存款从现金端剔除" | 受限现金始终扣除，除非某一项以 `include=false` 并写明理由覆盖 |
| 6 | 中 | `scripts/netcash.py:84-90, 104-109` | 扣除项只有 `amount_usd` | PS.2、`netcash.md` §2.2 和基金原 skill 都要求 `item, value_local, value_usd, include, reason` | 一路保留 `value_local` |
| 7 | 中 | `references/output-schema.md` | 文档里的输出结构和脚本实际输出不一致 | anchors：文档是 `market_cap.value_usd`、`value_mn_usd`、`shares`、`fx.from/to`，脚本输出 `market_cap_usd`（实际单位是百万）、`shares_mn`、`from_currency/to_currency`。netcash：文档是嵌套的 `basic/strict` 加明细，脚本输出平铺的合计、没有明细。股东回报的键名和 `rationale_zh` 格式也不同。**疑似：** `market_cap_usd` 是百万，而 netcash 的 `--market-cap-usd` 要和资产负债表口径一致；SEC 数据是美元原值，净现金和市值之比可能差 1e6 | 键名改为 `market_cap_usd_mn`，两种口径都输出；用真实输出重新生成文档 |
| 8 | 中低 | `scripts/return_score.py:91-97` | `rationale_zh` 没有可信度档位和理由 | 文档要求粘贴为评分明细，包括"可信度档位及理由"，但只输出"可信度 90%"；还有"-0.5（-0.5（x））"这样的重复嵌套。基金原脚本会输出档位说明 | 加档位表和 `--credibility-reason`，去掉重复的调整值 |
| 9 | 低 | `scripts/netcash.py:175-187` 对 `references/netcash.md` §3.1 | "大存大贷"和现金质量的阈值，文档和代码不一致 | 文档：负债/现金 ≥ 30%；代码：≥ 50% 加市值条件。没有市值时，负债/现金 ≥ 0.5 就判定为 complex。文档把无法解释的其他应付/应收 > 20% 判为 complex，代码判为 moderate | 统一为基金原 skill 的口径 |
| 10 | 低 | `scripts/anchors.py:213, 226` | 股数单位靠猜 | 大于 1e5 当作原始股数，否则当作百万股。以"千股"为单位的数据会让市值小 1000 倍 | 加 `--shares-unit` |
| 11 | 低 | `scripts/return_score.py:57` | R2 必须正好三年数据 | 上市不满三年的公司没有 N/A 的写法，会诱导智能体补一个年份 | 允许 R2 为 null 并写明理由 |
| 12 | 低（疑似，不在本模块） | `plugins/dalton/skills/dalton-earnings-deep/scripts/price_reaction.py:64` | `auto_adjust=False` | 窗口内发生拆股时，52 周区间会失真 | 改为复权价，或显式处理拆股 |

## PS.2 列出的四个已知移植 bug

| bug | 状态 |
|---|---|
| 港币汇率对 | **只修了一半**：传入币种时正确（报表用 CNYUSD，价格用 HKDUSD，有测试）；默认路径和离线路径仍有问题（见问题 1） |
| 隐含资产里未换算的价格 | 已修：`anchors.py:204-206` 会换算价格，0700.HK 500 HKD 得到 64.1 USD。没有隐含资产脚本，这部分计算由智能体按 `hidden-assets.md` 完成 |
| 评分显示的四舍五入 | 已修：只对最终分数取整。回归样例 R1 9.96、R2 12.72、R3 13.15，最终 13.1；+0.5 的调整显示为"+0.5" |
| 伪造的 52 周高低点 | 已修：`price_reaction.py:178-206` 用真实的 252 个交易日窗口，样本不足时会标注。公司背景 skill 没有脚本，这一项由智能体自行取数 |

## 已确认没问题的关键点
- 净现金的两种口径按基金原 skill 说明实现：基础口径包含受限现金、定期存款和高流动性的 FVPL/FVOCI；负债只算有息负债；租赁列示但不扣除；严格口径 = 基础口径 − 类债务项 − 非自由现金；行业表忠实，另加了平台/金融科技一行。
- `hidden-assets.md` 与基金一致：A–D 分类、排除项、折价表（10–20、20–40、20–30、20–40、30–50）、显著性阈值（> 10%、3–10%、< 3%）、0.5% 以下不查的规则；没有可靠估值依据时写"无可靠独立估值依据"，不编造；和净现金去重。
- `shareholder-return.md` 与基金的 `scoring.md` 一致：股息按支付日计；授权额度不算；股票股利不算；特别股息连续两年以上才计入；真实回报占比依次按股数口径、费用口径，否则默认 50%；可信度五档；调整合计限制在 [−1, +2]，最终分限制在 [0, 20]，只取整一次。
- `return_score.py` 的计算和基金原 `compute_score` 完全一致，输入有校验。
- Sloan 比率和 CFO/NI 计算正确，包括净利润为负或为零的情况。
- 港股代码补零正确（700.HK → 0700.HK）。
