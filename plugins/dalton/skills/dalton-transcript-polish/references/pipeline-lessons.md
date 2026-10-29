# 管线细节与踩坑记录

ASR 转写、飞书文档写入或 `ingest_research` 入库遇到边界问题时读这份。

## 1. ASR 转写（`prepare_transcription` + `dalton_asr.py`）

- **主代理发起与后台执行**：长音频转写只能由主代理（Analyst 或 Chief of Staff）先调 `prepare_transcription`，再用 `Bash`（设置 `run_in_background=True`）启动 `.agents/skills/dalton-transcript-polish/scripts/dalton_asr.py run`。子代理不支持 `run_in_background=True`，切勿委托子代理发起转写。
- **英文录音必须显式传 `language="en-US"`**：调用 `prepare_transcription` 时，英文录音必须传 `language="en-US"`，中文传 `language="zh-CN"`。不指定语言时，底层 Seed-ASR 可能把英文直接输出成中文，并连带产生 10 倍量级错误（实测原话 "15 billion USD" 被直接转成 "15亿美元"）。若发现英文录音的 `transcript.txt` 出现成段中文，立即用 `language="en-US"` 重新发起转写。
- **金融与行业热词（`hotwords`）**：按会议主题在 `prepare_transcription` 中传入 `hotwords`（如 `["GMV", "ARPU", "Take Rate", "货币化率", "资本开支"]`），降低专名与缩写误识率。
- **长录音分段与跨段说话人编号**：超过 30 分钟（`> 1800s`）的录音会自动按 10 分钟（`600s`）切段提交，`transcript.txt` 中的标签带分段前缀（`[S1-Speaker 1]`、`[S2-Speaker 1]`）。不同分段的 `Speaker 1` 互不对应；此外超长多人对谈可能出现声纹并人，精修时必须结合问答语境与称呼核实发言归属。
- **不要从 `stdout` 截取逐字稿**：`dalton_asr.py` 的标准输出只打印阶段状态（`stage`）、进度计数与文件路径，不会打印逐字稿全文。转写完成后直接读取 `--output-dir`（`transcriptions/<job_id>/`）下的 `transcript.txt` 与 `raw_response.json`，保留原文件不覆盖，成稿另存为 `polished_transcript.md`。
- **轮询停止不等于任务取消**：主代理回合结束满 1 小时后台命令停止或收到 `SIGTERM` 时，桥接层上的转写任务仍在继续。用 `dalton_asr.py status` 或 `dalton_asr.py result` 取回结果；任务处于 `failed` 或 `indeterminate` 时用 `dalton_asr.py retry` 复用已成功分片重试；只有用户明确要求放弃时才跑 `dalton_asr.py cancel`。

## 2. 飞书文档（仅当用户明确要求时）

- **默认不写飞书**：成稿默认保存在工作区（音频转写写 `transcriptions/<job_id>/polished_transcript.md`，非音频文本整理写 `published/`），只有用户在本次对话中明确要求写飞书时才调用 `feishu_create_doc`。
- **创建前完成公司名 P0 核对**：`feishu_create_doc` 的 `title` 在创建时定死，无法事后修改标题。创建前必须先用 `web_search` 核实公司标准中文名、英文名与股票代码（标题格式：`{公司名} — {会议类型} ({日期})`）。
- **超长双语稿分段追加**：单次写入超过约 15,000 字符可能截断。英文录音的双语纪要（中文主体 + `## 英文校订稿`）先用 `feishu_create_doc` 写入中文主体（标题到校正说明结束），再用 `feishu_append` 追加英文校订稿，切分点选在 `---` 分隔线处，不在句中截断。
- **不调用任何改权限脚本**：`feishu_create_doc` 已在服务端按调用者身份自动处理文档访问权限，新建文档只做新建与追加，本技能不改任何飞书权限。返回里出现 `warning` 字段（自动分享失败）时，把告警原样转述给用户，由用户自行在飞书里处理。
- **入库前同步飞书修改**：若本次已按用户要求创建了飞书文档，用户在确认入库前可能直接在飞书里改过正文；入库前先用 `feishu_read` 读取最新内容，再传给 `ingest_research`。

## 3. 研究库入库（`ingest_research`）

- **按"谁在说"分流**：
  - 发言主体是券商分析师、策略师或销售自己的观点（无外部专家、非公司管理层答问）→ `speaker="sell_side"`，必填 `institution` 与 `title`，电话会传 `kind="卖方电话会"`，可选 `analyst`、`doc_date`、`tickers`。
  - 专家访谈、公司管理层交流（路演 / Group Call / 1x1 / 业绩会）、NDR、Fireside Chat、买方交流 → `speaker="expert_or_company"`，必填 `target_type`（`"company"` 或 `"sector"`）、`target`、`content_type`、`date`。
  - 拿不准是谁在说时**不要传 `speaker`**，工具会返回 `speaker_unknown` 与标准提问，先向用户确认再入库。
- **公司 `target` 命名与别名登记（`aliases`）**：
  1. 有美股 ticker 的用大写 ticker：`FUTU`、`BABA`、`PDD`、`TCOM`；
  2. 仅港股/A股上市且有公认英文品牌名的用大写英文品牌名：`MEITUAN`、`POPMART`、`MIXUE`、`HKEX`；
  3. 都没有的用中文公司名：`字节跳动`、`快手`、`菜鸟`；
  4. **禁止用纯数字股票代码建目录**。若 `ingest_research` 返回 `needs_alias`（附 `existing_targets` 候选），改用已有英文目录名或新建英文目录名重新调用，同时把原中文名和股票代码传入 `aliases`（如 `aliases=["002271.SZ", "东方雨虹"]`），写入成功后会自动登记别名。
- **`content_type` 使用规范类型**：从 `管理层会议纪要`、`专家访谈`、`NDR纪要`、`Fireside Chat`、`买方交流`、`行业趋势`（行业讨论用）、`研究笔记` 中选择，不要自创类型；完整允许值以桥的允许列表 `WIKI_INGEST_ALLOWED_CONTENT_TYPES`（`scripts/dalton/researchdata_bridge.py`）为准，不在列表内的类型会被 400 拒绝；卖方电话会与券商研报走 `speaker="sell_side"`。
- **自动入库开关（`dalton_auto_ingest`）**：
  - 工作区未开启自动入库（`get_workspace_auto_ingest` 为 `False`，默认）时，必须分两步：先 `confirm=False` 预览并向用户展示目标库与元数据，用户确认后再 `confirm=True` 正式写入。
  - 工作区已开启自动入库（`workspaces.config["dalton_auto_ingest"] = True`）时，分类明确且库内无重复嫌疑会直接写入并返回单行回执（`已入库 <library>；编号 <id>；<title>`）；若命中疑似重复（返回 `duplicate_suspected`）或未传 `speaker`，工具仍会暂停并要求先问用户（用户确认强制写入相似稿件时传 `confirm=True` 与 `allow_similar=True`）。
