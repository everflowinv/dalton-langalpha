<!--
写入者：分析师 agent（建模与修订时）或旧认知迁移脚本（P1.4 写入 model/legacy/）
何时写：初次深度覆盖建模、财报后更新实际数或因新证据修订模型假设时
读写模式：按版本号只追加新文件（<TICKER>-model-v<NN>.xlsx），严禁覆盖旧版本文件；同步经 knowledge_update(doc="models") 登记版本
参考规范：PK.1 工作区文件布局、dalton-conventions/references/model-standards.md
-->

# 财务模型目录（`model/`）

本目录存放公司多期财务预测与估值模型文件。

## 命名与版本纪律（PK.1 & `model-standards.md`）

1. **标准命名**：`<TICKER>-model-v<NN>.xlsx`（例如 `META-model-v01.xlsx`、`META-model-v02.xlsx`）。
2. **每版一个新文件，不覆盖旧版**：每次修订模型假设或更新季度实际数时，递增版本号保存为新 `.xlsx` 文件，并在 `data/models.json`（经 `knowledge_update(doc="models", ...)`）登记版本号、变更原因、旧值 vs 新值及受影响的 Thesis 支柱。
3. **期间标准**：
   - 年度历史：从 **2018A** 起；
   - 单季历史：从 **2024Q1A** 起（累计拆单季 `H1-Q1`、`9M-H1`、`FY-9M` 标为 `Derived filing actual`）；
   - 年度预测：向前滚动 **3 年**；
   - 季度预测：覆盖至**次年 Q4**；
   - 覆盖期只能向后扩展，不得缩短。
4. **分层与底表纪律**：
   - 历史硬编码只取一手监管 Filing（附单元格级来源台账）；
   - 严格区分 Filing actual、Derived、推算（残差/分摊）、假设、情景五层；
   - 空值留空，严禁填 `0`。
5. **迁移存档（`model/legacy/`）**：P1.4 从旧系统迁入的原始预测 JSON 与摘要存放在 `model/legacy/` 下，仅供只读核对。
