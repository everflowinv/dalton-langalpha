<!--
写入者：分析师 agent 或用户
何时写：起草初筛报告（Preview）、多空辩论（Bull/Bear）、完整投资备忘录（Memo）或财报前瞻/点评草稿时
读写模式：草稿阶段随时可读写修改，永不拦截；提交正式发布时由 publish_report 读取并生成 .audit.md 核验侧车
参考规范：PK.1 工作区文件布局、PK.8–PK.13 发布治理
-->

# 待发布草稿目录（`drafts/`）

本目录存放正在撰写或迭代中的研究报告草稿。

## 规则与治理原则

1. **草稿自由迭代**：草稿可以是任意 readiness posture，随时可新建和修改，系统在草稿阶段不做写入拦截。
2. **文件命名**：`<slug>.md`（可选配套 `<slug>.html`、`<slug>.xlsx`），核验侧车文件为 `<slug>.audit.md`。
3. **发布门槛（Publish Gate）**：当草稿达到 `review-ready` 并准备向 PM 正式发布时，调用 `publish_report` 工具。系统将在此时集中执行一次数字核验、跨模型家族独立审稿与 PM 审批卡确认，通过后自动归档至 `published/`。
