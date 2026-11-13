---
name: dalton-init-company
description: "初始化或升级覆盖公司的标准工作区目录骨架（含带托管摘要块的 agent.md、各目录说明及幂等清单），不覆盖分析师已改文件。新建覆盖公司工作区或补齐缺失目录结构时使用；运行本地脚本即可快速完成。"
---

# Dalton 公司工作区骨架初始化（`dalton-init-company`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
Shared research layer: .agents/skills/research-conventions/SKILL.md.

## 1. 适用场景与边界

当开始覆盖一家新公司、或为已有覆盖名单中的标的初始化工作区目录时，使用本 skill 建立标准工作区骨架（对齐 PK.1 文件布局与 PK.6 `agent.md` 托管块规范）。

- **本脚本建立目录骨架，并写入初始 `data/company.json`**（公司标识、交易所、一句话介绍；写入前用技能自带的 company schema 校验）。其余知识文档（`data/{thesis,debate,drivers,management,lessons,agenda}.json`）仍由分析师在首次产出认知时调用 `knowledge_update(doc="...", ops=[{"op": "create", "value": {...}}], reason="...", base_version=None)` 创建，从而经过 schema 校验与版本记录。
  - 4 家存量迁移公司（`META`、`AMZN`、`GOOGL`、`MSFT`）的其他知识文档由 P1.4 经 PK.7 导入 API 创建。
  - 覆盖名单没有给出交易所时，`exchange` 写 `OTHER`，脚本输出的 `notes` 里有「交易所待核」。核实实际交易所后，用知识库更新改成 `NASDAQ`、`NYSE`、`HKEX`、`SSE` 或 `SZSE`。

## 2. 生成的工作区骨架

```text
<TICKER>/                        # 工作区根目录
├── agent.md                     # 注入上下文（含 PK.6 托管块标记与 LangAlpha 种子小节）
├── data/company.json            # 初始公司文档；缺交易所时 exchange 为 OTHER
├── data/consensus/README.md     # 卖方一致预期快照规范与 JSON schema
├── model/README.md              # 财务模型命名与版本规范（<TICKER>-model-v<NN>.xlsx）
├── research/README.md           # 事件笔记与专题研究目录说明
├── drafts/README.md             # 待发布草稿目录说明
├── published/README.md          # 正式发布物只读归档目录说明
├── sources/README.md            # 原始材料与迁移副本只读目录说明
└── .dalton/init.json            # 初始化版本与各文件 sha256 幂等清单
```

## 3. 在沙箱工作区中执行方式

在目标公司工作区根目录下运行（需要已安装的 `jsonschema`；校验用的 schema 在技能的 `references/schemas/`）：

```bash
# 先预览将要写入或跳过的文件（默认 dry-run）
python3 .agents/skills/dalton-init-company/scripts/init_company.py \
  --ticker META \
  --root . \
  --coverage ../Home/dalton/coverage.yaml

# 确认无误后执行写入
python3 .agents/skills/dalton-init-company/scripts/init_company.py \
  --ticker META \
  --root . \
  --coverage ../Home/dalton/coverage.yaml \
  --apply
```

如果 `../Home/dalton/coverage.yaml` 尚未同步或标的不在名单中，可通过命令行参数直接指定元信息：
`--name "Company Name" --tier watch --market US --peers "PEER1,PEER2"`。

## 4. 幂等保护规则（`.dalton/init.json`）

脚本每次运行时逐文件比对磁盘当前内容哈希与 `.dalton/init.json` 记录的初始哈希：

| 磁盘文件状态 | 处理动作 | 报告状态 |
|---|---|---|
| 文件不存在 | 写入模板文件并记录 sha256 | `written` |
| 文件存在，哈希与清单一致，且模板内容未变 | 跳过（0 写入） | `unchanged` |
| 文件存在，哈希与清单一致，但 `template_version` 升级导致渲染内容更新 | 安全覆盖（分析师未曾改动过该文件） | `updated` |
| `agent.md` 恰好为 LangAlpha 默认种子模板内容 | 视为未修改，直接覆盖为 Dalton 模板 | `written` |
| `agent.md` 已被分析师修改，但缺少 `<!-- dalton:summary:start` 托管块标记 | 保留原文件全部内容，仅在文件末尾追加一对空托管块标记 | `managed_block_appended` |
| 任何文件已被分析师修改（哈希与清单不同） | **绝不覆盖**，原样保留分析师内容 | `skipped_modified` |
