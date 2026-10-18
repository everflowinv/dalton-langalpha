# 财报后前瞻对账与 Thesis 校准规则（`calibration.md`）

本文件定义 `dalton-earnings-deep`（PS.9 阶段 2）的三项必做任务：**① 前瞻预测逐条对账（`reconcile.md`）、② Thesis 支柱财报后校准与反漂移检查（`thesis-calibration.md`）、③ 管理层指引兑现记录更新（`management-delta.md`）**，以及最终汇总生成的 **`knowledge-revision.json`** 规范。

---

## 一、前瞻预测逐条对账规则（`reconcile.py` → `reconcile.md`）

### 1. 逐条核对与三档判定
读取财报前 T-3 生成的 `predictions.json`，使用 `scripts/reconcile.py` 将每条预测与当季一手披露实际值（或最新官方指引）逐一比对，输出固定列对账表：
`指标（ID｜期间｜口径）｜我们的预期｜一致预期｜实际｜对/错/部分｜错因`

每条预测必须明确给出以下三档判定之一，**严禁模棱两可或跳过错判条目**：
- **`对`**：实际结果落在 `our_expectation` 预测区间内（或点预测误差在合理容差内且方向、机制完全吻合）；
- **`部分`**：核心方向判断正确，但幅度超出/不及预期区间，或总量猜对但内部结构（如量价拆分、分部贡献）与预想机制不符；
- **`错`**：实际结果与 `our_expectation` 方向相反或偏离区间，核心驱动机制被证伪。

### 2. 四类错因归类与 `lessons` 提取（强制）
凡判定为 **`错`** 或 **`部分`** 的预测，必须在“错因”栏归入以下四类之一，并写出具体业务原因：
1. **`数据错`**：财报前依赖的高频跟踪、渠道调研或第三方样本存在代表性偏差、口径不一致或时间窗口错位；
2. **`模型错`**：业务量传导到收入/利润率的转化率、经营杠杆、费用刚性、税率或汇兑弹性测算有误；
3. **`判断错`**：对竞争格局、客户需求持续性、新品爬坡速度或管理层资本配置意图的定性判断失误；
4. **`意外事件`**：财报窗口期内发生不可预见的监管处罚、地缘政策突变、一次性诉讼和解或突发供应链中断。

**转化为 `lessons` 候选**：每条“错”或“部分”必须提炼为一条结构化经验教训，包含：
- `lesson`：一句话规律总结；
- `root_cause`：`data_error` / `model_error` / `judgment_error` / `exogenous_shock`；
- `applicable_when`：该教训在未来什么场景下适用；
- `counterexample_or_limit`：什么条件下该规律不成立（避免过度泛化）。

### 3. 连续两个季度同向偏差规则
对照工作区历史对账记录与 `management.md`：
- 若发现在**同一类指标**（例如下季收入指引、核心分部营业利润率）上，管理层指引或我方预测**连续两个季度朝同一方向偏差**（例如实际收入连续两季超越管理层指引中点约 2%–3%），必须将该规律显式写入：
  - `management` 文档的 `guidance_style` / `credibility_notes`（服务端会自动重算 `beat_rate` 与 `median_surprise_pct`）；或
  - `drivers` 文档的对应驱动说明（例如“该公司下季收入指引中点一贯保守约 2%，测算隐含门槛时须加回历史中位 beat”）。

---

## 二、Thesis 财报后校准规则（`thesis-calibration.md`）

沿用 `.agents/skills/thesis-tracker/SKILL.md` 的 `post-earnings` 模式，对工作区 `thesis.md` 的每个支柱进行校准。

### 1. 第一步：盈利质量拆解（Evidence-Quality Parse，防假 Beat）
在将当季业绩作为任何支柱的证据之前，必须先拆解表面 Beat/Miss 的来源：
- 区分是来自**核心业务量（Volume）、价格（Price）、产品结构（Mix）、经常性运营效率**，还是来自**有效税率下降、股票回购缩减分母、外汇顺风、一次性非经营收益或 KPI 定义修改**。
- **硬约束**：若当季 EPS Beat 完全由低税率、回购或一次性项目贡献，而核心收入与经营利润未超预期，则**所有经营性 Thesis 支柱必须维持 `untested`（或按实际经营表现判 `neutral`/`weakening`），严禁判为 `confirming`**。

### 2. 支柱信号词与状态词（严格使用闭集枚举）

- **信号词（`signal`，10 选 1）**：
  `strongly confirming`、`confirming`、`mildly confirming`、`neutral`、`mildly weakening`、`weakening`、`strongly weakening`、`mixed`、`invalidating`、`untested`
- **支柱状态词（`status`，8 选 1）**：
  - `strengthening`：证据超越预期路径，且未来路径不变或更优；
  - `intact`：证据与立项时的预期路径一致；
  - `watch`：触及预警阈值或证据喜忧参半（`mixed`），取决于下一个验证点；
  - `impaired`：该支柱已跌破证伪/破坏阈值，但其他核心支柱仍支撑整体逻辑；
  - `broken`：核心主张已被证伪，持有理由消失；
  - `changed`：公司实际业务演进已偏离原立项逻辑，需重新立项（`re-underwrite`）；
  - `untested`：本季财报未提供检验该支柱的有效经营证据；
  - `retired`：已完成历史使命并主动关闭（注明日期与原因）。

### 3. 整体 Thesis 状态对齐规则（反漂移合成）
- 整体状态由**核心支柱（`priority: "core"`）**决定：
  - 若有 **1 个核心支柱处于 `impaired`**，整体状态不得优于 `watch`（除非写出有明确量化证据支撑的抵消理由）；
  - 若有 **≥2 个核心支柱处于 `impaired`**，整体状态必须设为 `impaired`；
  - 若有**任一核心支柱处于 `broken` 或 `changed`**，整体状态必须同步设为 `broken` 或 `changed`。
- **事件五词终判映射**：
  - `NO_CHANGE`（无变化）、`THESIS_STRENGTHENED`（强化）、`THESIS_WEAKENED`（削弱）、`THESIS_BROKEN`（证伪）、`NEW_THESIS`（新论点）。
  - 凡判 `THESIS_BROKEN` 或核心支柱进入 `impaired` / `broken`，必须触发 PM 升级通知与 `pm_handoffs`。

### 4. 股价反应必须先分解再作旁证
- **严禁拿财报后股价涨跌直接代替基本面信号**（例如股价涨了就说 thesis 强化、股价跌了就说 thesis 削弱）。
- 引用股价变动前，必须先结合 `price_reaction.py` 的相对表现（剔除大盘 `SPY/QQQ` 与板块 ETF beta），将个股超额波动归因于：盈利预测修正（EPS revision）、估值倍数重估、财报前买方持仓拥挤度或流动性因素。无法归因的股价异动只能列为待查问题。

### 5. 五种漂移模式（Drift Patterns）强制逐条排查
每次财报后校准必须逐条给出 **`有` / `无`** 判断并附一句解释：
1. **理由变了（The reason changed）**：当前继续看多/看空的理由是否已偏离最初立项时的核心支柱，却未做正式重估？
2. **催化剂洗白（Catalyst laundering）**：原定在本季财报兑现的催化剂未达标，是否被悄悄替换成定性口号而非新的带日期验证点？
3. **估值替代（Valuation substitution）**：当增长或利润率支柱走弱时，是否用“估值已经很便宜/P/E 低”来替原本的成长逻辑辩护？
4. **拿期限搪塞（Dismissal by horizon）**：面对连续出现的负面经营数据，是否一味以“短期季度噪音、我们看 3–5 年长期”为由拒绝下调支柱状态？
5. **拿保护当理由（Protection as reason）**：对于原本为博取上行空间而建仓的标的，是否把“账上净现金多/有回购托底”当成了继续持有的主要理由？

---

## 三、管理层指引兑现记录更新（`management-delta.md`）

1. **提取本季兑现数据**：
   - 对每一项上一季给出过官方指引的指标（营收、毛利率、营业利润/EPS 等），记录：`期间、指标、上季指引区间、上季指引中点、本季实际值、相对中点偏差（% 或 bps）、兑现结果（beat / in_line / miss）`。
2. **更新下一季新指引**：
   - 将本次财报新发布的下季/全年指引写入 `guidance_history` 新行（`actual` 留待下季填补）。
3. **校准管理层可信度评价**：
   - 判断管理层发言是否量化、是否与上季口径一致、是否直面问题；若出现悄悄更改 KPI 定义或选择性披露，必须在 `credibility_notes` 中记录并下调相关指引权重。

---

## 四、知识层修订草案结构（`knowledge-revision.json`）

阶段 3 生成的 `research/<D>-deep-<quarter>/knowledge-revision.json` 供启动 skill 的主智能体读取并调用 `knowledge_update` 与 `knowledge_append_verdict`：

```json
{
  "ticker": "META",
  "quarter": "FY26Q3",
  "release_date": "2026-10-28",
  "review_done": true,
  "escalate_to_pm": false,
  "verdict": {
    "conclusion": "THESIS_STRENGTHENED",
    "trigger": { "kind": "earnings", "ref": "FY26Q3:tp1" },
    "summary": "FY26Q3 广告展示量与均价双升带动收入超预期，AI 推荐提升转化率验证核心支柱 P1；2026 全年 Capex 上修但 OCF 充裕。",
    "progress": "substantive",
    "next_validation": {
      "what": "核查 FY26Q4 假日季广告 CPM 增速与 2027 年 Capex/总费用初步指引区间",
      "due": "2027-01-29"
    }
  },
  "agenda_ops": [
    {
      "op": "complete_validation",
      "match": "predictions.json"
    },
    {
      "op": "add_validation",
      "item": {
        "what": "跟踪 FY26Q4 广告均价增速与 2027 费用指引兑现",
        "due": "2027-01-29",
        "linked_pillar": "P1"
      }
    }
  ],
  "updates": {
    "thesis": {
      "reason": "FY26Q3 财报后支柱信号与证据台账更新",
      "evidence_refs": ["sec:0001326801-26-xxxx", "ae:doc_12345"],
      "ops": []
    },
    "debate": {
      "reason": "更新 AI 资本开支 ROI 与核心广告增速分歧的本季验证结果",
      "evidence_refs": ["sec:0001326801-26-xxxx"],
      "ops": []
    },
    "drivers": {
      "reason": "写入 FY26Q3 广告展示量、单价、营业利润率与 Capex 实际读数",
      "evidence_refs": ["sec:0001326801-26-xxxx"],
      "ops": []
    },
    "management": {
      "reason": "补填 FY26Q3 指引实际兑现值并登记 FY26Q4 新指引",
      "evidence_refs": ["sec:0001326801-26-xxxx"],
      "ops": []
    },
    "lessons": {
      "reason": "记录本次前瞻预测偏差原因",
      "evidence_refs": ["research/2026-10-28-deep-FY26Q3/reconcile.md"],
      "ops": []
    },
    "models": {
      "reason": "登记财报后更新模型 v02",
      "evidence_refs": ["model/META-model-v02.xlsx"],
      "ops": []
    }
  }
}
```
