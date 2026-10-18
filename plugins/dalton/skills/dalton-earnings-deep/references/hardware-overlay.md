# 半导体与硬件财报深度点评叠加层（`hardware-overlay.md`）

本文件在 [`eight-sections.md`](eight-sections.md) 八节标准结构之上，叠加**半导体与硬件行业**专属的外部核验清单、分章节深化项、A–G 七类细分业务子模板与专业术语规范。

---

## 一、适用对象（典型 Ticker）

- **AI 加速器与计算芯片**：`NVDA`、`AMD`、`INTC`、`ARM`、`QCOM`
- **定制 ASIC、以太网交换与高速互联**：`AVGO`、`MRVL`、`ANET`、`CRDO`、`ALAB`
- **晶圆代工与先进封装**：`TSM`、`ASE`、`AMKR`
- **半导体设备与材料（WFE）**：`ASML`、`AMAT`、`LRCX`、`KLAC`、`ENTG`、`ACLS`、`AEHR`
- **存储芯片（Memory）**：`MU`
- **模拟、功率与嵌入式半导体**：`TXN`、`ADI`、`ON`、`MCHP`、`NXPI`、`STM`
- **AI 服务器与硬件系统**：`SMCI`、`DELL`、`HPE`

> 注：`AAPL`、`META`、`TSLA` 等若研究重心在消费电子、广告或整车，默认走通用八节基座；仅当分析重点聚焦其自研芯片/硬件供应链时按需参考本叠加层。

---

## 二、外部与产业链必查五项信号

撰写半导体/硬件深度财报点评时，除公司自身披露外，必须交叉核验以下五类产业链信号：

1. **超大规模云厂商（Hyperscaler）Capex**：Meta、Microsoft、Alphabet（Google）、Amazon 最新季度的资本开支实际值、全年/下年 Capex 指引及 AI 基础设施占比表述，判断对上游算力芯片与网络订单可见度的传导。
2. **晶圆代工与先进封装产能动态**：TSMC CoWoS（CoWoS-S / CoWoS-L / SoIC）月产能在手扩产节奏、N3 / N2 晶圆排产占比、HBM3E / HBM4 良率与出货分配。
3. **出口管制与产业政策**：美国 BIS 出口管制更新、实体清单（Entity List）、AI Diffusion Rule 及高带宽存储/先进设备对华出货限制对公司中国区收入的具体冲击。
4. **核心客户公告与互联站队**：大客户最新自研 ASIC 项目节点、HBM 长协锁单、Scale-up（NVLink / UALink）与 Scale-out（InfiniBand / Spectrum-X / RoCE 以太网）网络选型。
5. **竞争路线图对比**：NVDA（Blackwell → Rubin → Rubin Ultra）、AMD（MI325X → MI350 → MI400）、Google TPU、Broadcom / Marvell 定制 ASIC 的 tape-out、送样（sample）与规模量产（ramp）时间窗。

---

## 三、八节结构各章叠加要求

### 第二节（本季度业绩 vs 预期）额外必查四项

1. **毛利率结构（Gross Margin Mix）**：区分高毛利业务（如数据中心、高端 EUV、软件授权）占比提升与低毛利业务占比变动对综合毛利率的拉动或拖累（以 bps 量化）。
2. **库存周转天数（Inventory Days / DOI）**：计算当季 DOI 及其环比/同比变化，明确判断当前处于**真实终端需求拉动（end-demand pull）**、**新品备货（new product build）**还是**渠道塞货（channel fill / digestion）**。
3. **晶圆与产品代际结构（Wafer / Product Mix）**：如 N3/N5 占比、HBM 占 DRAM 比重、新一代架构占比对 ASP 与毛利率的影响。
4. **新品爬坡一次性成本（NPI / Ramp Cost）**：识别架构切换初期（如 Blackwell 初期爬坡、2nm 初期折旧、新 HBM 良率爬坡）对当季与下季指引毛利率的阶段性压制及正常化时点。

---

### 第三节（业务结构拆解）专属规范与 A–G 子模板

#### 3.1 分部收入表（严格使用公司自身披露的分部口径）
- 如 NVDA/AMD/MRVL 按 `Data Center / Gaming / Client / Auto / Professional Visualization`；
- TSM 按 `Process Node (N3/N5/N7/...)` + `Platform (HPC / Smartphone / Automotive / IoT / DCE)`；
- ASML 按 `EUV / ArFi / ArF Dry / KrF / Metrology` + `Installed Base Management (Service)`；
- MU 按 `DRAM / NAND` + `CNBU / MBU / EBU / SBU`。

#### 3.2 主要增长引擎必补五项硬件要素
每个核心增长引擎除通用五项外，必须补充：
1. **产能瓶颈位置**：CoWoS-S / CoWoS-L / SoIC、HBM stack 层数（8-hi / 12-hi / 16-hi）与良率、光模块激光器（EML / CW laser）供给；
2. **供应链伙伴绑定**：TSMC / SK Hynix / Micron / Samsung / 日月光（ASE）/ Amkor，区分长协（LTA / 预付款锁产能）与现货（spot）；
3. **Scale-up vs Scale-out 网络架构**：NVLink / InfiniBand / 高速以太网（Spectrum-X / Arista）/ 铜缆（AEC）与光互联（800G / 1.6T / CPO）渗透率；
4. **客户集中度与结构**：前四大 Hyperscaler 占比、主权 AI（Sovereign AI）、企业级客户（Enterprise）与二线云（Tier-2 CSP / Neocloud）；
5. **产品代际路线图**：当前主力出货代际与下一代产品的 tape-out、样片、量产与收入贡献时间表。

#### 3.2 子模板 A–G（按公司所属细分赛道选用）

- **A. AI 加速器（AI Accelerator：NVDA / AMD）**
  - Hopper → Blackwell → Blackwell Ultra → Rubin → Rubin Ultra 出货与切换节奏；
  - 机柜级方案（GB200 / GB300 NVL72）vs 板卡级（HGX）出货结构；
  - Hyperscaler 与 Sovereign AI 收入占比；
  - HBM 容量与带宽升级对成本与售价（ASP）的影响；
  - 网络业务附着率（Networking attach rate：Spectrum-X、Quantum InfiniBand、NVLink Switch）。

- **B. 定制 ASIC 与网络芯片（Custom ASIC / Networking：AVGO / MRVL）**
  - 核心 Hyperscaler XPU/ASIC 项目进展（Google TPU、Meta MTIA、Microsoft Maia、AWS Trainium 及新增大客户定点的量产时间表）；
  - AI 收入中 Custom Silicon（计算 ASIC）与 AI Networking（Tomahawk / Jericho 交换芯片、DSP、Retimer、PCIe Switch）各自占比与增速；
  - 非 AI 传统企业网/存储/宽带业务的周期复苏位置。

- **C. 晶圆代工（Foundry：TSM）**
  - 各制程节点（N3 / N5 / N7 / N2）占晶圆收入比重；
  - 各技术平台（HPC / Smartphone / Automotive / IoT）收入增速与环比贡献；
  - CoWoS 与先进封装产能扩张速度及占总营收比重；
  - 先进节点涨价（ASP uplift）、海外晶圆厂（Arizona / Kumamoto）折旧摊薄对长期 53%+ 毛利率目标的影响。

- **D. 半导体设备（WFE：ASML / AMAT / LRCX / KLAC）**
  - 当季新签订单（Net Bookings）、在手订单（Backlog）及 EUV / High-NA EUV / DUV 台数拆解；
  - 中国区收入占比变化及出口管制下的正常化占比指引；
  - 高粘性装机服务收入（Installed Base / Customer Support Revenue）增速；
  - 逻辑（Leading-edge Logic / Gate-All-Around）vs 存储（DRAM / HBM /高堆叠 NAND）客户订单结构。

- **E. 存储芯片（Memory：MU）**
  - DRAM 与 NAND 各自的出货比特增速（Bit Shipments Growth）与平均售价（ASP）环比变化；
  - HBM（HBM3E / HBM4）单季收入规模、占 DRAM 总收入比重及跨年产能售罄（sold-out）状态、毛利率溢价；
  - 数据中心 SSD 与传统 PC/智能手机终端库存水位；
  - 晶圆厂 Capex（偏重 HBM 封装与洁净室）与行业供给纪律。

- **F. 模拟、功率与嵌入式（Analog / Power / Embedded：TXN / ADI / ON / MCHP）**
  - 工业（Industrial）、汽车（Automotive，含中国 EV 与欧美车企分化）、通信、个人电子终端收入环比走势；
  - 分销商渠道库存周数（Channel Inventory Weeks）相对于正常水位的位置，判断属于去库尾声还是补库起点；
  - 订单取消率（Cancellations）、加急单（Turns business）比例与 Book-to-Bill；
  - 固定资产折旧与晶圆厂稼动率（Utilization）对毛利率周期弹性的影响。

- **G. 高速网络与光通信/互联（Networking / Optical / Interconnect：ANET / CRDO / ALAB）**
  - AI 后端集群网络收入目标兑现进度与前两大云客户集中度；
  - 400G → 800G → 1.6T 端口/光模块/AEC（有源电缆）/ Retimer 升级节奏；
  - Scale-up 域内互联与 CPO（共封装光学）演进路线对传统可插拔模块的长期影响。

---

### 第四节（三大财务报表）额外核查四项

1. **存货结构**：拆解原材料、在产品（WIP）、产成品环比变化，结合下季收入指引判断库存是健康备货还是积压。
2. **Capex 与折旧节奏**：晶圆厂/存储厂资本开支强度（Capex / Revenue）及新厂转固带来的折旧爬坡；设计公司看自用超算集群与长期资产投入。
3. **长期采购承诺与预付款（Purchase Commitments / Prepayments）**：查阅 10-Q 附注中对晶圆代工（CoWoS/Wafer）或 HBM 供应商的长期采购承诺及客户预付款变动。
4. **一次性授权收入（License / Royalty Lumpy Revenue）**：对 ARM、QCOM 等含知识产权授权的公司，严格区分经常性版税（Royalty）与周期性集中确认的许可费（License）。

---

### 第六节（市场观点与股价表现）与第七、八节叠加要求

1. **第六节基准对比**：运行 `price_reaction.py` 时，`--sector-etf` 必须传入 **`SOXX` 或 `SMH`**（费城半导体指数 ETF），并在正文中对比直接同业对标公司（如 NVDA vs AMD、AVGO vs MRVL、TSM vs ASML）的同期表现；空头逻辑须专门回应“AI Capex 消化期（digestion）”、“ASIC 替代通用 GPU”、“渠道库存重复下单（double ordering）”等行业核心争议。
2. **第七节跟踪指标表（半导体标准示例）**：
   | 跟踪维度 | 跟踪指标 | 更新频率 |
   |---|---|---|
   | 先进封装与存储产能 | TSMC CoWoS 月产能 / HBM3E·HBM4 出货占比 | 季度 |
   | 产品代际切换 | 新架构机柜/芯片出货量与良率爬坡 | 季度 |
   | 下游需求锚点 | 四大 Hyperscaler 季度 Capex 实际值与指引修正 | 季度 |
   | 渠道与库存周期 | 自有 DOI 天数与分销商渠道库存周数 | 季度/月度 |
   | 监管与地缘 | BIS 出口管制细则与中国区收入占比 | 事件驱动 |
3. **第八节估值参考**：除远期 P/E、EV/Sales、EV/EBITDA、PEG 外，**对半导体周期股（存储、模拟、设备）必须给出中周期市盈率（Mid-Cycle P/E）参考**，说明当前利润处于周期峰值、中值还是谷底。

---

## 四、半导体与硬件保留英文术语清单

以下术语在中文报告中保留英文原文（首次出现可括号注中文）：
`Non-GAAP`、`CoWoS`（`CoWoS-S` / `CoWoS-L`）、`SoIC`、`HBM`（`HBM3E` / `HBM4`）、`Hyperscaler`、`Sovereign AI`、`NVLink`、`InfiniBand`、`Spectrum-X`、`Scale-up`、`Scale-out`、`Custom ASIC`、`XPU`、`Retimer`、`AEC`、`CPO`、`EUV`、`High-NA EUV`、`DUV`、`WFE`、`Gate-All-Around (GAA)`、`N3 / N2`、`Foundry`、`Wafer`、`Yield`、`Tape-out`、`NPI`、`Backlog`、`Bookings`、`Book-to-Bill`、`DOI`、`Channel Inventory`。
