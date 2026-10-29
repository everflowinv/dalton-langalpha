---
name: dalton-transcript-polish
description: Transcribe workspace audio (Seed-ASR via prepare_transcription + dalton_asr.py) and polish the transcript.
---

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
Company workspace context: .agents/skills/dalton-knowledge/SKILL.md + .agents/skills/dalton-agenda/SKILL.md.

# Transcript Polish & Audio Transcription (`dalton_asr.py`)

## 核心铁律：转写必须由主代理发起

- **转写必须由主代理发起**：长录音转写依赖主代理的 `Bash` 后台长命令机制（设置 `run_in_background=True`，复用 `src/ptc_agent/agent/tools/bash.py:112`、`src/ptc_agent/agent/tools/command_handoff.py:159`、`src/server/services/commands/watcher.py:13` 的后台移交与完成回报链路）。
- **子代理没有后台命令**：子代理（Subagent）调用 `Bash` 时不支持 `run_in_background=True` 与自定义长超时，因此**严禁将录音转写任务委托给子代理发起**；必须在主代理（Analyst 或 Chief of Staff）回合内直接调用 `prepare_transcription` 并启动后台 `dalton_asr.py` 命令。
- **权限限制**：录音转写仅对管理员（`admin`）与内部成员（`internal`）开放；外部成员（`external`）调用会被直接拒绝，主代理应如实说明身份限制，不得尝试绕过。

## 标准两步工作流

### 第一步：主代理调用直接工具 `prepare_transcription`

主代理调用直接工具签发绑定到真实调用者、工作区与音频 SHA-256 的 `0600` 凭证文件：

```python
prepare_transcription(
    path="audio/expert_call_2026Q1.m4a",
    language="zh-CN",
    hotwords=["宁德时代", "神行电池", "资本开支"],
)
```

- **显式传入 `language`**：英语录音必须显式传 `language="en-US"`，中文传 `language="zh-CN"`，避免数字、货币单位或专有名词误识别。
- **返回值**：仅返回 `job_id` 与沙箱内 `0600` 权限的 `ticket_file` 路径（如 `.agents/threads/<thread8>/transcriptions/<job_id>.ticket.json`），凭证原文不会出现在工具输出或命令行参数中。

### 第二步：主代理用 `Bash`（设置 `run_in_background=True`）运行 `dalton_asr.py`

主代理使用 `Bash` 工具并开启 `run_in_background=True` 启动 `dalton_asr.py run`：

```bash
python3 .agents/skills/dalton-transcript-polish/scripts/dalton_asr.py run \
  --ticket-file .agents/threads/<thread8>/transcriptions/<job_id>.ticket.json \
  --output-dir transcriptions/<job_id>
```


- **凭证安全校验**：`dalton_asr.py` 仅从 `--ticket-file` 读取 `0600` 权限的常规文件；若文件权限不是 `0600`、为软链接，或尝试通过命令行参数（`--ticket` / `--token`）或环境变量传递凭证，命令会立即拒绝执行。
- **精简标准输出**：`stdout` 仅输出阶段（`stage`）、`job_id`、`segment_count`、`completed_segments`、`updated_at`、输出文件路径与简短错误信息，**绝不将整份逐字稿正文或凭证打印到标准输出**，也不会在供应商仅返回“处理中”时虚构百分比。
- **产物分离保存**：转写成功后，在 `--output-dir` 下分别写入：
  - `raw_response.json`：火山 Seed-ASR 原始分片响应、分片校验信息与原始毫秒时间戳（不覆盖逐字稿文本）。
  - `transcript.txt`：按全局时间偏移合并、带分段说话人前缀（`[S1-Speaker 1]`、`[S2-Speaker 1]`）的逐字稿正文。
  - `transcription_meta.json`：任务元数据、用量统计与产物路径索引。

## 轮询中断恢复、`retry` 与 `cancel` 语义

1. **后台轮询命令停止不等于取消任务**：
   - 当主代理回合结束后满一小时触发后台命令停止规则，或轮询进程收到 `SIGTERM` / `SIGINT` 退出时，**后端与宿主机桥上的转写任务仍在继续执行**，`job_id` 完整保留。
   - 主代理随后可执行 `status` 或 `result` 取回进度与最终逐字稿：
     ```bash
     python3 .agents/skills/dalton-transcript-polish/scripts/dalton_asr.py status \
       --ticket-file .agents/threads/<thread8>/transcriptions/<job_id>.ticket.json

     python3 .agents/skills/dalton-transcript-polish/scripts/dalton_asr.py result \
       --ticket-file .agents/threads/<thread8>/transcriptions/<job_id>.ticket.json \
       --output-dir transcriptions/<job_id>
     ```
2. **桥重启期间平滑退避**：
   - 轮询期间若遇宿主机桥重启或短暂网络 / 5xx 错误，`dalton_asr.py` 自动指数退避重试，不会把转写任务误判为失败。
3. **失败或未决任务重试（`retry`）**：
   - 若任务处于 `failed` 或 `indeterminate`，仅原任务创建者可执行 `retry`（复用已完成分片，不重复提交已成功分片）：
     ```bash
     python3 .agents/skills/dalton-transcript-polish/scripts/dalton_asr.py retry \
       --ticket-file .agents/threads/<thread8>/transcriptions/<job_id>.ticket.json \
       --output-dir transcriptions/<job_id> \
       --wait
     ```
4. **显式取消（`cancel`）语义**：
   - 只有显式执行 `cancel` 子命令才会取消任务：
     ```bash
     python3 .agents/skills/dalton-transcript-polish/scripts/dalton_asr.py cancel \
       --ticket-file .agents/threads/<thread8>/transcriptions/<job_id>.ticket.json
     ```
   - **重要说明**：已提交给火山 Seed-ASR 的分片能否在供应商侧取消、以及是否仍计费均未核实——`cancel` 仅停止后续尚未提交的分片，并如实输出已提交分片数量与请求 ID。

## 逐字稿精修（Polish）规范

- 读取 `--output-dir` 下的 `transcript.txt` 与 `raw_response.json` 进行精修，保留原文件不覆盖，将精修稿另存为 `polished_transcript.md`。
- **跨分段说话人防串号**：超过 30 分钟（`> 1800s`）的录音按 10 分钟（`600s`）切段转写，说话人标签默认带段号作用域（如 `S1-Speaker 1`、`S2-Speaker 1`）。不同分段的 `Speaker 1` **不代表同一自然人**；精修时必须根据上下文语义、称呼与问答角色核实后才能映射为具体职务或姓名，未确认前保留分段前缀。
