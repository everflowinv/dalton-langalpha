---
name: dalton-transcript-polish
description: "录音转写、逐字稿纪要整理与入库：主代理调用 Seed-ASR 转写音频，按句对句原则保真整理纪要、核对数字量级与专名，交付工作区（或按要求写飞书）并由 ingest_research 分流入库。"
---

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
Company workspace context: .agents/skills/dalton-knowledge/SKILL.md + .agents/skills/dalton-agenda/SKILL.md.

# 录音转写与纪要整理（`dalton-transcript-polish`）

将工作区音频（通过 `prepare_transcription` + `dalton_asr.py`）或用户提供的原始逐字稿整理成保真投研纪要，默认交付到工作区，并按说话人分流入库。

## 参考文件（按条件加载，不要一次全读）

| 什么时候读 | 读哪份 |
|---|---|
| 开始整理正文前（必读） | `references/transcript-editing.md` — 可删/需保留/不能新增、句对句原则、跨段说话人、英译中、长文分块 |
| 正文含金额、比例、日期或数量 | `references/numeric-integrity.md` — 数字账本、中英量级换算、双语交叉核对、数字审计脚本 |
| 正文含公司名、人名、产品、论文或可疑术语 | `references/verification-protocol.md` — 搜索顺序、来源优先级、置信度分级、校正说明表 |
| ASR 转写、飞书写入或 `ingest_research` 入库遇边界问题 | `references/pipeline-lessons.md` — 管线参数、分段说话人、飞书分段追加与入库分流细则 |

---

## 一、录音转写管线（仅音频输入）

### 核心铁律：转写必须由主代理发起

- **转写必须由主代理发起**：长录音转写依赖主代理的 `Bash` 后台长命令机制（设置 `run_in_background=True`）。子代理（Subagent）不支持 `run_in_background=True` 与自定义长超时，**严禁将录音转写委托给子代理发起**；必须在主代理（Analyst 或 Chief of Staff）回合内直接调用 `prepare_transcription` 并启动后台 `dalton_asr.py` 命令。
- **权限限制**：录音转写仅对管理员（`admin`）与内部成员（`internal`）开放；外部成员（`external`）调用会被拒绝，应如实说明身份限制，不得尝试绕过。

### 标准两步工作流

1. **主代理调用 `prepare_transcription` 签发 `0600` 凭证文件**：
   ```python
   prepare_transcription(
       path="audio/expert_call_2026Q1.m4a",
       language="zh-CN",
       hotwords=["宁德时代", "神行电池", "资本开支"],
   )
   ```
   - 英文录音必须显式传 `language="en-US"`，中文传 `language="zh-CN"`（避免英文被直接译成中文并引发 10 倍量级错）。
   - 返回 `job_id` 与沙箱内 `0600` 权限的 `ticket_file`（如 `.agents/threads/<thread8>/transcriptions/<job_id>.ticket.json`）。
2. **主代理用 `Bash`（设置 `run_in_background=True`）启动 `dalton_asr.py`**：
   ```bash
   python3 .agents/skills/dalton-transcript-polish/scripts/dalton_asr.py run \
     --ticket-file .agents/threads/<thread8>/transcriptions/<job_id>.ticket.json \
     --output-dir transcriptions/<job_id>
   ```
   - `dalton_asr.py` 仅从 `--ticket-file` 读取 `0600` 常规文件，拒绝命令行或环境变量传凭证；`stdout` 只打印阶段进度与产物路径，不打印逐字稿正文。
   - 成功后在 `transcriptions/<job_id>/` 下生成：`raw_response.json`（原始分片响应与毫秒时间戳）、`transcript.txt`（带分段说话人前缀 `[S1-Speaker 1]` 的合并逐字稿）、`transcription_meta.json`（任务元数据与用量）。

### 轮询中断恢复、`retry` 与 `cancel`

- **后台轮询停止 ≠ 取消任务**：主代理回合结束满 1 小时触发后台命令停止或进程收到 `SIGTERM` 时，宿主机桥上的转写任务仍在继续。用 `status` 查看进度、用 `result` 取回产物：
  ```bash
  python3 .agents/skills/dalton-transcript-polish/scripts/dalton_asr.py status \
    --ticket-file .agents/threads/<thread8>/transcriptions/<job_id>.ticket.json
  python3 .agents/skills/dalton-transcript-polish/scripts/dalton_asr.py result \
    --ticket-file .agents/threads/<thread8>/transcriptions/<job_id>.ticket.json \
    --output-dir transcriptions/<job_id>
  ```
- **失败重试（`retry`）与显式取消（`cancel`）**：任务处于 `failed` / `indeterminate` 时，原发起者可执行 `dalton_asr.py retry --ticket-file ... --output-dir ... --wait` 复用已成功分片重试；仅当用户明确要求放弃任务时才执行 `dalton_asr.py cancel --ticket-file ...`（仅停止未提交分片，已提交给火山引擎的分片能否取消及是否计费未核实）。

---

## 二、纪要整理规范

### 硬边界（任何情况下不突破）

1. **不新增原文没有的事实**：不补论据、不补过渡问句、不把联网查到的背景写进受访者嘴里。必要背景一律走"编辑注"，写在段落外。
2. **不把摘要式材料补写成逐字稿**：材料本身是要点稿时，成稿标题写"纪要"，判断不了时取保守值。
3. **不从"看起来合理"反推数字**：`78 billion` 就是 780 亿，不是 78 亿。不擅自补小数点、改单位、换币种。
4. **身份、关系、法律、财务和重大数字不适用中置信度的宽松规则**，一律按低置信度处理：保留原词 + 标注待核。
5. **可读性与保真冲突时，保真优先。**

### 执行流程

#### Step 1：收集元数据与公司名 P0 核对

确认公司名称（或行业分类）、会议类型、会议日期（默认今天）、参与方。用户消息里已有的直接提取，不反复追问。
**公司名核对是 P0**：转写常把公司名转错（如"讯策"→"迅策"、"潘提"→"Palantir"），整理前必须用 `web_search` 核实标准中文名、英文名与股票代码。

#### Step 2：四阶段纪要整理

以深耕科技、消费、制造与金融行业的高级股票分析师视角执行：

- **阶段一 · 通读**：识别参与方身份、会议背景与讨论主题。超过 20,000 字的长稿分块前先建说话人映射表、术语标准写法表、时间线和待核专名清单（见 `references/transcript-editing.md` 第 6 节）。
- **阶段二 · 精细化调整**：
  - **句对句**：逐句对照原文，保留全部实质内容、论证逻辑和具体例子，正文篇幅与原文相当，不压缩成摘要。
  - **语言净化**：删无意义语气词与机械重复；保留情态动词、改变结论的自我修正、有强调作用的重复与带新信息的离题段落（见 `references/transcript-editing.md` 第 2 节）。
  - **术语专业化**：P0 修正明显术语误识（"发声模型"→"大模型"）；P1 首次出现补英文全称或缩写；P2 确信度 >70% 且上下文有直接依据时才补全行业黑话并括号保留原文；仅首次出现标注。
  - **数字**：统一转阿拉伯数字；每个实质数字建账本，绑定单位、币种、时间、修饰对象和说话人；存疑标 `[数字待核：…]`，缺单位标 `[核实：原文缺单位]`（见 `references/numeric-integrity.md`）。
  - **专名核对**：按 `references/verification-protocol.md` 的搜索顺序与置信度分级核实，只看搜索摘要不算核实。
- **阶段三 · 语言处理**：
  - 原文中文：精准校对与润色，不翻译。
  - **原文英文**：**先出完整中文翻译版（作为正文主体，逐段翻译不遗漏），再在文末追加 `## 英文校订稿` 完整英文润色稿**。
- **阶段四 · 格式与说话人归属**：
  - **多方会议不丢归属 + 跨分段说话人防串号**：清除时间戳；多方专家会、有多个提问人的卖方交流必须保留发言归属（单人问答稿用**加粗提问**即可）。超过 30 分钟（`> 1800s`）的录音按 10 分钟（`600s`）切段转写，`S1-Speaker 1`、`S2-Speaker 1` 等标签仅在各自段内有效——**不同分段的 `Speaker 1` 不代表同一自然人**，必须根据上下文语义、称呼与问答角色核实后才映射为具体职务或姓名，未确认前保留分段前缀。
  - 保留原文已有的 Sources、URL、文档名和引用位置；正文前写内容提要（讨论了什么、形成哪些判断、有哪些分歧或待办，禁写"本次会议围绕……展开"等套话）。

#### 输出格式模板

```markdown
# {公司名/主题} — {会议类型}

**日期**: {YYYY-MM-DD}
**参与方**: {参与方描述}

---

## 内容提要

{讨论了什么、形成了哪些判断、还有哪些分歧或待办}

---

## 正文

{整理后的纪要正文}

---

## 术语说明

- **术语1**: 解释...

---

## 校正说明

| 原文 | 成稿处理 | 理由与置信度 | 来源 |
|---|---|---|---|
| {原始写法} | {修正后写法 / 保留 / 待核} | {依据，标高/中/低} | {链接或"未找到可靠来源"} |

---
（以下仅当原文为英文时追加）

## 英文校订稿

{与中文主体同结构的完整英文润色稿}
```

校正说明只记重要项目（影响检索或理解的专名修正、未闭环的数字冲突、所有待核项）；无修正项时写"本次整理未做需要说明的修正"，不省略该节。

#### 交付前检查（进入 Step 3 前逐条完成）

1. 原文每个实质信息点都能在成稿中找到，被删内容仅属噪音或机械重复；
2. 没有把要点扩写成虚构对白，没有把外部知识写进受访者口中；
3. 数字、否定、条件、比较和发言归属没有漂移；
4. **含金额的稿件必须运行数字审计脚本并逐条处理 `WARN`**（不要为消除告警而篡改原文）：
   ```bash
   python3 .agents/skills/dalton-transcript-polish/scripts/audit_numeric_units.py \
     transcriptions/<job_id>/transcript.txt transcriptions/<job_id>/polished_transcript.md
   ```
5. 专名核对完成，待核项均已进入校正说明表；
6. 中文通顺无宣传腔/总结腔；英文录音已全文翻译且文末附完整英文校订稿；同一专名全文写法统一。

#### Step 3：交付（默认写工作区，仅按明确要求写飞书）

- **默认交付到工作区**：
  - 音频转写任务：保留 `transcriptions/<job_id>/transcript.txt` 与 `raw_response.json` 不覆盖，将成稿写入 `transcriptions/<job_id>/polished_transcript.md`。
  - 非音频文字输入：按 `dalton-conventions` 交付物位置规则，写入工作区 `published/<YYYY-MM-DD>-<slug>-transcript.md`。
  - 在回复中给出**工作区成稿路径**与**待核项清单**（姓名/数字/单位/术语，无则写"无"），并询问是否确认入库（工作区开启自动入库时见 Step 4）。
- **仅当用户在本次对话中明确要求写飞书时**：
  - 调用 `feishu_create_doc(title="{公司名} — {会议类型} ({日期})", markdown=...)` 创建文档；若为双语长文（超过约 15,000 字符），先用 `feishu_create_doc` 写入中文主体（至校正说明结束），再在 `---` 分隔线处用 `feishu_append(token=<feishu_create_doc 返回的文档 token 或链接>, markdown=...)` 追加 `## 英文校订稿`。
  - **不调用任何改权限脚本**（新建飞书文档只做新建与追加，文档权限由服务端自动处理）。

#### Step 4：入库（`ingest_research`）

使用 `ingest_research` 按"谁在说"分流入库（细则见 `references/pipeline-lessons.md`）：

- **卖方观点（`speaker="sell_side"`）**：发言主体是券商分析师、策略师或销售自己的观点时，传 `speaker="sell_side"`、`institution="{机构}"`、`title="{标题}"`、`kind="卖方电话会"`（可选 `analyst`、`doc_date`、`tickers`）与 `file_path`（或 `content`）。
- **专家会 / 公司会（`speaker="expert_or_company"`）**：外部专家访谈、公司管理层交流、NDR、Fireside Chat、买方交流等，传 `speaker="expert_or_company"`、`target_type`（`"company"` 或 `"sector"`）、`target`（大写 ticker / 英文品牌名，禁纯数字代码；若返回 `needs_alias` 则改用英文目录名并通过 `aliases` 传入原中文名与代码）、`content_type`、`date` 与 `file_path`（或 `content`）。
- **拿不准是谁在说时**：**不传 `speaker`** 调用 `ingest_research`，按工具返回的提问先向用户确认，绝不猜着入库。
- **确认门槛与工作区自动入库（`get_workspace_auto_ingest`）**：
  - 默认（工作区 `dalton_auto_ingest` 关闭）：必须等用户确认。先传 `confirm=False` 预览，用户确认后再传 `confirm=True` 正式写入；若此前按用户要求写过飞书且用户在飞书改过内容，写入前先用 `feishu_read` 拉取最新全文。
  - 工作区已开启自动入库（`workspaces.config["dalton_auto_ingest"] = True`）：分类明确且无重复嫌疑时，调用 `ingest_research` 会自动完成校验与写入并返回单行回执（`已入库 <library>；编号 <id>；<title>`）；若命中疑似重复（`duplicate_suspected`）或未传 `speaker`，仍按工具返回的问题先问用户。
