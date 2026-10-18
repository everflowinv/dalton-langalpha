# 专题研究默认信息集群（Default Research Clusters）

在 Phase 0 研究规划阶段，若 PM 未指定特殊维度，默认按以下 **5 个投资研究信息集群（C1–C5）** 拆解并派发并行 `dalton-evidence` 子代理。每个集群应生成 5–8 条中英文检索词，并在广度检索后精读 3–5 篇最高质量的全文材料。

---

## 1. 投资与产业专题默认五集群（C1–C5）

### C1：财务数据 + 估值（Financials & Valuation）
- **核心问题**：行业/赛道规模（TAM/SAM）的实际增速、毛利率与营业利润率结构、单位经济模型（Unit Economics）、ROIC 与资本开支强度、可比公司估值倍数（P/E、EV/EBITDA、EV/Sales）与历史分位。
- **优先信源**：SEC 10-K/10-Q/20-F 分部披露、`researchdata` `finance`（财务比率与可比公司表）、一致预期修正历史。
- **必核指标**：收入口径差异（总额法 vs 净额法、GAAP vs Non-GAAP）、CapEx / Revenue、FCF 转化率、可比公司 2 年远期估值溢价/折价原因。

### C2：竞争格局 + 行业趋势（Competitive Landscape & Industry Trends）
- **核心问题**：存量份额与增量份额（Incremental Share）归属、技术路线演进、客户切换成本、上下游议价权（供应商集中度与客户集中度）、新进入者壁垒与价格战风险。
- **优先信源**：竞争对手 10-K/法说会纪要、第三方行业统计（IDC/Gartner/Mercury/SemiAnalysis 等）、`researchdata.research(route="tmtb")`、`researchdata.research(route="funda-ai")`、`researchdata.research(route="guidepoint")` 产业链上下游访谈。
- **必核指标**：CR3/CR5 市场份额变化（带具体季度）、ASP 趋势、大客户二供/自研进展、交期（Lead Time）与库存水位。

### C3：催化剂 + 风险（Catalysts & Risks）
- **核心问题**：未来 3–12 个月有哪些可验证的重大事件（新品发布、大客户定点、产能爬坡、监管审批、合同续签）？哪些宏观、地缘、监管或技术替代风险会直接证伪当前多头/空头逻辑？
- **优先信源**：公司法说会路线图、监管机构（FTC/DOJ/BIS/欧盟）公告、行业展会日程、供应链量产排期。
- **必核指标**：催化剂发生时间窗口、验证指标与量化阈值、尾部风险触发概率与利润表冲击幅度。

### C4：管理层 + 治理（Management & Governance）
- **核心问题**：核心高管背景与过往兑现记录（Guidance Track Record）、高管薪酬考核挂钩指标（DEF 14A）、资本配置优先级（研发 vs 扩产 vs 回购 vs 并购）、内部人买卖（Form 4）与关键技术/销售主管流失情况。
- **优先信源**：DEF 14A（Proxy Statement）、Form 4、历次财报电话会问答记录、高管公开访谈。
- **必核指标**：过去 8 个季度指引命中率、SBC 占收入比与摊薄率、ROIC 与管理层 LTIP 挂钩权重。

### C5：牛熊观点 + 卖方研究（Bull/Bear Views & Sell-Side Research）
- **核心问题**：当前买方与卖方的主流叙事是什么？多头最依赖的 2–3 个核心假设是什么？空头最犀利的证伪点是什么？市场分歧集中在哪个具体的经营驱动变量（Driver）上？
- **优先信源**：`researchdata.research(route="alphaengine")`（近 6 个月大行深度报告与评级变更）、`researchdata.research(route="sales-notes")`（买方路演反馈）、`researchdata.community` 专业投资者深度讨论。
- **必核指标**：卖方目标价区间与隐含倍数、多空双方对核心 Driver（如次年出货量/毛利率）的具体数字分歧。

---

## 2. 政策 / 地缘 / 监管专题可选六集群（Geopolitical / Regulatory Variant）

当专题聚焦于关税、出口管制、反垄断或产业政策时，可将集群调整为：
1. **G1 法条与监管原文**：法案/行政令原始条款、生效日期、豁免期与执法机构管辖权。
2. **G2 受影响产品与收入敞口**：各覆盖公司受限产品线占总收入/毛利的具体比例。
3. **G3 供应链绕道与合规替代方案**：海外建厂、降规版产品、第三方转口成本与良率损耗。
4. **G4 竞争对手与本土替代受益方**：禁令/关税下的份额重分配赢家与输家。
5. **G5 历史先例与执行力度**：过往同类政策的实际执法尺度、漏洞封堵周期与公司应对效果。
6. **G6 情景推演与估值折价**：温和执行 / 基准执行 / 全面升级三档情景对 EPS 与目标倍数的影响。
