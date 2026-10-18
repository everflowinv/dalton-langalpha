# 公告监控表单清单与过滤规则（`watch-forms.md`）

本文档定义建议写入各公司知识层 `agenda.watch.filings`（`data/agenda.json`）的 SEC 表单监控清单与过滤阈值，供 PK.16 的 `sec` 探针过滤信号，并指导分析师区分“必须深读审阅”与“只记录不深读”的公告。

---

## 一、建议写入 `agenda.watch.filings` 的配置结构

初始化公司工作区（P1.3）或第一次运行 `dalton-filing-review` 时，建议在 `data/agenda.json` 的 `watch.filings` 中配置以下表单与阈值：

```json
{
  "forms": [
    "8-K",
    "8-K/A",
    "10-Q",
    "10-Q/A",
    "10-K",
    "10-K/A",
    "6-K",
    "20-F",
    "S-1",
    "S-3",
    "S-3ASR",
    "424B2",
    "424B5",
    "DEF 14A",
    "SC 13D",
    "SC 13D/A",
    "SC 13G",
    "SC 13G/A",
    "Form 4"
  ],
  "record_only_forms": [
    "FWP",
    "S-8",
    "144",
    "EFFECT"
  ],
  "form4_filter": {
    "roles": ["CEO", "Chief Executive Officer", "CFO", "Chief Financial Officer", "President", "Chairman", "10% Owner"],
    "min_transaction_usd": 5000000,
    "always_include_open_market_buy": true
  }
}
```

---

## 二、哪些公告需要深读（Read & Review）

以下公告命中后唤醒 `dalton-filing-review` 走完整五步审阅流程：

1. **定期报告**：
   - `10-Q`、`10-Q/A`、`10-K`、`10-K/A`（美股本土发行人）；
   - `20-F`、`20-F/A`（在美上市外国私人发行人年报）。
2. **重大事件临时报告**：
   - `8-K`、`8-K/A`（全部 Item 均需查看；若为 `Item 2.02` 财报公告且当季 `/dalton-earnings-flash` 快讯已跑，则只核对快讯数字与 8-K 附件 EX-99.1 一致性）；
   - `6-K`（在美上市外国私人发行人临时公告，重点审阅业绩、融资、重大合同与监管事项）。
3. **融资与资本结构文件**：
   - `S-1`、`S-3`、`S-3ASR`、`424B2`、`424B5`（股权增发、可转债、公司债发行定价补充文件，需重算严格净现金）。
4. **治理与股东委托书**：
   - `DEF 14A`（高管薪酬考核 KPI 调整、股权激励授权与股东提案）。
5. **大股东权益变动**：
   - `SC 13D`、`SC 13D/A`（全部必读）；
   - `SC 13G`、`SC 13G/A`（持股跨越 5%/10% 门槛或重要大股东大幅增减持）。
6. **高管与关键内部人交易（`Form 4`，满足任一条件才深读）**：
   - 交易人为 **CEO 或 CFO**（或创始人/董事长）；
   - 单笔或同日累计公开市场买卖（Transaction Code `P` 或 `S`）金额 **≥ 500 万美元（`$5,000,000`）**；
   - 任何董事或高管的**公开市场主动买入（Transaction Code `P`）**。
   - （其余普通员工 RSU 归属代扣税款 Code `F` 或小额 10b5-1 例行减持不深读。）

---

## 三、哪些公告不深读、只记录（Record Only）

以下例行文件不消耗全文阅读预算、不推送 PM，只在被唤醒时记录 accession 并以 `NO_CHANGE`（`progress="none"`）快速收尾：

- **`FWP`（Free Writing Prospectus，自由书写招股书）**：债券或股票发行过程中的路演条款单，最终以 `424B2` / `424B5` 定价文件为准；
- **`S-8`**：员工持股计划例行注册登记表；
- **`Form 144`**：拟出售受限证券意向通知（以实际发生后的 `Form 4` 核实为准）；
- **未达阈值的普通 `Form 4`**：非 CEO/CFO、非公开市场买入（如仅 Code `F`/`M`）且金额低于 500 万美元的例行申报。
