---
name: dalton-richard-coverage-weekly
description: "Richard 覆盖 105 家公司周报（Core 核心 27 家逐家深查 + Watch 观察 78 家按相对同组异动筛选），生成飞书文档并在定时模式下提交简报"
---

# Richard 覆盖周报（Core 逐家深查 + Watch 异动筛选）

为 Richard Lu 覆盖的 105 家公司生成周度更新：Core（核心覆盖，27 家）逐家深入检索；Watch（观察名单，78 家）先按相对同组的表现筛选，只对异动公司定向检索；最终生成飞书文档。

自行撰写的标题、正文、摘要、日志和最终回复遵守文末「撰写硬规则」。逐字引文、原始转写、链接、代码、命令和固定协议字段保持原样。

---

## 运行模式（按需对话 vs 定时自动化）

1. **按需对话模式**：用户在对话中触发（可指定起止收盘日，也可上传新的 Weekly Risk Reward（周度风险回报）CSV（逗号分隔值）表格）。遇到表格中未映射的新增公司或行情接口报错时，先向用户确认映射信息或重试。
2. **定时自动化模式（每周日 09:00 Asia/Shanghai）**：无人值守运行，全程执行至完成或明确失败为止，跳过向用户提问：
   - 默认时间窗口为「上周五收盘至本周五收盘」，由 `window` 子命令按美东 `16:00 America/New_York` 收盘时间，推算最近一个已收盘的周五作为结束日（`end`），前推 7 天作为起始日（`base`）。若在美东周五 `16:00` 收盘前运行，结束日取上一周的周五，不取当天。港股与 A 股按各自收盘日计算，休市处理见下文「假期与休市规则」。
   - 完成后通过 `automation_report` 提交**纯文本简报**（内容依次为：飞书文档链接、3 句以内本周核心要点、未覆盖与待核查清单）。**接收方由自动化配置决定（用户飞书私信），正文、参数和日志中均省略接收方标识。**
   - **三种提交标识（`report_run_id`）的使用规则**（同一标识保存后内容不可改；若用周报正文的标识提交了失败说明，同周稍后重跑成功的周报正文就无法发出）：
     - **周报正文**（正常完成时）：固定使用 `window` 返回的 `report_run_id`，格式为 `richard-coverage-weekly-<end>`（例如 `richard-coverage-weekly-2026-10-16`），传 `material="yes"` 与 `text` 提交。
     - **失败说明（`window` 已成功，后续步骤阻塞时）**：改用 `window` 返回的 `failure_report_run_id`，传 `material="yes"` 与 `failure_text` 提交明确失败原因，禁止写成「无更新」，**严禁使用周报正文的标识**。本次运行内重提失败说明时，必须沿用同一个 `failure_report_run_id`。
     - **失败说明（`window` 本身返回 `ok: false` 时）**：因无 `end` 与 `failure_report_run_id`，改用 `richard-coverage-weekly-failed-window-<当前 UTC（协调世界时）时间，格式 YYYYMMDDTHHMM>`，同样传 `material="yes"` 与 `failure_text` 提交后结束。

---

## 覆盖名单与分类规则

- 名单以最新的 Weekly Risk Reward（Richard）表格为准：Coverage（覆盖层级）列标 `Core` 为逐家深查层，标 `Watch` 为筛选层，空白行作为同组对比公司（peer，未纳入覆盖名单的可比同行）。
- 用户在对话中提供新表格时传入 `--sheet <CSV路径>`（`screen_movers.py` 直接读取 Coverage 列）；未提供时默认使用内置名单（2026-10-09 版：Core 27 家、Watch 78 家，共 105 家；若工作区存在 `state/richard-coverage-weekly/universe.csv` 则优先读取）。
- **Live Position（当前实盘持仓，下称「持仓」）规则**：持仓公司仍按表格各自所属标签处理——Google、Amazon、DoorDash、Affirm、亚朵、Boss直聘为持仓 Core；EchoStar、D.R. Horton、唯品会为持仓 Watch。持仓 Watch 公司即使未触发异动阈值，也必须进入 `targets.json`，并在飞书文档 Watch 行情对比表最前面单独列出涨跌与同组对比。
- 股票代码、同组分组、所属板块与标签定义在 `scripts/screen_movers.py` 的 `TICKERS`（324 家全映射，含未覆盖的同组对比公司），ETF（交易型开放式指数基金）和额外对比公司定义在 `EXTRA_PEERS`。CLI（命令行工具）中的 `CORE` 列表补充了 Core 27 家的中英文检索别名，`self-test` 子命令会校验两边 Core 名单严格一致。新表格出现 `unmapped`（未映射公司）时，先补全映射再重跑。
- 已知改名公司：EchoStar 代码 `ECHO`（原 SATS）；Marsh 代码 `MRSH`；Fiserv 代码 `FISV`；Block 代码 `XYZ`；BNY 代码 `BNY`（原 BK）；智谱 `2513.HK`，Minimax `0100.HK`，希音 `0625.HK`。

常用辅助命令（工作区路径用 `--workspace-dir <workspace>` 传入）：

```bash
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py self-test
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py list
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py list --core
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py list --watch
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py --json list
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py estimate --json
```

---

## 时间窗口与状态目录

```bash
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py \
  window [--mode auto|manual] [--base <YYYY-MM-DD>] [--end <YYYY-MM-DD>]
```

- 默认窗口（`--mode auto`）：取上周五收盘至本周五收盘。脚本查询美股基准指数 `SPY`（标普 500 指数 ETF）实际日线（收盘 K 线）确认结束日是否为美股交易日；若在美东周五 `16:00 America/New_York` 收盘前运行，或周五休市（例如耶稣受难日），自动回退到之前最近一根完整收盘 K 线作为 `end`。**若 `SPY` 日线获取失败，脚本返回 `ok: false`**：立即停止，禁止猜测日期；按需对话模式提示用户确认或显式指定 `--base` / `--end`，定时自动化模式按前节「失败说明（`window` 本身返回 `ok: false` 时）」提交失败说明后结束。
- 按需对话周中触发（`--mode manual`）：按 `SPY` 实际日线取截至美东 `16:00` 前最近一根已完成收盘 K 线作为 `end`（自动跳过周末与节假日如 `2026-01-01`），`base` 为 `end - 7` 天；用户显式指定起止日时用 `--base` 与 `--end` 覆盖。
- **检索窗口**：向前后各外推 1 天至 `[base - 1 天, end + 1 天]`（即 `search_from` 至 `search_to`），避免遗漏盘后与亚洲时区发布的信息。
- **假期与休市规则**：港股、A 股或美股在窗口内遇长假或休市时，在飞书文档开头写明实际开盘天数；零交易日（整周休市）的市场涨跌单列标注，不直接与正常交易市场横向混比。
- **单期状态与产物目录**：`state/richard-coverage-weekly/runs/<end>/`
  - `screen.json`（全量行情筛选结果）
  - `targets.json`（待深查名单：Core 27 家 + 命中 Watch + 持仓 Watch）
  - `notes.json`（内部库扫描结果）
  - `ae/`、`x/`、`xq/`（各来源逐家原始取数缓存；雪球摘要另存为 `xq/<code>.summary.json`）
  - `draft.md`（飞书发布前成稿）
  - `part_<N>.md`（按 `---` 切分后的第 `N` 段正文原文）
  - `feishu-return.json`（`feishu_create_doc` 原始返回）
  - `feishu-read.json`（`feishu_read` 完整回读返回，用于段落核查）
  - `doc.json` / `doc.progress.json` / `doc.failed.json`（单期发布防重记录——即防止同周重复建档的凭证文件、分段追加进度与失败次数记录）

---

## 第一步：行情筛选（Core 与 Watch 全部 105 家）

```bash
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/screen_movers.py --json \
  --workspace-dir <workspace> \
  --base <base> --end <end> \
  [--sheet <csv>] \
  --out state/richard-coverage-weekly/runs/<end>/screen.json
```

- 每家公司计算：窗口涨跌、同组中位数（含同组全部公司、未覆盖对比公司及 `EXTRA_PEERS`）、相对同组超额收益、每日超额收益及 60 日历史 z 值（标准分，衡量当前超额相对过去 60 个交易日波动幅度的偏离倍数）、每日量比（当日成交量与前 20 日均量的比值）。
- Watch 异动命中规则（满足任一即命中）：
  - 窗口超额 `abs(window_excess) >= 6pp`（6 个百分点，即 `0.06`）或 `abs(window_z) >= 2.0`
  - 单日超额 `abs(excess) >= 5pp`（5 个百分点，即 `0.05`）或 `abs(z) >= 3.0`
  - 单日量比 `vol_ratio >= 2.5`
- **强制停跑条件：`screen.json` 返回 `ok: false` 时严禁继续。** 若超过半数美股尚无 `end` 当日收盘 K 线，脚本返回 `ok: false` 与 `warning`（`targets` 子命令也会拒绝执行）。严禁拿缺日数据下结论：
  - **按需对话模式**：暂停生成，提示用户改选最近完整交易日或稍后重跑。
  - **定时自动化模式**：可立即重跑 `screen_movers.py` 确认 1 次；若仍返回 `ok: false`，立刻停止后续检索与建档，改用 `window` 返回的 `failure_report_run_id` 提交**失败说明**后结束。
- 仅命中「量比」且价格走势平稳、或明显由同一行业或宏观事件带动的 Watch 异动公司，写作时按同一事件合并成一段，免于逐家单列。

生成待深查名单（Core 27 + 命中 Watch + 持仓 Watch）：

```bash
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py \
  targets --workspace-dir <workspace> \
  --screen state/richard-coverage-weekly/runs/<end>/screen.json \
  --out state/richard-coverage-weekly/runs/<end>/targets.json
```

---

## 第二步：Core 逐家检索（27 家，五类来源各自独立查询、缺一如实标缺口）

每家公司单独查询，保持五类来源完整，严禁把多家公司合并到同一次查询中：

1. **AlphaEngine（外部研报与新闻数据库）卖方研报与新闻点评**：
   - 先用批量脚本在代码沙箱（脚本运行隔离环境）内通过 `tools.researchdata` 串行扫描 Core（持仓 Core 优先，其余 Core 随后）。每家查 2 次（研报/纪要类 `["sell_side_report", "sell_side_comment", "meeting_minutes"]` 与新闻/公告类 `["news", "announcement"]`），相关命中不足 3 条且存在另一语种别名时自动补查 1 次。
   - 单日检索额度约 190 次。若返回额度上限或无权访问错误，脚本立刻以退出码 `3` 停止：保留已保存的 `ae/*.json`，**停止后续公司的 AlphaEngine 检索（不再重试 AlphaEngine）**，继续完成其余来源与成稿，并将未查 AlphaEngine 的公司如实列入飞书文档「口径与缺口」。若出现登录过期类报错，按需对话模式提示用户重新认证后再补跑；定时自动化模式下同样停止 AlphaEngine 检索并记入「口径与缺口」，若全部关键来源均受阻则提交**失败说明**。

   ```bash
   python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py \
     ae --workspace-dir <workspace> \
     --targets state/richard-coverage-weekly/runs/<end>/targets.json \
     --run-dir state/richard-coverage-weekly/runs/<end> \
     --date-from <search_from> --date-to <search_to> --tier Core
   ```

   - 直接在沙箱外或单家补查时使用 `research` 工具（`get_document` 每次运行严格控制在 8 篇以内；日常先看 `ae-digest` 子命令输出的摘要挑重点，再按需拉取全文）：

   ```python
   research(
       route="alphaengine",
       operation="search_library",
       query="Alphabet",
       start="2026-10-08",
       end="2026-10-17",
       limit=20,
       params={"document_categories": ["sell_side_report", "sell_side_comment", "meeting_minutes"], "sort": "relevance"},
   )
   ```

   ```python
   research(
       route="alphaengine",
       operation="get_document",
       params={"doc_id": "123456", "content_field": "original_text", "offset": 0, "max_chars": 30000},
   )
   ```

2. **内部库（`sales-notes` 卖方销售邮件、`tmtb` TMT 行业资讯库、`funda-ai` 基本面研究库、`company-wiki` 公司研究知识库）**：
   - 运行沙箱脚本一次扫完全部 `targets.json`（内部通过 `tools.researchdata` 的 `research` 工具取数）：

   ```bash
   python3 .agents/skills/dalton-richard-coverage-weekly/scripts/sales_notes_search.py \
     --workspace-dir <workspace> \
     --targets state/richard-coverage-weekly/runs/<end>/targets.json \
     --date-from <search_from> --date-to <search_to> \
     --out state/richard-coverage-weekly/runs/<end>/notes.json
   ```

   - 匹配与排序规则：`kw` 对 ASCII（英文及数字等基础字符）关键词执行区分大小写整词边界匹配（避免 `DASH`、`ECHO` 误配普通英文单词），对 CJK（中日韩文字）按子串匹配；每家输出 `matched_docs`、`by_source`、最多 12 篇 `picks`（按 `sales-notes`/`tmtb` 优先 -> `funda-ai` -> `company-wiki`，同层按命中数与日期倒序）、`omitted`、`omitted_list`、`limit_hit`。
   - 持仓公司必须逐条查看 `omitted_list` 中的遗漏标题；有重要遗漏或需看完整长文时，按 `document_id` 调用 `research` 读取全文。当前无本地离线后备库，若某个内部子库超时或报错，脚本会在 `warnings` 中显式列出失败来源；只要 `warnings` 含失败来源或 `limit_hit` 非空，必须写入「口径与缺口」。

   ```python
   research(
       route="all",
       operation="read",
       document="mail:111154",
       params={"offset": 0, "max_chars": 30000},
   )
   ```

3. **新闻（`web_search`）**：
   - 每家 Core 至少执行 1 次单公司查询；对异动交易日补查 `"<公司> stock <日期> why"`。若报错或结果为空，换更具体的英文或中文公司全称重试 1 次，仍失败则在文档中标注「搜索失败」。

   ```python
   web_search(
       query="DoorDash stock news",
       start_date="2026-10-08",
       end_date="2026-10-17",
   )
   ```

4. **X（原 Twitter 社交平台，`community` 路由 `x-timeline` 为主，`x_search` 辅助）**：
   - 批量抓取原帖并按浏览量与点赞数倒序排列阅读；港股、A 股或易混代码（如 `$BZ`）自动使用脚本内置的 `X_QUERY` 关键词表达式：

   ```bash
   python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py \
     x-timeline --workspace-dir <workspace> \
     --targets state/richard-coverage-weekly/runs/<end>/targets.json \
     --run-dir state/richard-coverage-weekly/runs/<end> \
     --since <search_from> --until <search_to> --tier Core
   ```

   - 单家补充调用形式：

   ```python
   community(
       route="x-timeline",
       operation="search",
       query="$GOOGL",
       since="2026-10-08",
       until="2026-10-17",
       count=40,
   )
   ```

   ```python
   x_search(
       query="GOOGL Alphabet",
       from_date="2026-10-08",
       to_date="2026-10-17",
   )
   ```

   - `x_search` 为合成检索，仅作交叉参考；结论须与行情及一手来源一致，有出入时弃用并在「口径与缺口」说明。

5. **雪球（`community` 路由 `xueqiu`，仅作散户情绪参考）**：
   - 批量抓取雪球帖并本地按 `since` 过滤和 `view_count` 倒序排列（港股自动补全 5 位代码如 `03690`，A 股自动转为 `SH`/`SZ` 前缀）。
   - **抽样范围说明**：`xueqiu` 接口仅接受 `ticker` 与 `pages`，属于「近期帖子抽样（最近 N 页，可能遗漏较早热帖）」；`xq` 子命令把接口原始响应完整存入 `xq/<code>.json`，再把过滤排序后的窗口摘要另存为 `xq/<code>.summary.json`。若调用失败或返回为空，严禁写成「无讨论」，必须在文档「口径与缺口」如实说明调用失败或无返回，并明示雪球为近期帖子抽样：

   ```bash
   python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py \
     xq --workspace-dir <workspace> \
     --targets state/richard-coverage-weekly/runs/<end>/targets.json \
     --run-dir state/richard-coverage-weekly/runs/<end> \
     --since <search_from> --tier Core
   ```

   ```python
   community(
       route="xueqiu",
       operation="posts",
       ticker="03690",
       pages=2,
   )
   ```

**每家 Core 必须回答的问题**：
1. 区间内涨跌和同组对比；
2. 哪一天放量或明显偏离同组；
3. 那一天发生了什么；
4. 卖方怎么看（机构、日期、评级、目标价）；
5. 这周为什么涨或跌；
6. 对持仓逻辑的影响和下一个验证点。

---

## 第三步：Watch 先看相对表现，再视情况检索

- 仅深入检索第一步命中异动阈值的 Watch 公司（简称「命中 Watch」）；未命中公司仅做行情汇总，免于逐家检索（但持仓 Watch 中的 EchoStar、D.R. Horton、唯品会固定单列于 Watch 行情对比表最前面）。
- 对命中 Watch，先读取第二步内部库脚本输出的 `notes.json` 结果（零额外外部调用成本），再为每个异动交易日补充 1 条 `web_search`，必要时按第二步相同规则调用 `x-timeline` / `xueqiu`。
- 对命中 Watch，按以下 3 项条件逐家筛选出真正需要研报补充的公司，再用 `--only <ticker列表>` 定向执行 `ae`（仅研报/纪要类，主名称，每家最多 1 次，排在 Core 之后串行运行；遇到额度上限同样立刻停止后续公司的 AlphaEngine 检索并写入飞书文档「口径与缺口」）：
  - 前几步检索仍未解释清异动原因；
  - 窗口相对超额超过 ±10 个百分点；
  - 异动当天发生了业绩发布、指引调整、卖方评级或目标价重大变化等需要机构解读的事件。

```bash
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py \
  ae --workspace-dir <workspace> \
  --targets state/richard-coverage-weekly/runs/<end>/targets.json \
  --run-dir state/richard-coverage-weekly/runs/<end> \
  --date-from <search_from> --date-to <search_to> --tier Watch \
  --only <满足条件1至3的Watch_ticker逗号列表>
```

- 写作按事件归类（监管与政策、业绩与指引、评级与目标价调整、行业与宏观驱动等），同一事件带动的多家公司合并写一段，避免零散罗列。

---

## 第四步：飞书文档发布与防重校验

### 4.1 标题、目录与结构格式要求

- **标题**：`Richard 覆盖周报 <base> 至 <end>（Core 逐家 + Watch 异动筛选）`
- **目录**：固定放入飞书 Eve 文件夹，参数传 `folder_token="SwkOf7AlFlCuQ5dODCGcY8Wyn1c"`（`feishu_create_doc` 已内建授权分享，无需再执行额外授权操作；若返回值包含 `warning`，在交付说明里提示需要手动补分享）。
- **文档结构（严格按以下顺序）**：
  1. 窗口范围、名单范围、假期与休市说明、基准指数涨跌（SPY / QQQ / KWEB / XLF / XLY / IWM / IGV / 恒生等）。
  2. **本周判断**：3 至 6 条，先给核心结论再给事实支撑。
  3. **Core 逐家（27 家）**：持仓公司排在最前并标注「持仓」；每家统一按「表现 / 发生了什么 / 卖方观点 / 社区 / 判断」展开，回答第二步全部 6 个问题。
  4. **Watch 异动汇总**：按事件归类成段；由同一事件带动的多只公司合并说明。
  5. **其余 Watch 公司行情对比表**：持仓 Watch（EchoStar、D.R. Horton、唯品会）置顶，列区间涨跌、同组中位数、超额并注明「未检索」。
  6. **口径与缺口**：列出 AlphaEngine 额度截断、内部子库失败或截断（`limit_hit`）、`x_search` 核对异常、雪球近期帖子抽样（最近 N 页，可能遗漏较早热帖）、搜索失败公司、休市情况及待核查事项。
- **格式硬规则**：
  - 层级用**粗体行**划分，少用或避免多层 Markdown（轻量标记语言）标题混杂、避免公式（方程）元素；
  - **禁止美元符号 `$`**：金额统一写「美元」，股票代码前禁止加美元符号；
  - **禁止波浪号 `~`**：数字区间与日期区间一律写「至」（例如 `10 至 12 美元`）；
  - 来源标注规范：卖方注明机构与日期；TMTB 注明文章名与日期；新闻聚合站标「新闻聚合」；仅见于单一来源的数字明确标注「单一来源」；X 与雪球观点附原帖链接。

### 4.2 防重检查与发布流程（防止同周重复建档与重复追加）

1. 先把成稿写入 `state/richard-coverage-weekly/runs/<end>/draft.md`，对照 4.1 格式硬规则与文末「撰写硬规则」检查无误后再发布。
2. 创建文档前先运行 `doc-status` 检查单期防重状态：

```bash
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py \
  doc-status --workspace-dir <workspace> --end <end>
```

   按返回值严格进入对应分支：
   - **分支 A（`reason == "create_failed_twice"` 或 `"doc_record_unreadable"`）**：**立即停止建档**，不得再调用 `feishu_create_doc`，走失败处理并如实上报（定时自动化模式下提交**失败说明**）。
   - **分支 B（`exists: true` 且 `create: false`）**：文档记录已存在，**严禁再次调用 `feishu_create_doc`**。再看 `body_incomplete`：
     - 若 `body_incomplete: false`（`pending_parts` 与 `unverified_parts` 均为空，且 `progress_missing` 为 `false`）：建档与全部段落追加均已完成，直接跳至第 6 步用 `feishu_read(..., max_chars=50000)` 做最终回读核查后交付。
     - 若 `body_incomplete: true` 且 `unverified_parts` 非空（含第 1 段部分失败）或 `progress_missing: true`（缺少分段进度记录）：跳至第 5 步先执行 `feishu_read` 与 `verify-append` 核查**待核查**段落，再决定补写或追加。
     - 若 `body_incomplete: true` 且仅 `pending_parts` 非空（`unverified_parts` 为空且 `progress_missing` 为 `false`，属于尚未尝试的正常后续段落）：跳至第 5 步直接按顺序追加 `pending_parts`。
   - **分支 C（`create: true`）**：记录本次 `doc-status` 返回的 `attempt_id`，进入第 3 步建档。

3. **切分段落并创建文档（仅当 `create: true`）**：按 `---` 将文稿切分为 `total_parts` 段（即使短文不分段也显式设为 `1`），把各段原文分别保存至 `state/richard-coverage-weekly/runs/<end>/part_<N>.md`，再调用 `feishu_create_doc` 写入第 1 段：

```python
feishu_create_doc(
    title="Richard 覆盖周报 2026-10-09 至 2026-10-16（Core 逐家 + Watch 异动筛选）",
    markdown="**窗口与基准**\n……",
    folder_token="SwkOf7AlFlCuQ5dODCGcY8Wyn1c",
)
```

4. **立即保存建档返回与初始进度**：无论 `feishu_create_doc` 返回成功、完全失败，还是部分失败（例如 `ok: false` 报错 `content_insert_failed` 但已带 `document_id`/`url`），都必须先将原始 JSON（结构化数据格式）写入 `state/richard-coverage-weekly/runs/<end>/feishu-return.json`，再立即执行 `save-doc` 并**显式传入 `--total-parts <N>` 与本次 `doc-status` 返回的 `--attempt-id <attempt_id>`**：

```bash
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py \
  save-doc --workspace-dir <workspace> \
  --end <end> --total-parts 2 --attempt-id <attempt_id> \
  --file state/richard-coverage-weekly/runs/<end>/feishu-return.json
```

   `save-doc` 的后续分支：
   - 若返回文档链接且 `body_incomplete: false`：跳至第 6 步做最终回读核查。
   - 若返回文档链接且 `body_incomplete: true`（含建档返回 `ok: false` 或 `outcome == "unknown"` 但已带 `document_id`/`url` 的情况，此时第 1 段记入 `unverified_parts` 待核查；或仅多段文稿的后续 `pending_parts` 待追加）：进入第 5 步继续处理。
   - 若无可用 `document_id`/`url`：`save-doc` 按 `attempt_id` 在 `doc.failed.json` 去重计数（同一 `attempt_id` 重复保存只计 1 次，不同 `attempt_id` 即使返回相同报错也分别计数）。若返回 `create: true`（仅失败 1 次），重新执行第 2 步 `doc-status` 获取新 `attempt_id` 后可重试建档 1 次；若返回 `create: false`（`reason == "create_failed_twice"`），**立即停止建档**并走失败处理。

5. **待核查段落校验、逐段追加与断点续跑**：
   - **核查优先**：只要 `unverified_parts` 非空（含第 1 段待核查）或 `progress_missing` 为 `true`，**严禁直接补写或追加**。先调用 `feishu_read(token=<url>, max_chars=50000)`（传足工具上限 `50000` 字符），将返回的完整 JSON 保存至 `state/richard-coverage-weekly/runs/<end>/feishu-read.json`，再运行 `verify-append` 比对规范化后的整段内容（第 2 段起可附 `--expected-after-file part_<k-1>.md`；若 `progress_missing` 为 `true`，补传 `--total-parts <N>`）：

```bash
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py \
  verify-append --workspace-dir <workspace> \
  --end <end> --part 1 \
  --part-file state/richard-coverage-weekly/runs/<end>/part_1.md \
  --doc-json-file state/richard-coverage-weekly/runs/<end>/feishu-read.json
```

   - **`verify-append` 三种返回结果严格分支执行**：
     1. **`present: true`（`action: "marked_completed"`）**：该段已完整存在于预期位置，脚本已登记完成。**跳过 `feishu_append`，严禁重复补写**，继续处理下一段。
     2. **`present: false` 且 `should_append: true`（`action: "append_required"`）**：`feishu_read` 未被截断（`truncated == false`）且文档中完全找不到该段（部分匹配检查也确认未命中）。确认尚未写入后，方可调用 `feishu_append` 补写该段。
     3. **`ok: false` 且 `indeterminate: true`（处于「无法确定」状态，例如 `error_code` 为 `readback_truncated_not_found`、`readback_partial_match`、`readback_failed`、`readback_invalid_json` 等）**：可能由于读回超过 `50000` 字符被截断且找不到，或首尾匹配但中间缺内容（仅部分匹配）。**严禁再调用 `feishu_append` 或整段重新追加，立即停止后续建档与追加**，保留已有文档链接，并在**周报正文**或**失败说明**的「未覆盖与待核查清单」中写明需人工核对该文档。
   - **正常追加与登记**：仅对从未尝试过的 `pending_parts`，或经 `verify-append` 明确返回 `present: false` 且 `should_append: true` 的段落，调用 `feishu_append` 写入，并立即将返回原文（含 `outcome="unknown"`、报错或无法解析的文本——无法解析的返回同样会自动记入 `unverified_parts` 待核查）传给 `record-append --part <k>`：

```python
feishu_append(
    token="https://feishu.cn/docx/DoxExampleDocId20261016",
    markdown="\n---\n**其余 Watch 公司对比表**\n……",
)
```

```bash
python3 .agents/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py \
  record-append --workspace-dir <workspace> \
  --end <end> --part 2 \
  --json-str '{"ok": true}'
```

6. **最终回读核查与交付门槛**：确认 `doc-status` 返回 `body_incomplete == false`（`pending_parts == []` 且 `unverified_parts == []`）后，调用 `feishu_read` 传 `max_chars=50000` 读回核对：章节顺序完整、表格无缺行、正文未残留未成对或未渲染的 `**` 记号。全部核对通过后才算完整完成并正常交付（若前面已因 `verify-append` 返回「无法确定」而停止建档或追加，按第 5 步要求保留文档链接并在「未覆盖与待核查清单」中写明需人工核对）：

```python
feishu_read(
    token="https://feishu.cn/docx/DoxExampleDocId20261016",
    max_chars=50000,
)
```

---

## 交付与自动化模式汇报

### 1. 聊天回复与周报正文共同要求（3 句以内 + 清单）

- 第 1 行：飞书文档标题与完整链接；
- 核心要点：3 句以内概括本周最核心的组合与板块变化；
- **未覆盖与待核查清单**：列出缺口、受限来源与待核查事项（若有未完整核对的飞书文档，写明需人工核对）。
- **零接收方原则**：文本正文及任何工具参数中一律禁止出现群 ID、用户 ID、邮箱、频道名或「发送给 xxx」。

### 2. 定时自动化专属确认流程

在定时自动化触发时，使用 `automation_report` 提交结果（成功时提交**周报正文**，失败时提交**失败说明**）：

- **成功时提交周报正文**：传 `report_run_id="richard-coverage-weekly-<end>"`、`material="yes"` 与 `text`：

```python
automation_report(
    action="submit",
    report_run_id="richard-coverage-weekly-2026-10-16",
    material="yes",
    text="Richard 覆盖周报 2026-10-09 至 2026-10-16\n文档：https://feishu.cn/docx/DoxExampleDocId20261016\n本周要点：1）核心持仓表现…… 2）Watch 板块异动……\n未覆盖与待核：……",
)
```

- **失败时提交失败说明**（例如 `window` 失败、行情缺日且重跑仍不足、内部库全部失败、飞书建档连续失败两次等导致无法交付周报正文时）：同样**必须传 `material="yes"`**，改用 `failure_text` 如实说明真实报错与阻塞步骤，禁止改写成「无更新」或漏传 `material`（否则会触发 `invalid_material` 报错）。标识使用 `window` 返回的 `failure_report_run_id`（若 `window` 本身失败，使用 `richard-coverage-weekly-failed-window-<当前 UTC 时间，格式 YYYYMMDDTHHMM>`），**严禁使用周报正文的标识**：

```python
automation_report(
    action="submit",
    report_run_id="<window 返回的 failure_report_run_id>",
    material="yes",
    failure_text="Richard 覆盖周报 2026-10-09 至 2026-10-16 执行失败：screen_movers 返回 ok=false（多数美股缺失当日收盘 bar），等待完整行情后重跑。",
)
```

**确认投递成功硬条件**：检查 `automation_report` 返回字段同时满足：
- `ok == true`
- `delivery_confirmed == true`
- `status == "sent"`
- `content_kind == "text"`（提交失败说明时按实际返回字段核对送达确认）
- 返回的 `report_run_id` 与本次提交实际使用的标识严格一致（周报正文为 `richard-coverage-weekly-<end>`，失败说明为对应的 `failure_report_run_id` 或 `richard-coverage-weekly-failed-window-...`）。

**未确认投递时的查询与重提规则**：若返回值未满足上述硬条件：
1. 先用**同一 `report_run_id`** 调用 `action="status"` 查询投递状态；
2. 若查询结果显示已确认送达（`delivery_confirmed == true` 且 `status == "sent"`），则停止，无需重提；
3. 若仍未确认送达，用**同一个 `report_run_id` 和同一份正文（`text` 或 `failure_text`）**重提 1 次（失败说明重提也沿用同一次运行的同一个 `failure_report_run_id`，不得新生成标识）。若重提后仍未确认，停止并如实记录投递异常：

```python
automation_report(
    action="status",
    report_run_id="richard-coverage-weekly-2026-10-16",
)
```

---

## 失败处理

- **`window` 返回 `ok: false`**：停止并禁止猜测日期；按需对话模式提示用户确认或显式指定 `--base` / `--end`，定时自动化模式以 `richard-coverage-weekly-failed-window-<当前 UTC 时间，格式 YYYYMMDDTHHMM>` 提交**失败说明**后结束。
- **行情脚本 `screen_movers.py` 返回 `ok: false`**：严禁使用缺日行情下结论；按需对话模式暂停生成并改至最近完整交易日或稍后重跑；定时自动化模式可重跑 1 次，仍返回 `ok: false` 则停止后续检索与建档，改用 `window` 返回的 `failure_report_run_id` 提交**失败说明**后结束。
- **AlphaEngine 额度上限或权限受限**：遇「无查看权限或额度已达上限」（退出码 `3`），立即停止后续公司的 AlphaEngine 检索（不再重试 AlphaEngine），保留已完成公司的缓存，继续完成其余来源与成稿，并在飞书文档「口径与缺口」逐家列明未查公司；若出现登录过期类报错，按需对话模式提示用户重新认证后再补跑，定时自动化模式无法交互认证时记入「口径与缺口」（若全部关键来源均受阻则提交**失败说明**）。
- **`web_search` 报错或无命中**：换更精确的公司中英文全称重试 1 次，仍失败则在飞书文档中标注「搜索失败」。
- **单期飞书建档或追加异常**：依靠 `doc-status` 与 `doc.json` 拦截同周重复建档；连续两次使用不同 `attempt_id` 建档失败（`create_failed_twice`）、记录文件损坏（`doc_record_unreadable`）或 `verify-append` 返回「无法确定」时，立即停止后续建档与追加，按失败处理提交**失败说明**（若已有部分写入文档且因「无法确定」停止，则在「未覆盖与待核查清单」写明需人工核对）。

---

## 撰写硬规则

【撰写硬规则：严禁防御性否定表达】
本任务自行撰写的标题、正文、摘要、分析、研究底稿、图注、卡片、报告、纪要及最终回复，严禁使用防御性否定表达，包括“不代表”“并非”“而不是”“不能”“不构成”“不应”；同义改写的防御性否定句同样禁用。
先直接给出判断，再写事实、依据、适用条件和具体待核项。需要说明边界或不确定性时，用正面、具体、简洁的陈述写清证据范围、置信度及改判条件。
保存或投递成稿前逐句检查，发现上述措辞就整句改写，保留事实、数字、责任归属、结论强弱和必要的风险信息。逐字引文、原始转写、代码、命令、原始报错及固定协议字段保持原样；原任务的栏目、验证、失败披露、来源和投递要求照常执行。
如原流程会委托子任务或调用其他模型撰稿，须将本条完整传入对应 prompt，并在汇总时复核；沿用原有子任务权限与限制。
