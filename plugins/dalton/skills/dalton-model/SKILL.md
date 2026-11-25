---
name: dalton-model
description: "在用户现有 Excel 模型上滚动维护、财报披露后更新实际值、从零新建单公司或行业财务预测模型，以及执行只读公式与合理性核查；沿用原模型样式与公式链，配套样式画像提取与静态/重算后校验脚本。"
---

# Dalton 公司与行业财务建模（`dalton-model`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
一致预期取数工具统一按本技能 `references/sanity-check.md` 执行。

执行建模任务前，先按工作区类型读取知识层文档：
- **公司工作区**：依次读取 `agent.md`、`thesis.md`、`debate.md`、`drivers.md` 与 `data/models.json`。
- **行业工作区**：依次读取 `sector.md` 与 `data/models.json`。

本技能负责建立与维护 Excel（电子表格工作簿，扩展名 `.xlsx`）财务预测模型。当工作区已有模型或用户上传了底稿时，直接在原模型基础上滚动维护并沿用原版面、样式与公式链；当工作区尚无模型时，按默认样式常量与全公式链规范从零新建模型。

---

## 一、四类任务模式与入口分流

开始操作前，先检查 `data/models.json`、`model/`（以及历史目录 `models/`）和 `sources/`，确定进入以下四种模式之一：

1. **存量模型滚动维护**：在现有单公司模型或行业多公司模型上向后扩展预测期间、增补公司模块、调整分部口径或校准驱动假设，输出新版本文件至 `model/`。
2. **财报披露后更新实际值**：读取一手监管财报，将刚披露季度的预测公式切换为实际披露值，重算现金、有息债务、利息收支与股本滚存，校准远期假设并输出预测变动归因。
3. **从零新建财务模型**：读取工作区知识层与一手财报，按默认格式规范与全公式链规则建立包含关键经营指标、分部拆解、三张财务报表、估值与汇总表的工作簿，保存至 `model/`。
4. **只读合理性与勾稽核查**：保持目标工作簿字节级只读，先运行重算前静态检查，对含外部链接的原件直接采用静态检查结果；对不含外部链接的原件复制临时副本重算并检查副本缓存值，核查预测值相对市场一致预期（`consensus`，多位卖方分析师预测的统计均值）或卖方报告的差异来源，输出问题清单。

**损坏命名区域前置修复**：若 `openpyxl`（读写 `.xlsx` 文件的 Python 库）读取原文件时因损坏的命名区域报错，先运行：

```bash
python .agents/skills/dalton-conventions/scripts/xlsx_repair.py <原文件.xlsx>
```

该脚本生成 `<原文件名>.repaired.xlsx` 修复副本并保持原文件不变，后续读取与画像提取改用该修复副本。

---

## 二、按需加载的四份细则参考文档

在进入对应环节前，读取 `.agents/skills/dalton-model/references/` 下的对应细则：

| 文件路径 | 读取时机 | 核心内容 |
|---|---|---|
| `references/format-spec.md` | 新建模型、提取存量样式画像或调整版面时 | 默认样式常量表、主题色与色调还原规则、存量工作簿按页与子区块画像规则、数字格式沿用边界与默认分组。 |
| `references/forecast-rules.md` | 设计预测逻辑、分部拆解与公式结构时 | 期间跨度、共用期间映射、季度加总年五类行公式与年度每股收益规则、分部改口径对账、无循环利息与股本滚存、未预测项留空、四类单元格角色与合法假设公式、汇总表规格。 |
| `references/rollforward.md` | 存量模型扩列、季报转实际、清外链或写盘时 | 双目录判定、新版本命名、输出副本扩列与求和区间重建、外部插件与四层外链清理、保留扩展部件的包级 XML 补丁写入与 `model_lineage.json` 谱系登记。 |
| `references/sanity-check.md` | 执行一致预期对账、差异归因与 `Checks` 复核时 | 按指标与期间取数及双能力门控、五项口径核实、可比期间差异归因四步法、理由写入位置、`Checks` 工作表规格、脚本警告说明位置与退出码判定。 |

底层通用操作直接复用 `.agents/skills/xlsx/scripts/` 中的确定性脚本：`recalc.py`（公式重算与缓存值回写）、`insert.py`（在输出副本上插入或删除行列并重指引用）、`render.py`（导出图片核对版面）与 `profile.py`（原始数据表探查）。样式画像提取、包级补丁写入与合规检查统一使用本技能 `scripts/model_style.py` 与 `scripts/style_audit.py`。

---

## 三、建模与更新八步执行顺序

### 第 1 步：锁定基准底稿、输出路径与谱系记录

1. 按 `references/rollforward.md` 的双目录与版本解析规则确定唯一输入底稿（新建模型时无输入底稿）和 `model/` 下的递增版本号目标路径：
   - 单公司工作区：`model/<TICKER>-model-v<NN>.xlsx`
   - 行业多公司工作区：`model/<Industry_Slug>_Model_v<NN>_<YYYYMMDD>.xlsx`
2. 在当次任务目录 `research/<YYYY-MM-DD>-model-<动作>/` 下保存全部脚本与中间文件，保持 `model/` 目录只存放历次 `.xlsx` 版本文件，所有原件与历史版本保持字节级只读。
3. 计算原底稿 `SHA-256`（安全哈希摘要值），并准备好在写盘后通过 `model_style.py lineage` 写入 `model_lineage.json`，明确记录当次最终交付文件的路径、哈希、实际输入底稿路径与哈希及样式画像路径。

### 第 2 步：提取样式画像与期间映射

- **存量模型维护**：先对原底稿运行重算前静态预检与样式画像提取（原底稿保持只读）：

```bash
python .agents/skills/dalton-model/scripts/model_style.py profile <原模型.xlsx> \
  --out research/<YYYY-MM-DD>-model-<动作>/style_profile.json

python .agents/skills/dalton-model/scripts/style_audit.py --static-only \
  --profile research/<YYYY-MM-DD>-model-<动作>/style_profile.json \
  <原模型.xlsx>
```

- **从零新建模型**：直接采用 `model_style.py` 内置的默认样式常量（详见 `references/format-spec.md`）。

### 第 3 步：输出映射与结构清单

在写工作簿前，整理本次改动清单（季报更新时写入 `research/<YYYY-MM-DD>-model-<动作>/mapping.md`）：
- 新增的预测年度列与季度列位置；
- 刚披露季度的预测转实际行项及一手财报来源；
- 经业务确认需留空的未预测科目（逐项写明工作表、行项、期间和原因）；
- 分部改口径的新旧区块映射与可比期间对账关系；
- 需从外部插件函数或外部工作簿链接抽离的单元格及对应静态输入格。

### 第 4 步：编写可复现脚本并生成新版本工作簿

1. 将本次构建或更新的 Python 脚本保存在任务目录下。
2. 存量模型扩列先在输出副本上调用 `.agents/skills/xlsx/scripts/insert.py columns ... --out <新模型.xlsx>` 并核查返回 JSON 的 `status == "success"` 与 `self_check.ok == true`，随后显式补齐边界外扩列的求和与衍生比率公式。
3. 存量工作簿含有 `<extLst>` 扩展节点、条件格式扩展或媒体部件时，使用 `model_style.py` 的包级 XML 补丁写入器 `patch_workbook_xml` 更新单元格、关闭迭代计算并清理四层外部链接，写后运行 `model_style.py part-diff <原模型.xlsx> <新模型.xlsx>` 核对零扩展部件或媒体丢失。
4. 按 `references/forecast-rules.md` 落实预测金额全公式、季度加总年零假设、无循环期初计息、期末股数与回购现金流联动及 `Checks` 工作表建设；按 `references/sanity-check.md` 完成一致预期对照表与理由写入。

### 第 5 步：全表公式重算

对新版本工作簿运行重算脚本：

```bash
python .agents/skills/xlsx/scripts/recalc.py <新模型.xlsx>
```

解析输出 JSON，确认 `status == "success"`（`total_errors == 0` 且 `unmatched == 0`）。若 `status` 为 `"errors_found"` 或 `"incomplete"`，按错误列表修正公式后重新运行直至 `status == "success"`。

### 第 6 步：运行静态与勾稽严格检查

先通过 `model_style.py lineage` 写入当次最终交付记录，再执行严格检查：

```bash
python .agents/skills/dalton-model/scripts/model_style.py lineage \
  --file research/<YYYY-MM-DD>-model-<动作>/model_lineage.json \
  --output <新模型.xlsx> \
  --baseline <原模型.xlsx> \
  --profile research/<YYYY-MM-DD>-model-<动作>/style_profile.json

python .agents/skills/dalton-model/scripts/style_audit.py <新模型.xlsx> --strict \
  --lineage research/<YYYY-MM-DD>-model-<动作>/model_lineage.json \
  --mapping research/<YYYY-MM-DD>-model-<动作>/mapping.md
```

新建模型省略 `--baseline` 与 `--profile`。按 `references/sanity-check.md` 的交付判定标准修复全部 `fail` 项，并在 `Checks` 工作表 `D` 列或 `README & Changelog` 写明所有 `"WARN"`、合法 `"N/A"` 及脚本警告的具体原因，直至 `style_audit.py --strict` 返回 `status == "pass"` 且退出码为 `0`。

### 第 7 步：渲染版面抽查

调用 `.agents/skills/xlsx/scripts/render.py <新模型.xlsx>` 导出核心工作表图片，核对列宽、数字完整显示（无 `###` 截断）、大纲分组折叠、冻结窗格与分节条层次。

### 第 8 步：核对原件哈希、登记知识层并交付

1. 重新计算原底稿的 `SHA-256` 哈希，确认与第 1 步完全一致。
2. 调用 `knowledge_update(doc="models", ...)` 登记新模型路径、`sha256` 及核心输出单元格（`key_outputs` 中的 `period` 按 `model_style.normalize_period_for_knowledge` 转为 `FY2026` / `4Q26`，无法匹配的复合年增长率列省略该非必填字段）。
3. 向用户交付新模型路径、关键年度与季度财务预测表、相对市场一致预期或卖方报告的核心差异归因摘要，以及原表硬编码修复清单（如有）。

---

## 四、只读合理性与勾稽核查模式（四步）

当用户要求只读审阅或核查一份既有工作簿时，保持目标文件字节级只读：

1. 对原件运行重算前静态检查 `style_audit.py --static-only <目标模型.xlsx>`，记录外部链接、外部插件函数、隐藏 `#REF!` 断裂引用、循环引用与公式结构问题。
2. 若原件含有外部链接部件或外部公式，直接以静态检查结果为基础输出问题清单；若原件无外部链接且需核对计算缓存值，复制临时副本到任务目录运行 `recalc.py <临时副本.xlsx>` 后对临时副本运行完整 `style_audit.py`。
3. 按 `references/sanity-check.md` 提取市场一致预期与卖方预测，核实五项口径后逐期拆解重大差异来源。
4. 交付包含公式与勾稽问题、外部依赖清单及一致预期差异拆解的核查报告，原文件哈希保持不变。
