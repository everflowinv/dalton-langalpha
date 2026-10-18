<!--
写入者：旧认知迁移脚本（P1.4 scripts/dalton/migrate_legacy_knowledge.py）或分析师归档外部一手原件副本时
何时写：公司认知初始化迁移或归档关键外部原始材料时
读写模式：只读归档约定（作为知识层 evidence_refs 的不可变文件引用底座，切勿篡改已归档原件）
参考规范：PK.1 工作区文件布局、P0D-P1 §P1.4 旧认知迁移
-->

# 原始来源与迁移档案（`sources/`）

本目录存放供溯源核对的只读原始材料副本与旧系统迁移快照：

- `sources/legacy/openclaw/`：OpenClaw `stock-tracker` 迁入时的 `thesis.md`、跟踪配置、提示词与去重日志原样副本。
- `sources/legacy/dalton-core/`：旧 Dalton 数据库迁入时的最新 `debate_map`、`company_dossier` 原始 JSON 记录。
- `sources/legacy/provenance.json`：知识层各条目（如 `P1`、`D1`、`K1`）回指旧系统版本与文件哈希的机器可读出处总表。
- `sources/wiki-index.md`：基金只读 Wiki（`vectors.db`）中与本公司直接关联及交叉提及的文档索引（正文不复制，按需经 `researchdata` 读取）。
