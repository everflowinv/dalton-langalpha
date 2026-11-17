# Dalton 历史过程文档归档（archive）

本目录归档 Dalton 早期开发阶段（2026 年 10 月中下旬至 11 月初）的阶段报告、逐轮代码审查记录、真实试运行报告、人工审查清单以及早期静态自动任务 JSON 模板；这些文档已停止更新，仅用于追溯历史决策依据。查找当前系统架构、日常运维或开发规范，请直接阅读上级目录的新文档：[总目录](../README.md)、[系统架构](../ARCHITECTURE.md)、[运维手册](../OPERATIONS.md) 与 [开发规范](../DEVELOPMENT.md)。

## 归档范围

- `dev-guide/review/`：第 1–7 轮代码与运行证据审查报告及汇总（历史过程记录）。
- `dev-guide/trial-runs/`：早期实盘试运行样本与评估记录。
- `dev-guide/automation-templates/`：早期自动任务 JSON 参考模板（经检索代码库，无运行时脚本或后端逻辑读取这些 JSON 文件，仅 `tests/unit/dalton/test_autonomy_preflight.py` 保留一行历史注释引用）。
- `dev-guide/P0*.md`、`dev-guide/PK-*.md`、`dev-guide/PS-*.md`、`dev-guide/REVIEW-CHECKLIST*.md`、`dev-guide/PROGRESS.md`、`dev-guide/README.md`：早期各批次开发简报、进度台账与审查清单（仍在执行的工程原则已提炼入 [DEVELOPMENT.md](../DEVELOPMENT.md)）。
- `workflow-completion-followups.md`：早期工作流收尾待办记录。
