# 知识层第 2 版（v2）结构化写入完整样例

本文档提供通过 `src/dalton/knowledge/schemas/*.schema.json` 全部字段与跨文档一致性校验的标准写入样例，供 `dalton-bullbear`、`dalton-call`、`dalton-earnings-preview`、`dalton-earnings-flash`、`dalton-thesis-delta` 等技能直接对照。

> **写入前注意**：
> 1. 所有对象均禁止 schema 之外的多余字段（`additionalProperties: false`），写任何字段名、枚举值前以本样例和 `src/dalton/knowledge/schemas/*.schema.json` 为准。
> 2. 若 `thesis` 引用了 `models`（如 `calc.method = "reference"` 指向模型单元格）、`agenda`（如 `actionability = "wait_evidence"` 的 `trigger_refs`）或 `debate`（如 `states[].debate_ids`、`breakeven_verdict[].debate_ids`），必须**先**写入或更新对应的 `models` / `agenda` / `debate` 文档，**再**调用 `knowledge_update(doc="thesis", ...)`，否则跨文档引用校验会拒绝写入。

---

## 1. `models` 写入样例（当 `calc.method = "reference"` 引用模型单元格时先行登记）

```json
{
  "schema_version": 1,
  "current": "v1",
  "versions": [
    {
      "id": "v1",
      "path": "model/META-model-v01.xlsx",
      "created_at": "2026-10-18T12:00:00Z",
      "reason": "建立三情景估值模型",
      "recalculated": true,
      "key_outputs": [
        {
          "name": "DCF 基准每股价值",
          "cell": "DCF!G18",
          "value": 675.5,
          "unit": "USD",
          "period": "FY2027"
        }
      ]
    }
  ]
}
```

---

## 2. `agenda` 写入样例（验证项 `next_validations` 与唤醒条件 `wake_conditions`）

- `actionability = "wait_evidence"` 时，`house_view.trigger_refs` 必须指向 `agenda` 中带明确日期的条目（`V-xxx`、`W*`、`Q-xxx` 或 `kind = "date"` 的 `C*`）。
- `wake_conditions` 中：
  - `kind = "price_level"` 时，必填 `price_level: {"band": {"lo", "hi", "unit"}, "transition": "enter" | "exit" | "either"}` 与正整数 `underwriting_version`（等于当前 `thesis.underwriting.scenario_version`）；
  - `kind = "date"` 时，到期时间写在 `params.date`（`YYYY-MM-DD` 或带时区偏移的 ISO 时刻），不要写在条件顶层。

```json
{
  "schema_version": 2,
  "as_of": "2026-10-18T12:00:00Z",
  "open_questions": [],
  "next_validations": [
    {
      "id": "V-001",
      "what": "验证 FY26Q3 广告单价同比增速是否维持在 6% 以上",
      "due": "2026-10-30",
      "source": "FY26Q3 10-Q 与业绩电话会",
      "expected": "广告单价同比增速 >= 6%",
      "linked": {
        "pillars": ["P1"],
        "debates": ["D1"]
      },
      "status": "pending"
    }
  ],
  "watch": {
    "events": [
      {
        "id": "W1",
        "type": "earnings",
        "date": "2026-10-30",
        "date_status": "confirmed",
        "source": "公司 IR 公告",
        "plan": "T-3 财报前瞻 / T0 快讯 / T+1 深度点评"
      }
    ]
  },
  "wake_conditions": [
    {
      "id": "C1",
      "kind": "price_level",
      "price_level": {
        "band": {"lo": 500.0, "hi": 638.0, "unit": "USD"},
        "transition": "exit"
      },
      "direction": "review",
      "transmission": "both",
      "underwriting_version": 1,
      "action": "跑 dalton-thesis-delta 复核三情景赔率卡",
      "enabled": true
    },
    {
      "id": "C2",
      "kind": "date",
      "params": {
        "date": "2026-10-30"
      },
      "direction": "review",
      "transmission": "eps_only",
      "action": "核对 FY26Q3 广告单价同比增速是否兑现",
      "enabled": true
    }
  ]
}
```

---

## 3. `debate` 写入样例（含 `debates[].status = "open"` 必填字段与财报前冻结的 `expectations[]`）

- `debates[].status = "open"` 的分歧必填：`why_it_matters`、`rank`（从 1 开始的正整数）、`transmission`（`"eps_only"` | `"multiple_only"` | `"both"`）、`market_lean`（`{"side": "bull" | "bear" | "split" | "unknown", "evidence": "..."}`）。
- `expectations[]`（最多 3 条）：
  - `street` / `buyside` / `house` 每项为 `null` 或 `{"value": 数字或 {"lo", "hi"} 或 null, "basis": "GAAP" | "non_GAAP" | ..., "as_of": "...", "sources": [...], "note": null, "confidence": "high" | "medium" | "low"}`；
  - 若非空条目的 `basis` 不一致或含 `"unknown"`，`gap` 必须填 `null`（服务端不计算预期差，只给警告，不会拒写）；
  - 财报前瞻冻结时：`frozen_pre_event: true`、`reconstructed: false`，并填写非空 `event_id`、`t0` 及严格早于 `t0` 的 `published_at`、`acquired_at`、`frozen_at`（首次标冻结时文档顶层 `as_of` 也必须严格早于 `t0`；已冻结条目在后续更新其他字段时，文档顶层 `as_of` 可以晚于 `t0`，但冻结条目本身一字不改）。

```json
{
  "schema_version": 2,
  "as_of": "2026-10-18T12:00:00Z",
  "debates": [
    {
      "id": "D1",
      "variable": "FY26Q3 广告单价同比增速与 FY27 盈利弹性",
      "question": "AI 推荐模型升级能否在 FY26Q3–FY27 持续推高广告转化率与单价",
      "why_it_matters": "广告单价每变动 1 个百分点影响 FY27 EPS 约 0.8 美元，直接决定基准情景能否站上 30 美元 EPS",
      "rank": 1,
      "transmission": "eps_only",
      "expectations": [
        {
          "metric": "广告单价同比增速",
          "period": "3Q26",
          "unit": "pct",
          "street": {
            "value": 6.0,
            "basis": "GAAP",
            "as_of": "2026-10-18",
            "sources": ["E-20261018-02"],
            "confidence": "medium"
          },
          "buyside": {
            "value": {"lo": 6.5, "hi": 7.5, "unit": "pct"},
            "basis": "GAAP",
            "as_of": "2026-10-18",
            "sources": ["E-20261018-03"],
            "confidence": "medium"
          },
          "house": {
            "value": {"lo": 8.0, "hi": 9.5, "unit": "pct"},
            "basis": "GAAP",
            "as_of": "2026-10-18",
            "sources": ["E-20261018-01"],
            "confidence": "high"
          },
          "gap": {"lo": 2.0, "hi": 3.5, "unit": "pct"},
          "frozen_pre_event": true,
          "event_id": "META-3Q26-earnings",
          "t0": "2026-10-30T20:05:00Z",
          "published_at": "2026-10-18T09:00:00Z",
          "acquired_at": "2026-10-18T10:00:00Z",
          "frozen_at": "2026-10-18T11:30:00Z",
          "reconstructed": false
        }
      ],
      "bull": {
        "case": "Reels 与消息广告加载率提升叠加 AI 转化率改善，推动单价增速超 8%",
        "who": "多头买方与头部互联网卖方",
        "evidence_ids": ["E-20261018-01"]
      },
      "bear": {
        "case": "跨境电商广告主投放增速见顶，高基数下广告单价增速回落至 4%–5%",
        "who": "谨慎派卖方",
        "evidence_ids": ["E-20261018-02"]
      },
      "strongest_rebuttal": {
        "against": "my_view",
        "text": "若北美宏观消费转弱，品牌广告预算缩减可能抵消 AI 转化率带来的单价提升"
      },
      "my_view": {
        "side": "bull",
        "confidence": "medium",
        "text": "广告主 ROI 数据显示转化率仍在提升，单价增速有望保持在 8% 以上"
      },
      "market_lean": {
        "side": "bull",
        "evidence": "卖方一致预期已上修至 6%，买方主流预期在 6.5%–7.5% 区间"
      },
      "flip_condition": {
        "text": "连续两个季度广告单价同比增速低于 4%",
        "driver_ids": ["K1"]
      },
      "linked_pillars": ["P1"],
      "status": "open",
      "opened_at": "2026-10-01T09:00:00Z",
      "last_reviewed_at": "2026-10-18T12:00:00Z",
      "history": [
        {
          "at": "2026-10-18T12:00:00Z",
          "change": "补充 3Q26 广告单价同比增速预期差并完成财报前冻结",
          "reason": "财报前瞻锁定三方预期",
          "evidence_ids": ["E-20261018-01", "E-20261018-02", "E-20261018-03"]
        }
      ]
    }
  ]
}
```

---

## 4. `thesis` 第 2 版完整写入样例（含 `market_view`、`house_view`、`underwriting`）

- `underwriting.stress`（压力情景）与 `borrow_cost_annual`（年化借券成本）：当 `house_view.actionability` 为 `"act_now"` 时必填 `underwriting.stress`（含 `calc`、`condition`、`needs_financing`；空头用于刻画逼空压力情景，多头用于刻画极端压力情景）；当 `house_view.stance` 为 `"short"` 时还须填写非空的 `borrow_cost_annual`（未知时 `actionability` 只能设为 `"wait_evidence"` 或 `"pass"`），多头 `borrow_cost_annual` 填 `null`。

```json
{
  "schema_version": 2,
  "as_of": "2026-10-18T12:00:00Z",
  "one_liner": "AI 推荐模型持续提升广告转化率与单价，当前股价具备明确赔率优势",
  "aggregate_status": "intact",
  "house_view": {
    "stance": "long",
    "conviction": "medium",
    "horizon_months": 12,
    "actionability": "act_now",
    "entry_band": null,
    "binding_constraint": "none",
    "binding_constraint_text": null,
    "flip_conditions": {
      "upgrade_if": "FY26Q3 广告单价同比增速突破 10% 且 2027 年资本开支指引增速低于 15%",
      "downgrade_if": "广告单价同比增速跌破 4% 或核心广告营业利润率同比下滑超过 200bp"
    },
    "trigger_refs": ["V-001"],
    "summary": "看好核心广告业务在 AI 推荐模型驱动下的单价与时长双升，12 个月维度具备显著正期望回报。",
    "variant_perception": "市场担心资本开支挤压自由现金流，低估了广告转化率提升对 2027 年 EPS 的拉动幅度。",
    "position_suggestion": "core",
    "updated_at": "2026-10-18T12:00:00Z"
  },
  "house_view_history": [],
  "market_view": {
    "summary": "卖方一致预期看好广告稳健增长，但对 2027 年资本开支节奏仍有分歧。",
    "as_of": "2026-10-18T12:00:00Z",
    "updated_at": "2026-10-18T12:00:00Z",
    "observations": [
      {
        "claim": "卖方一致预期 FY2027 营业收入同比增长 14%，Non-GAAP EPS 约 28.5 美元",
        "who": "主流卖方一致预期",
        "as_of": "2026-10-18",
        "sources": ["E-20261018-02"],
        "confidence": "medium"
      },
      {
        "claim": "买方普遍担忧 2027 年 AI 基础设施资本开支突破 550 亿美元",
        "who": "多策略买方反馈",
        "as_of": "2026-10-18",
        "sources": ["E-20261018-03"],
        "confidence": "medium"
      }
    ],
    "positioning": "balanced",
    "positioning_sources": ["E-20261018-03"]
  },
  "underwriting": {
    "anchor": {
      "price": 580.0,
      "price_at": "2026-10-18",
      "currency": "USD",
      "source": "NASDAQ 收盘价"
    },
    "scenario_version": 1,
    "fiscal_year_end": "12-31",
    "states": [
      {
        "name": "bear",
        "condition": "广告单价同比增速回落至 4% 且资本开支超预期推升折旧",
        "summary": "宏观放缓叠加折旧压力",
        "debate_ids": ["D1"],
        "calc": {
          "method": "eps_pe",
          "eps": {"lo": 24.0, "hi": 25.0, "unit": "USD"},
          "multiple": {"lo": 19.0, "hi": 20.0, "unit": "x"},
          "basis": "non_GAAP",
          "period": "FY2027",
          "sources": ["E-20261018-01"]
        },
        "cash_return_per_share": 2.0
      },
      {
        "name": "base",
        "condition": "广告单价维持 7%–8% 增长，营业利润率保持稳健",
        "summary": "核心广告稳健兑现",
        "debate_ids": ["D1"],
        "calc": {
          "method": "eps_pe",
          "eps": {"lo": 29.0, "hi": 31.0, "unit": "USD"},
          "multiple": {"lo": 22.0, "hi": 23.0, "unit": "x"},
          "basis": "non_GAAP",
          "period": "FY2027",
          "sources": ["E-20261018-01"]
        },
        "cash_return_per_share": 2.0
      },
      {
        "name": "bull",
        "condition": "AI 推荐模型带来广告转化率跃升，单价增速超 10%",
        "summary": "AI 变现全面超预期",
        "debate_ids": ["D1"],
        "calc": {
          "method": "eps_pe",
          "eps": {"lo": 34.0, "hi": 36.0, "unit": "USD"},
          "multiple": {"lo": 24.0, "hi": 25.0, "unit": "x"},
          "basis": "non_GAAP",
          "period": "FY2027",
          "sources": ["E-20261018-01"]
        },
        "cash_return_per_share": 2.0
      }
    ],
    "stress": {
      "condition": "北美广告支出萎缩 10% 且反垄断罚款落地",
      "calc": {
        "method": "eps_pe",
        "eps": {"lo": 20.0, "hi": 21.0, "unit": "USD"},
        "multiple": {"lo": 16.0, "hi": 17.0, "unit": "x"},
        "basis": "non_GAAP",
        "period": "FY2027",
        "sources": ["E-20261018-01"]
      },
      "needs_financing": "no"
    },
    "protection": {
      "survival": "账面净现金逾 400 亿美元，年度自由现金流超过 400 亿美元，极端压力下无需外部融资",
      "needs_financing": "no",
      "value": "核心社交网络日活用户底盘稳固，16 倍底部市盈率加净现金构成坚实估值底座",
      "value_range": {"lo": 430.0, "hi": 460.0, "unit": "USD"},
      "path": "持续大额股份回购与季度分红支撑每股内在价值",
      "failure": "核心应用日活用户出现结构性流失或净现金转为净负债"
    },
    "weight_bounds": {
      "bear": {"lo": 0.15, "hi": 0.25},
      "base": {"lo": 0.45, "hi": 0.55},
      "bull": {"lo": 0.25, "hi": 0.35},
      "rationale": "广告主转化率改善已在连续两个季度财报中得到验证，基准与乐观情景合计权重不低于 75%",
      "sources": ["E-20261018-01"]
    },
    "breakeven_verdict": [
      {
        "side": "long",
        "verdict": "clearly_above",
        "b_row": 0.5,
        "basis": "当基准情景权重为 50% 时，盈亏平衡所需乐观情景权重远低于我方判断下限 25%",
        "debate_ids": ["D1"]
      }
    ],
    "hurdles": {
      "long_hurdle_annual": 0.15,
      "short_hurdle_annual": 0.15,
      "min_total_return": 0.08,
      "max_adverse_move_act_now": 0.25,
      "max_bear_loss_act_now": 0.25,
      "horizon_months": 12
    },
    "borrow_cost_annual": null,
    "dividend_yield": 0.003,
    "rerating_observed": false,
    "rerating_sources": []
  },
  "pillars": [
    {
      "id": "P1",
      "claim": "AI 推荐模型持续提升广告转化率与单价",
      "priority": "core",
      "baseline": {
        "text": "2026 年二季度广告单价同比增长 8%",
        "as_of": "2026-08-01"
      },
      "expected_path": "未来四个季度广告单价同比增速保持在 6%–10% 区间",
      "thresholds": {
        "confirm": [
          {
            "id": "T1",
            "metric": "广告单价同比增速",
            "operator": ">=",
            "value": 8.0,
            "unit": "pct",
            "period": "FY2027",
            "label": "estimate"
          }
        ],
        "warn": [
          {
            "id": "T2",
            "metric": "广告单价同比增速",
            "operator": "<",
            "value": 5.0,
            "unit": "pct",
            "period": "FY2027",
            "label": "estimate"
          }
        ],
        "break": [
          {
            "id": "T3",
            "metric": "广告单价同比增速",
            "operator": "<",
            "value": 2.0,
            "unit": "pct",
            "period": "FY2027",
            "label": "estimate"
          }
        ]
      },
      "status": "intact",
      "next_proof_point": {
        "what": "验证 FY26Q3 广告单价同比增速是否维持在 6% 以上",
        "due": "2026-10-30",
        "source": "FY26Q3 10-Q",
        "validation_id": "V-001"
      },
      "provenance": "draft",
      "created_at": "2026-10-01T09:00:00Z"
    }
  ],
  "evidence": [
    {
      "id": "E-20261018-01",
      "at": "2026-10-18T10:00:00Z",
      "period": "FY2027",
      "source": {
        "type": "filing",
        "identity": "META FY2026 Q2 10-Q 与分部经营数据",
        "tier": 1,
        "ref": {
          "kind": "filing",
          "form": "10-Q",
          "period": "2Q26",
          "accession": "0001326801-26-000042"
        }
      },
      "fact": "2026 年二季度广告展示量同比增长 10%，平均广告单价同比增长 10%，核心应用营业利润率达 50%。",
      "pillar_ids": ["P1"],
      "signal": "confirming",
      "quality": "high"
    },
    {
      "id": "E-20261018-02",
      "at": "2026-10-18T10:30:00Z",
      "period": "FY2027",
      "source": {
        "type": "consensus",
        "identity": "Visible Alpha 卖方一致预期汇总",
        "tier": 3,
        "ref": {
          "kind": "file",
          "path": "research/2026-10-18-va-consensus.json"
        }
      },
      "fact": "卖方一致预期 3Q26 广告单价同比增长 6.0%，FY2027 Non-GAAP EPS 约 28.5 美元。",
      "pillar_ids": ["P1"],
      "signal": "neutral",
      "quality": "medium"
    },
    {
      "id": "E-20261018-03",
      "at": "2026-10-18T11:00:00Z",
      "period": "FY2027",
      "source": {
        "type": "market_color",
        "identity": "买方台账与卖方销售交易台仓位反馈",
        "tier": 4,
        "ref": {
          "kind": "file",
          "path": "research/2026-10-18-positioning-notes.md"
        }
      },
      "fact": "买方对 3Q26 广告单价增速预期集中在 6.5%–7.5%，多空仓位相对均衡。",
      "pillar_ids": [],
      "signal": "neutral",
      "quality": "medium"
    }
  ],
  "official": null
}
```
