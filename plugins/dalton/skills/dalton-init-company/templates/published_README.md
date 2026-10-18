<!--
写入者：仅限 publish_report 后端原生工具（经数字核验、独立审稿与 PM 审批通过后自动写入）
何时写：正式报告发布落库时（同时更新 published/INDEX.md 与 data/thesis.json 的 official 字段）
读写模式：只追加发布物，按约定严禁手工改写或删除（文件 sha256 记录在 dalton_publication_files 表，被改动时系统会发出警告）
参考规范：PK.1 工作区文件布局、PK.8–PK.11 发布治理
-->

# 正式发布物归档（`published/`）

本目录存放已通过发布核验与 PM 审批的正式研究交付物。

## 纪律与约定

1. **唯一写入通道**：本目录下的报告（`<YYYY-MM-DD>-<slug>.md` / `.pdf` / `.xlsx`）与索引 `published/INDEX.md` **只经 `publish_report` 工具写入**。
2. **只读不改写**：已发布文件代表基金在发布时点的正式档案，其内容哈希记录在数据库中。任何后续观点更新应通过撰写新草稿或更新知识层完成，严禁直接修改或删除 `published/` 下的历史文件。
