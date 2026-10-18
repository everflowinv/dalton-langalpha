# 财报前瞻检索与取证规程（`retrieval.md`）

本文件定义 `dalton-earnings-preview`（PS.8）的证据记录字段、内部研究库四个必查入口、分阶段检索策略、图片核对硬规则、一轮公开 X 检索边界与停止降级规则。

---

## 一、取证记录字段与覆盖日志

### 1. 证据笔记必留字段

边查边把已采用的原文短摘和上下文写入 `research/<D>-preview-<季度>/evidence-notes.md`。每条证据记录必须保留以下 12 项字段：
1. **来源路由（`route`）**：`alphaengine` / `sales-notes` / `tmtb` / `funda-ai` / `estimates` / `sec`（及公开社媒 `x_api.search_posts`）
2. **原始 ID / URL**：如 `doc_id`、`mail:<ID>`、`tmtb:<ID>`、`funda:<ID>`、SEC accession 或原帖 URL
3. **文件标题**
4. **作者与真实身份**：区分买方原话、机构 sales 转述买方、机构 sales 个人观点、卖方分析师、独立研究作者（FUNDA AI / TMTB）、公司管理层
5. **发布日期与时间戳**（必须 `<= as-of` 冻结时刻）
6. **涉及财季与期间口径**（区分 FY / CY、单季 Q / 累计 YTD / MTD）
7. **正文定位**：页码、章节标题、表格名、图片 ID 或电话会段落
8. **原始来源链**：若为转述，写明上游原始报告或调研出处（同一上游只计 1 条独立证据）
9. **观点 / 数字类型**：`company_guide` / `street_consensus` / `broker_forecast` / `buyside_bogey` / `high_freq_data` / `debate_claim`
10. **单位与会计/业务口径**：币种、GAAP / Non-GAAP、固定汇率（CC）或分部口径
11. **支持的结论**
12. **已读正文 / 已核原图状态**：`full_text_read: true/false`、`image_verified: true/false/na`

> **硬约束**：只读了搜索摘要（snippet）而未 `read` / `get_document` 读正文的候选材料，**严禁进入最终事实清单与正文引用**。

### 2. 检索覆盖记录表

在报告末尾“来源与覆盖”章节逐项列出 **`AlphaEngine`、`sales-notes`、`TMTB`、`FUNDA AI`、`行业前瞻`、`X`** 六个通道的覆盖状态：
- 查询词与时间窗口（`start` 至 `end`）
- 实际精读篇数
- 命中的最新文档日期
- 通道状态：`找到有效证据` / `已查无相关结果` / `通路失败（注明具体错误或额度状态）`

本地缓存无结果或日期陈旧时，不能把“缓存未命中”写成“源头无此信息”。

---

## 二、四个内部研究库入口与一致预期接口

在 PTC 沙箱中调用 `researchdata` 前，本线程先调一次 `researchdata.catalog()`。

```python
# 1. AlphaEngine（卖方研报、专家纪要、行业前瞻）
researchdata.research(
    route="alphaengine",
    operation="search_library",
    query="<公司名或代码>",
    start="<起日 YYYY-MM-DD>",
    end="<止日 YYYY-MM-DD>",
    limit=20,
    params={"sort": "relevance"},
)
researchdata.research(
    route="alphaengine",
    operation="get_document",
    params={"doc_id": "<搜索返回的 doc_id>"},
)

# 2. Sales Notes（机构销售台邮件与买方反馈）
researchdata.research(
    route="sales-notes",
    query="<公司名或代码>",
    start="<起日 YYYY-MM-DD>",
    end="<止日 YYYY-MM-DD>",
    limit=20,
    params={"media": "all"},
)
researchdata.research(
    route="sales-notes",
    operation="read",
    document="<返回的 mail:ID>",
)

# 3. TMTB（TMT Breakout 独立买方/科技评论）
researchdata.research(
    route="tmtb",
    query="<公司名或代码>",
    start="<起日 YYYY-MM-DD>",
    end="<止日 YYYY-MM-DD>",
    limit=20,
    params={"media": "all"},
)
researchdata.research(
    route="tmtb",
    operation="read",
    document="<返回的 tmtb:ID>",
)

# 4. FUNDA AI（独立基本面研究与高频图表）
researchdata.research(
    route="funda-ai",
    query="<公司名或代码>",
    start="<起日 YYYY-MM-DD>",
    end="<止日 YYYY-MM-DD>",
    limit=20,
    params={"media": "all"},
)
researchdata.research(
    route="funda-ai",
    operation="read",
    document="<返回的 funda:ID>",
)

# 5. 一致预期与历史财报反应快照（必须落盘到 data/consensus/）
researchdata.finance(route="estimates", ticker="<TICKER>")
```

### 各入口使用要点

- **公司名与 Ticker 双查**：四个研究入口均须用“公司名（含常用中英文别名）”与“Ticker”分别检索，避免漏召回。
- **AlphaEngine**：保持 `sort="relevance"`，时间范围放在 `start/end`；需要按来源或行业过滤时先调 `operation="list_filters"` 核实合法枚举。注意 `Sector Specialist Commentary`、`TECH SKETCH`、`Special Situations Daily` 等销售台评论可能归在研报分类下，必须读正文核对作者属于 sales/trading 还是 research analyst。
- **Sales Notes**：默认查原始邮件（排除 TMTB）；`layer="digest"` 仅作定位索引，引用前必须 `read` 回原始邮件 `mail:<ID>` 核实上下文与说话人身份。
- **TMTB**：本地库代表已入库覆盖；若需补充最新几天的 TMTB，可在 AlphaEngine 中指定 `source_names: ["TMT Breakout"]` 补查，相同文档与本地缓存去重，不重复计票。非 TMT 行业公司若无覆盖如实标注即可，不强凑材料。
- **FUNDA AI**：属于独立研究来源，观点署名 `FUNDA AI`，**不自动计为卖方一致预期、直接买方证言或公司一手披露**；若与 TMTB、sales 邮件或 AlphaEngine 引用同一份上游数据/研报，只算 **1 条来源链**。原邮件若仅含节选或通知，不得声称已读网页全文。
- **融合检索辅助**：`route="all", params={"sources": ["sales-notes", "tmtb", "funda-ai"]}` 可用于补充交叉召回，但**不能替代对四个入口的分别检索与覆盖核对**。

---

## 三、分阶段检索策略

1. **阶段 1（预期门槛与高频 Color）**：
   - 先对公司名和 ticker 做宽检索，再根据已读线索挑选针对性词拆开补查：
     - 英文：`bogey`、`whisper`、`buy-side expectations`、`hurdle`、`into the print`、`channel checks`、`weekly tracking`；
     - 中文：`买方预期`、`业绩门槛`、`排产`、`出货`、`动销`、`流量`、`价格`。
   - 每次查询只带少量关键词，避免把十几个词塞进同一个 query 导致召回退化。
2. **阶段 2（个股与行业 Preview）**：
   - 查 `<公司名/Ticker> + earnings preview / 业绩前瞻`；
   - 另查 `<行业/板块名> + earnings preview / 季度前瞻`（**不强制标题含公司名**）。拿到行业前瞻报告后，在正文、跨公司比较表与附图中搜索目标公司名称、业务别名与 ticker。优先采用最新日期版本，旧版仅用于观察预期上修/下修轨迹。
3. **阶段 3（Debate 机制补查）**：
   - 复用阶段 1–2 已读证据，按因果机制归并多空分歧；仅当某个关键驱动变量缺乏正反方证据时，围绕该具体变量做一次定向补查，不无限循环重扫。

---

## 四、图片与全文核对硬规则

1. **默认开启图片检索**：`sales-notes`、`tmtb`、`funda-ai` 检索统一带 `params={"media": "all"}`。若正文未命中但关键表格可能在图片里，补查 `params={"media": "images"}`。
2. **回读父文档与原图核验**：
   - 调用 `read(document="image:<id>")` 获取原图及 `parent_document_id`，必须回读父文档确认发布日期、作者身份与上下文；
   - **所有来自图片表格的数字，必须查看原图逐项核对：正负号、小数点、单位（百万/十亿/bps/%）、期间标签（FY/CY、Q/YTD）**；
   - **严禁直接采信 `description_verified=false` 的自动识图文字描述作为数字证据**；若原图模糊或附件缺失，必须标注缺口，不得盲信文本摘要中的二手数字。
3. **正文完整分页读取**：
   - 正文 `read` / `get_document` 遇到截断或 `next_offset` 时，必须继续分页读取直至拿全核心章节与表格；仅看到标题或搜索摘要出现公司名，绝不等于已读过该机构的观点。

---

## 五、一轮公开 X 检索：找 Narrative 共性，不做情绪投票

1. **调用方式与时间窗**：
   - 使用公开 X 检索工具（`x_api.search_posts`），设置 `from_date`（近 90 天起点）与 `to_date`（严格等于 `as-of` 冻结日期），按需开启图片理解。
   - **历史回测防泄漏**：必须逐条复核帖子实际发布时间戳，严格剔除 `as-of` 之后发布的帖子。
2. **保密与合规边界**：
   - 公开 X 查询词只允许包含公开公司名、ticker、行业术语与公开业务问题；**严禁将内部 sales 邮件原文、私有 buyside bogey 数字或未公开研报段落放入公共搜索框**。
3. **“一轮”的边界与去重规则**：
   - “一轮”指围绕公司财报预期与核心多空因果链做一次集中检索（必要时对遗漏机制加 1–2 条定向查询），不扩展 Reddit、雪球或员工匿名社区。
   - 按原帖 URL / status ID 去重，并核查其是否只是转述同一份卖方研报、TMTB 或新闻：若多位 X 作者引用同一份卖方调查，只能表述为“该调查在公开讨论中被广泛传播”，**只能算 1 条原始证据链**。
   - 不凭账号头像或语气把普通发帖人认定为“机构买方”（核实不了背景的一律写“X 作者”）；纯喊单、纯技术图形点位、无业务因果机制的目标价一律剔除。

---

## 六、停止与降级规则

- 完成内部四入口分别检索、行业前瞻排查与一轮公开 X 后，即以已核实的证据成稿。
- 若存在明确缺口，仅围绕该缺口用可用备用路径补查一次；遇到工具报错、凭证失效或配额告警时，如实记录到“来源与覆盖”章节（区分“已查无相关结果”与“通路失败”），不绕过权限、不编造替代数字。
