# EPS 预期修正 vs 估值倍数归因方法论（`attribution.md`）

本文档定义 `dalton-weekly-review`（及初次覆盖历史分解）使用的股价变动“盈利预测修正 vs 估值倍数扩张/收缩”定量归因公式与判定规则，由 `scripts/attribution.py` 执行。

---

## 一、对数分解公式（数学严格闭合）

设分析窗口起点收盘价为 $P_{\text{base}}$，终点收盘价为 $P_{\text{end}}$（均采用复权收盘价以消除拆股与分红除权跳空）；窗口起点与终点的远期 12 个月一致预期每股收益分别为 $\text{EPS}_{\text{base}}$ 与 $\text{EPS}_{\text{end}}$（均采用 NTM 口径）。

定义远期动态市盈率：
$$\text{PE}_{\text{base}} = \frac{P_{\text{base}}}{\text{EPS}_{\text{base}}}, \quad \text{PE}_{\text{end}} = \frac{P_{\text{end}}}{\text{EPS}_{\text{end}}}$$

因为 $P = \text{EPS} \times \text{PE}$，对两端取自然对数可得精确恒等式：
$$\ln\left(\frac{P_{\text{end}}}{P_{\text{base}}}\right) = \ln\left(\frac{\text{EPS}_{\text{end}}}{\text{EPS}_{\text{base}}}\right) + \ln\left(\frac{\text{PE}_{\text{end}}}{\text{PE}_{\text{base}}}\right)$$

转换为百分点（percentage points, pp）表示：
- **股价对数变化（`log_return_pp`）**：$100 \times \ln(P_{\text{end}} / P_{\text{base}})$
- **EPS 预期贡献（`eps_contrib_pp`）**：$100 \times \ln(\text{EPS}_{\text{end}} / \text{EPS}_{\text{base}})$
- **估值倍数贡献（`pe_contrib_pp`）**：$100 \times \ln(\text{PE}_{\text{end}} / \text{PE}_{\text{base}})$

> **闭合校验（PS.18 P5-1 硬门槛）**：`eps_contrib_pp + pe_contrib_pp` 恒等于股价对数变化 `log_return_pp`，闭合误差 $< 0.1\text{pp}$（浮点精度误差 $< 10^{-12}\text{pp}$，四舍五入后误差 $\le 0.01\text{pp}$）。同时在输出表中展示简单收益率 $(P_{\text{end}}/P_{\text{base}} - 1) \times 100\%$ 供直观阅读。

---

## 二、NTM EPS 时间加权插值公式

为了消除财年切换时的跳变，远期 12 个月一致预期 $\text{EPS}_{\text{NTM}}$ 由本财年（`0y`）与下一财年（`+1y`）一致预期按本财年剩余天数加权插值：

$$\text{EPS}_{\text{NTM}}(t) = w(t) \cdot \text{EPS}_{0y}(t) + \bigl(1 - w(t)\bigr) \cdot \text{EPS}_{+1y}(t)$$

其中权重：
$$w(t) = \text{clamp}\left(\frac{\text{本财年结束日} - t}{365}, \; 0.0, \; 1.0\right)$$
（默认财年结束日为 `12-31`；非日历年公司按其财年结束月日计算，如 `06-30`。）

### 窗口起点（`base`）与终点（`end`）EPS 数据来源优先级
1. **首选：一致预期快照历史（`../<TICKER>/data/consensus/*.json`）**：
   - 取该工作区 `data/consensus/` 下时间戳离 `base` 最近的一份快照计算 $\text{EPS}_{\text{base}}$，离 `end` 最新的一份快照计算 $\text{EPS}_{\text{end}}$（PS.11 每晚预检与增量任务自动存档）。
2. **回退：一致预期接口的 `eps_trend`（`7daysAgo` / `30daysAgo`）**：
   - 当某公司尚无历史快照文件（如未建独立工作区的 Watch 异动公司）时，从 `eps_trend` 读取 `0y` 与 `+1y` 的 `current`（作为 `end`）和 `7daysAgo`（一周窗口正好对齐 `base`），并在输出表 `source_note` 中明确标注**“近似（7daysAgo）”**。

---

## 三、主导驱动判定规则（`driver`）

对有效计算出 `eps_contrib_pp`（记为 $E$）与 `pe_contrib_pp`（记为 $M$）的公司，按以下规则判定主导驱动：

1. **两项贡献同号（$E \times M \ge 0$）**：
   - 记总绝对贡献 $S = |E| + |M|$；
   - 若 $|E| / S \ge 70\%$：判定为 **`盈利驱动`**；
   - 若 $|M| / S \ge 70\%$：判定为 **`估值驱动`**；
   - 若两者占比均在 $30\% \sim 70\%$ 之间：判定为 **`两者`**。
2. **两项贡献异号（$E \times M < 0$，即盈利与估值反向运动）**：
   - 判定为 **`相抵`**，展示文案格式为：**`盈利 {E:+.1f}pp、倍数 {M:+.1f}pp 相抵`**（例如：`盈利 +3.2pp、倍数 -4.8pp 相抵`）。

---

## 四、负 EPS 或接近零 EPS 的特殊处理（`NM`）

当公司处于亏损或微利状态（$\text{EPS}_{\text{base}} \le 0.05$ 或 $\text{EPS}_{\text{end}} \le 0.05$）时，市盈率倍数无经济学意义且对数无定义：
- **不做对数分解**；
- `eps_contrib_pp` 与 `pe_contrib_pp` 置为 `null`；
- `driver` 统一输出：**`NM，改用 EV/Sales 定性`**。

---

## 五、输出表结构与知识层回写

周会稿第六节输出完整归因表：

| 公司 | 股价变化（简单 / 对数） | EPS 预期贡献（pp） | 倍数贡献（pp） | 主导驱动 | 盈利预测变化来源（哪家券商、哪个数据点） | 倍数变化解释（利率 / 行业风险溢价 / 远期增长预期 / 资金面；无明确逻辑写“原因暂缺”） |
|---|---|---|---|---|---|---|

- **知识层联动**：每周的归因结果通过 `Home/dalton/research/weekly-<end>/proposals/<TICKER>.json` 提案交由各公司分析师写入 `drivers` 文档的股价驱动历史记录，供季度复盘检验某只股票历史上主要由盈利修正还是估值重估推动。
