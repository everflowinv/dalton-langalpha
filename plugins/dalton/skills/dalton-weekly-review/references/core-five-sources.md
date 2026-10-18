# Core 公司周度五源检索规范（`core-five-sources.md`）

在 `weekly-coverage-review` 的“Core 逐家”阶段，每家 Core 公司由一个独立的 `dalton-evidence` 子智能体负责，**严格遵守“一次只查一家公司”的纪律**，绝不把多家公司混入同一个检索 query。

---

## 一、检索时间窗口

- 设周度量化的起点收盘日为 `base`（上周同一交易日），终点收盘日为 `end`（最近一个完整交易日）。
- 定性检索窗口统一前后各放宽 1 天：**`start = base - 1 天`，`end = end + 1 天`**，以防漏掉盘后公告与跨时区发布的研报。

---

## 二、五类必查来源（Core Five Sources）

### 1. AlphaEngine（卖方研报、卖方点评、会议纪要、新闻公告）
通过 PTC 调用 `researchdata.research(route="alphaengine", operation="search_library", ...)`，每家公司执行 **2 次检索**：
1. **研报与纪要类**：
   - `document_categories: ["sell_side_report", "sell_side_comment", "meeting_minutes"]`
   - `sort: "relevance"`，`start: base - 1d`，`end: end + 1d`，`query`: 公司主名（美股用英文名/ticker，中概用中文名）。
2. **新闻与公告类**：
   - `document_categories: ["news", "announcement"]`
   - 参数同上。
3. **跨语言补查与配额纪律**：
   - 当某类相关结果少于 3 条时，换用另一种语言的公司名补查 1 次；
   - 挑出解释本周股价或基本面变化最关键的文档调 `get_document` 读取正文；
   - **配额保护**：若接口返回“无查看权限或额度已达上限”或 `E_DESKTOP_NOT_RUNNING`，**立即停止重试**，在输出文件及“口径与缺口”中如实写明**“AlphaEngine 额度用尽 / 未登录，未覆盖”**，**严禁写成“本周无更新”**。

### 2. 内部研究库（`sales-notes`、`tmtb`、`funda-ai`、`company-wiki`）
通过 PTC 分别或融合检索内部四库：
1. **`sales-notes`（GS、Jefferies、BofA 等销售邮件）、`tmtb`、`funda-ai`**：
   - 使用公司别名（`aliases`）在窗口内按关键词/语义检索，读取命中文档并按大小写精确复核提及本公司的段落；
   - **别名过宽拆分规则**：若某家公司别名较泛导致单次检索触及返回上限（`limit_hit`），按各个别名分开检索。
2. **`company-wiki`（基金 Wiki，只读）**：
   - 列出窗口内新入库（`indexed_since = base`）的文档：归在本公司目录（`params.company=TICKER`）下的全部读取；行业级文档中命中本公司别名 ≥ 2 次的读取。

### 3. 公开新闻（`web_search`）
1. 执行 1 次带显式日期范围（`date_after = base - 1d`、`date_before = end + 1d`）的常规新闻检索；
2. 对 `screen.json` 中该公司出现**放量（量比 ≥ 2.0x）或单日明显偏离同组（|日超额| ≥ 3pp 或 |z| ≥ 2）**的每个异常交易日，额外执行 1 次定向检索：`"{公司名/TICKER} stock {YYYY-MM-DD} why"`。

### 4. 社交媒体 X
- 检索带 `since:{base}` 的帖子，优先按浏览量与点赞数排序阅读；
- 只采纳有实质数据或基本面拆解的观点，且**必须能与行情走势或其他独立来源相互印证；对不上的内容直接弃用并在文末说明**。

### 5. 雪球（P6 接口）
- 本阶段（P4/P5）跳过雪球检索，统一在周会稿末“口径与缺口”一节注明“雪球数据源在 P6 接入，本期未覆盖”。

---

## 三、每家 Core 公司必须回答的七个问题（输出至 `core-<TICKER>.md`）

子智能体在完成五源检索后，必须先读取该公司工作区的 `../<TICKER>/thesis.md`、`../<TICKER>/debate.md` 以及本周 `../<TICKER>/verdicts.md`，并在 `Home/dalton/research/weekly-<end>/core-<TICKER>.md` 中完整回答：

1. **窗口涨跌与同组对比**：本周复权涨跌幅、同组中位数、超额收益（pp）与窗口 z 值（来自 `screen.json`）；
2. **异常交易日定位**：哪一天出现了放量或相对同组明显背离；
3. **事件还原**：异常日当天及本周发生了什么具体事件（附一手公告、新闻或内部库来源）；
4. **卖方观点汇总**：本周发布研报/点评的机构、日期、评级、目标价变动与核心论点；
5. **归因判断（含 EPS vs 倍数分解）**：结合 `attribution.json` 中的 EPS 预期贡献（pp）与估值倍数贡献（pp），说明本周是盈利驱动、估值驱动、两者兼有还是相抵，以及背后的业务逻辑；
6. **对 Thesis 与 Debate 的影响**：结合该公司本周 `verdicts.md` 已记录的结论与本次新发现，评估对哪个支柱或 debate 有什么影响，给出本周综合事件五词，并判断是否具备升级为本周 `call_candidate` 的潜力；
7. **下一个检查点（Next Validation）**：接下来重点验证的指标、事件与具体日期。

---

## 四、知识层修改提案（`proposals/<TICKER>.json`）

子智能体在 Home 运行，不直接修改 `../<TICKER>/data/*.json`。凡有需要写入公司知识层的内容（包括将本周 EPS vs 倍数归因写入 `drivers` 的股价驱动记录、更新 `debate` 倾向或新增验证项），写入 `Home/dalton/research/weekly-<end>/proposals/<TICKER>.json`：

```json
{
  "ticker": "META",
  "week_end": "2026-10-30",
  "proposed_verdict": "THESIS_STRENGTHENED",
  "summary": "一句话总结本周对知识层的修订建议",
  "updates": [
    {
      "doc": "drivers",
      "reason": "写入 2026-10-30 周度 EPS 修正 vs 估值倍数归因记录",
      "payload": {
        "window": "2026-10-23 to 2026-10-30",
        "price_change_pct": 4.5,
        "eps_contrib_pp": 3.8,
        "pe_contrib_pp": 0.6,
        "driver": "盈利驱动"
      }
    }
  ]
}
```
