# 回填总纲

指望周更慢慢攒不现实：每周只有几家出财报，攒满 290 家要两年。回填是独立于周更的一次性工程，跟周五那个窗口完全隔离。详细执行规程见下半部分。

窗口统一为 **2025Q1 至最近一个完整季度，六个季度**。财务和原话用同一个窗口，两边能按季度并排看。

一期三层，顺序不能换：

1. **业务画像**，290 家全量，`add-profile`。它决定一家公司走哪张排序表、有没有转写可拉，是后面所有取数的路由表，必须先做完。这一层不依赖财报文档，跑得最快。
2. **财务 + AI 指标 + 管理层原话**，按公司分批并行，每批把该批公司六个季度的三类数据**一次做完**。不拆成三轮：它们共用同一份电话会转写，拆开等于把同一篇纪要拉三次、读三次，配额和时间翻三倍，而且几轮之间口径容易对不上。
3. **对账**，`coverage-ledger --write` 全表核，空格子要么回填，要么用 `mark-gap` 标明缺什么、为什么缺。

**二期：专家口径层**，`backfill-plan --layer expert` 切批，`add-expert` 入库。

放二期有两个理由，都不是「不重要」：

- 它的取数节奏跟前三类不同。前三类共用同一份电话会转写，一次阅读三类产出；专家层要按公司逐家检索 Guidepoint 库、逐份读访谈，跟财报文档无关，合进一期只会拖慢一期。
- 没有管理层原话在库里，`--alignment` 判不出来。专家说「客户还在 POC 打转」到底是补充还是矛盾，取决于同期管理层说了什么。所以 `backfill-plan --layer expert` 只对核心三类已补齐的公司排队，没补齐的不进批次。

台账现在就按四类建格，二期开跑时不用重建口径、不用重跑对账。

`backfill-plan` 负责切批次。批次只决定谁先谁后，不决定谁被覆盖——290 家全在批次清单里，跑完对账。

规则：

- 财务只收 A 级与 B 级。总收入同比直接抄业绩新闻稿里的「up X% year-over-year」，不从 XBRL 自己拆季度相减。
- 原话按「收录判据」收，一个季度讲了 20 处就记 20 条。来源优先级：电话会 Q&A > prepared remarks > 业绩新闻稿。新闻稿那句是写给媒体的，通常四平八稳；被分析师追着问出来的那句才有信息量。
- 专家口径（二期）：`--claim` 用自己的话写，`--verbatim` 不超过 20 词，必须带 `--source-attribution`。判 `contradicts` 必须能指到同期具体哪句管理层表述。
- 能拿到转写的先 `add-transcript` 留档，再从留档里抽，`--transcript-file` 和 `--paragraph-index` 回指原文。
- A 股绝大多数公司不披露 AI 分项收入，按分级如实标，不能让定性表述稀释掉带 ARR 数字的 A 级证据；确实没有的用 `mark-gap` 标缺口，不留空白格。
- 数据源：美股走 SEC 10-Q/10-K 加 AlphaEngine 电话会转写；港股走港交所公告加 AlphaEngine；A 股走巨潮定期报告与业绩说明会记录。

---

# 执行规程

这份文件是回填批次的作业指导书。并行跑的每一批都按同一套判据抽取，不同批次抽出来的东西才可比。

窗口固定：**2025Q1 至最近一个完整季度**，共六个季度。财务和原话共用这个窗口。

## 一、批次怎么领

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> backfill-plan --layer profile --batch-size 25   # 第一层
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> backfill-plan --layer company --batch-size 10   # 第二层
```

输出里每批带 ticker 清单和该批还缺多少格。一个批次只碰自己那几家的目录，不写别家的文件。

批次只决定顺序，不决定覆盖范围。全部 290 家都在清单里，最后由 `coverage-ledger` 对账。

## 二、第一层 业务画像（290 家全量）

先做完这一层再动第二层。它决定每家走哪张排序表。

判断顺序：

1. 读公司最近一份年报或 20-F 的业务描述，不是读新闻稿。
2. 读产品页与定价页，确认 AI 是独立产品、内嵌能力，还是营销词。
3. 确认披露形态：有没有单列 AI 分项收入、有没有披露 AI 用量口径、能不能拿到电话会转写。

`--route` 四选一：

- `channel_one` 财报单列 AI 分项收入 / ARR / bookings，走表一。
- `channel_two` 不单列 AI 分项，但主业增长可能被 AI 推动，走表三。**默认落点**，多数公司在这里。
- `usage_only` 只给用量口径（MAU、tokens、渗透率），走表四。
- `insufficient` 两条通道都进不去，榜单显示数据不足。选它必须同时 `mark-gap`。

`--purity` 判据是**公司交付的核心产品有多依赖 AI**，与是否披露 AI 收入无关。与 `universe.json` 里的既有判断不一致时照实填，`profiles` 命令会把不一致列出来供复核，不要为了对齐而改判断。

`--business` 和 `--ai-role` 各写一到三句，落到具体产品和客户，不写「领先的 AI 解决方案提供商」这类话。`--evidence-url` 填做出判断所依据的那份材料。

## 三、第二层 财务 + 指标 + 原话（同一遍做完）

对每家公司的每个季度，按下面顺序做一遍，**不要分三轮**：

1. 取该季业绩新闻稿与财报原文 → `add-fundamental`（总收入、公司披露的总收入同比、cRPO/RPO 同比、净留存、大客户数）。
2. 同一份材料里如果公司单列了 AI 分项数字或用量数字 → 每个数字一条 `add-metric`，披露了几个记几个。
3. 取该季电话会转写 → `add-transcript` 留档 → 逐段扫，命中收录判据的每一段一条 `add-quote`。
4. 该季确实没有的东西 → `mark-gap` 标明缺什么、为什么缺、查过哪些来源。

### 原话收录与抽取

抽取前读 [数据库合同的原话收录判据](database-contract.md#原话收录判据)，统一使用五种 anchor、完整段落、全量收录、负面/零结果也收和标签不参与过滤的规则；本批不另设筛选门槛。

回填另保留两条：逐字存原语言，不改写、不翻译、不合并两段，并记录 `--language`；同一段在新闻稿与电话会都出现时，两条都记，用 `--venue` 区分。Q&A 用 `--analyst-question` 保留原问题。

### specificity 怎么判

- `vague` 只有方向没有量：「AI 正在帮助我们的客户提效」——有对象但没结果。
- `directional` 说清影响哪个指标但没给数：「AI 带动了续约率」。
- `quantified` 给了可核对的数字：「AI 相关 ARR 超过 5 亿美元」。

从 `vague` 跳到 `quantified` 的那个季度，通常是公司内部确认这门生意成立的时点。这条线只有在标签打全的情况下才能画出来，所以每条都要标。

## 四、来源优先级

原话：电话会 Q&A > 电话会 prepared remarks > 投资者日 > 业绩新闻稿 > 定期报告正文。

财务：业绩新闻稿与财报原文里公司自己写的同比百分比 > 自己算。XBRL 里 Q2、Q3 常常是累计口径，脚本相减容易算错。

取数路径：美股走 SEC 10-Q/10-K（`finance(route="sec", operation="list-filings")`）加 AlphaEngine 电话会转写；港股走港交所公告加 AlphaEngine；A 股走巨潮定期报告与业绩说明会记录。

## 五、缺口怎么标

确认某格拿不到时，`mark-gap` 必填 `--reason` 和 `--checked-source`：查过哪些来源才判定缺失。

- `no_transcript` 无电话会转写
- `no_chinese_minutes` 只有中文纪要或无规范纪要
- `not_disclosed` 该期未单独披露
- `no_earnings_call` 该市场不开业绩会
- `not_listed` 该期尚未上市
- `not_reported_yet` 该期财报尚未发布
- `source_unavailable` 来源暂时取不到，需重试

`source_unavailable` 是唯一一个「以后要重试」的原因，其余都是确定性缺失。不要用它当兜底：拿不到就查清楚是哪一类。

## 六、批次收尾

每批跑完自查：

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> coverage-ledger --tickers <本批 ticker 列表>
```

本批的每一格必须是「已补」或「已标缺口」，不能留「待补」。有待补就说明这一批没做完，不要报完成。

全部批次跑完：

```bash
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> coverage-ledger --write
python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> profiles
```

台账里剩下的空格子就是真实缺口，逐条处理完才算收工。

## 六点五、第二期 专家口径层

一期三层对账清零之后才开这一层。领批次：`python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> backfill-plan --layer expert --batch-size 10`，它只把核心三类已补齐的公司排进批次。

每家每期做三步：

1. 用 `research(route="guidepoint", operation="search_library")` 按公司名和产品名检索该季度前后的访谈，query 优先英文。
2. 逐份读，把讲到这家 AI 业务的主张抽出来。判据跟管理层原话一致：有对象才收，只有立场不收。
3. `add-expert` 入库，逐条判 `--alignment`：跟同期管理层怎么说的对一遍。

判 `--alignment` 的方法：先跑 `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> quotes --ticker T --since <该季>` 把同期管理层原话调出来，再决定这条专家主张是 `confirms`、`contradicts`、`extends` 还是 `unrelated`。同期库里没有管理层表述的写 `no_management_claim`，不要凭印象判 `contradicts`。

判 `contradicts` 必须填 `--management-ref`，指到具体哪句（fingerprint 或「期间 + 要点」都行）。CLI 会拒收没有 ref 的矛盾——无法复核的矛盾没有价值，只会在报告里制造噪音。

合规：`--verbatim` 不超过 20 词（CLI 强制），必须带 `--source-attribution` 原文，`--expert-background` 写岗位和视角、不写姓名。专家内容不得用于 KYC 或合规筛查。

检索不到覆盖的：`mark-gap --data-type experts --reason no_expert_coverage --checked-source "查过 Guidepoint，该季无相关访谈"`。这一层缺口会很多，属正常，标清楚即可。

## 七、不要做的事

- 不要为了让某家公司「看起来有数据」而降低证据等级往里塞。宁可标缺口。
- 不要在抽取阶段判断某句话「不重要」而丢掉。判据只管有没有对象。
- 不要改别的批次的公司目录。
- 不要动 `state/ai_app_db/snapshots/`，那是周报的对照基线。
- 不要把回填产物写进旧的 `state/ai_app_metrics.jsonl` / `state/ai_app_fundamentals.jsonl`，它们迁移后只读。
- 不要为了凑专家层覆盖率把泛行业访谈硬挂到某家公司名下。对不到具体公司的留在周报正文，不入库。
- 不要在专家层做自动语义比对判矛盾。矛盾必须是人读完两边后判的，并能指到具体那句管理层表述。
