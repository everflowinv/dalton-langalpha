---
name: dalton-financial-lenses
description: "Dalton 三把财务镜头与盈利质量计算：在同一报告期、汇率与市值锚点下，计算基础/严格净现金（Definition A/B）、隐含资产（折前/折后及隐含经营资产）、股东回报 R1/R2/R3 二十分制评分与盈利质量（Sloan 比率、CFO/NI、CCC）。触发词：财务镜头、净现金、net cash、隐含资产、hidden assets、股东回报、R1/R2/R3、盈利质量。"
---

# Dalton 财务镜头（`dalton-financial-lenses`）

> Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
> 公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

本技能把基金的三把核心“财务镜头”（净现金双口径、隐含资产、股东回报 R1/R2/R3）及完整覆盖 Stage 0 所需的盈利质量指标（Sloan 比率、CFO/NI、CCC）统一在同一家公司、同一报告期、同一汇率和同一美元市值锚点下完成。它既可通过 `/dalton-financial-lenses` 独立触发，也被 `dalton-company-background`、`dalton-bullbear`、`initial-coverage` 和 `full-coverage`（数字底表 `facts.md`）复用。

**市场范围**：本阶段（P2–P5）只做美股（含美股上市 ADR）；遇到港股或 A 股标的时，直接说明“港股/A 股深度支持在 P6 提供”，不硬算。

---

## 一、执行流程（6 步）

### 第 1 步：读取规范与确认范围
1. 先读 `.agents/skills/dalton-conventions/SKILL.md`。
2. 确认需要计算的镜头：用户单独指定某把镜头时按需加载对应 `references/`，被其他 skill 或 workflow 调用时**默认三把镜头全部计算**（`full-coverage` 阶段 0 同时计算盈利质量 `earnings_quality.py`）。

### 第 2 步：运行 `anchors.py` 锁定统一锚点
所有镜头和后续数字必须共用同一组市值、汇率和报告期锚点，严禁各算各的市值或混用不同日期汇率：
```bash
python3 .agents/skills/dalton-financial-lenses/scripts/anchors.py \
  --ticker {TICKER} --period-end {YYYY-MM-DD} \
  [--financial-currency {USD|CNY|HKD|...}] [--fx-rate {RATE} | --allow-network] \
  [--market-cap-usd-mn {MC_MN}] [--adr-ratio {RATIO}] --json > anchors.json
```
- 股本优先使用最新 10-Q/10-K/20-F 封面股数（`dalton_sec.get_company_facts` 的 `dei:EntityCommonStockSharesOutstanding` 或稀释股数），可传 `--shares`、`--shares-unit {auto|raw|thousands|millions}`、`--adr-ratio` 与 `--price-local` 覆盖；yfinance 仅作行情与实时汇率获取（`--allow-network`）。
- 非美元列报公司（如美股中概 ADR 或港股人民币报表）**必须显式传入 `--financial-currency CNY`**（或开启 `--allow-network` 获取实时汇率 `CNYUSD=X`）；离线且未传 `--fx-rate` 时脚本直接报错退出，严禁使用写死的旧汇率。

### 第 3 步：获取一手 Filing 财务与附注数据
1. 用 `dalton_sec.get_company_facts(ticker=..., concepts=..., periods=...)` 获取最新 10-Q/10-K 资产负债表、利润表、现金流量表科目（严格遵守 R1–R10 规则，带单位、期间与 accession）。
2. 用 `researchdata.finance(route="sec", ...)` 的 `read-item` / `search-text`（或自带 `get_sec_filing`）读取资产负债表附注（现金及短期投资构成、受限资金、有息债务明细、合同负债/递延收入、联营合营与股权投资、回购授权与执行 `share repurchase program`）。

### 第 4 步：按口径与确定性脚本计算三把镜头（及盈利质量）
> **硬规则**：以下三个脚本必须统一传入 `--anchors anchors.json` 读取第 2 步锁定的市值、汇率与期间；同时传入 `--anchors` 与手填 `--market-cap-usd-mn` / `--fx-rate` 会直接报错退出（仅手填时输出会标记 `anchors_source: "manual"`）。

1. **净现金双口径**（详见 [`references/netcash.md`](references/netcash.md)）：
   - 整理现金端、债务端、类债务与不自由现金扣减项，调用确定性脚本计算基础净现金（Definition A）、严格净现金（Definition B）、营运资本、其他应收/应付 >20% 预警、“大存大贷”标记、现金质量评级与隐含经营资产（Implied Operating Assets）：
   ```bash
   python3 .agents/skills/dalton-financial-lenses/scripts/netcash.py \
     --anchors anchors.json --input netcash_input.json --json
   # 或直接传命令行参数（由 --anchors 提供统一市值与汇率）：
   python3 .agents/skills/dalton-financial-lenses/scripts/netcash.py \
     --anchors anchors.json \
     --cash {CASH} --restricted-cash {RC} --time-deposits {TD} --st-investments {STI} \
     --st-debt {STD} --cur-lt-debt {CLTD} --lt-debt {LTD} --convertible-debt {CD} \
     --lease-liabilities {LEASE} --json
   ```
2. **隐含资产**（详见 [`references/hidden-assets.md`](references/hidden-assets.md)）：
   - 识别 A 联营/合营权益、B 上市参股股权、C 投资性房地产、D 其他可变现非核心资产。
   - **去重硬规则**：凡已在净现金基础现金端计入的理财/货币基金类 FVPL/FVOCI，严禁在隐含资产中重复计入。
   - 账面值 < 市值 0.5% 的非核心资产不做联网查价，直接按账面值与标准折扣处理；> 0.5% 的上市持股必须查最新市价穿透并打折。
   - 计算隐含经营资产：`Implied Operating Assets = Market Cap − Net Cash (Strict/Basic) − Hidden Assets (Net)`。
3. **股东回报 R1/R2/R3**（详见 [`references/shareholder-return.md`](references/shareholder-return.md)）：
   - 必算 7 项指标，分母统一使用第 2 步锁定的当前美元市值基准（不回溯历史市值）。
   - 区分回购性质（真实股东回报型 / SBC 稀释对冲型 / 混合型）并计算真实回报比例与 SBC 年稀释比例。
   - **必须调用 `return_score.py` 确定性计算评分，严禁手算或中途四舍五入**：
   ```bash
   python3 .agents/skills/dalton-financial-lenses/scripts/return_score.py --json \
     --anchors anchors.json \
     --ttm-div {TTM_DIV_PCT} --ttm-buyback {TTM_BUYBACK_PCT} \
     --true-return-share {SHARE_0_TO_1} \
     --annual {FY3},{FY2},{FY1} \
     --fwd-total {FWD_TOTAL_PCT} --credibility {100|90|75|60|50} \
     [--credibility-reason "{REASON}"] \
     [--adjust "+1:理由" --adjust "-0.5:理由"]
   ```
4. **盈利质量（Stage 0 / 按需）**：
   - 使用 `scripts/earnings_quality.py` 计算 Sloan Ratio（总应计口径 `(NI - CFO - CFI) / Avg Total Assets` 与经营应计口径 `(NI - CFO) / Avg Total Assets`）、`CFO / NI` 比率、现金转换周期 `CCC = DIO + DSO - DPO`（缺任一分项时 `ccc_days` 返回 `null`）：
   ```bash
   python3 .agents/skills/dalton-financial-lenses/scripts/earnings_quality.py \
     --anchors anchors.json \
     --ni {NI} --cfo {CFO} [--cfi {CFI}] \
     [--avg-assets {AVG_A} | --assets-begin {A0} --assets-end {A1}] \
     [--revenue {REV} --cogs {COGS} --inventory {INV} --ar {AR} --ap {AP}] --json
   ```

### 第 5 步：落盘输出
1. 结构化 JSON 写入 `research/<YYYY-MM-DD>-lenses/lenses.json`（字段严格遵循 [`references/output-schema.md`](references/output-schema.md)）。
2. 中文报告按 [`templates/lenses.md`](templates/lenses.md) 写入 `drafts/<YYYY-MM-DD>-lenses.md`。
3. 被 workflow 子智能体调用时，只向 workflow 返回 JSON/草稿路径与 ≤10 行摘要；在 `full-coverage` Stage 0 中，将 `net_cash_basic`、`net_cash_strict`、`hidden_asset_net`、`return_r1`、`return_r2`、`return_r3`、`return_score`、`qoe_sloan`、`qoe_cfo_ni`、`qoe_ccc` 写入 `facts.md`（FACTS-LEDGER）。

### 第 6 步：自检清单（全部通过方可交付）
- [ ] 三把镜头的报告期、汇率、美元市值锚点完全一致。
- [ ] 每个资产负债表与现金流科目均注明 filing 表名、期间、单位与 accession。
- [ ] 隐含资产已与净现金现金端（FVPL/短期投资）严格去重。
- [ ] 其他应收/其他应付占流动资产/流动负债 > 20% 时已显式预警；已执行“大存大贷”检查；租赁负债已列示备查。
- [ ] 已实现回购只计实际现金支出，未将董事会授权额度混入已实现回报。
- [ ] 股东回报评分由 `return_score.py` 算出，只做最后一步一位小数四舍五入。
