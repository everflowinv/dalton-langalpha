# Dalton 开发与运维文档目录

这个目录面向维护 Dalton 的工程师和开发智能体，汇总当前代码库的真实结构、运维入口和开发约束（分析师产品手册在前端 `web/src/pages/Help/`）。想看系统全貌先读架构文档，做上线变更或查告警看运维手册，提代码或同步上游前对照开发规范。

## 核心文档

- [ARCHITECTURE.md](./ARCHITECTURE.md)：讲清 Dalton 与上游 LangAlpha 的叠加关系、每个 `src/dalton/*` 子包职责、前后端请求与数据流、Dalton 数据库表与 `d001`–`d039` 迁移链路、宿主机 `launchd` 服务和外部依赖。
- [OPERATIONS.md](./OPERATIONS.md)：讲清容器构建启停、`.env` 改动生效步骤、宿主机 `launchd` 常驻服务安装与重启、异地备份、Tailscale Funnel 对外入口及故障排查。
- [DEVELOPMENT.md](./DEVELOPMENT.md)：讲清上游同步流程、`DALTON-HOOK` 叠加层规则、`pr_baseline.sh` 基线测试、前端首屏体积预算、双品牌规范、迁移编号规则和代码审查红线。
- [blueprint.md](./blueprint.md)：2026-10-18 立项产品蓝图，顶部新增「现状对照」节，标出各目标的落地、调整和放弃状态。

## 功能专题（`features/`）

- [monitors.md](./features/monitors.md)：监控面板平台——四档可见权限、`MONITOR_DATA_JOB_REGISTRY` 绑定、实时数据内部写入与短时签名授权（grant），以及 `DALTON_ARTIFACT_PUBLISHING_ENABLED` 发布总开关。
- [monitor-feeds.md](./features/monitor-feeds.md)：本机看板迁入与采集守护进程——美股/CN 板块轮动、A 股盘中监测、港股动量三个采集源、交易时段控制和东财限流节流策略。
- [sentiment-dashboard.md](./features/sentiment-dashboard.md)：市场情绪看板拆分——6 个全公司可见面板与共享静态资源的生成方式，以及 `seed_openclaw_data.sh` 数据资产安装机制。
- [alphaengine-cache.md](./features/alphaengine-cache.md)：AlphaEngine 检索系统缓存——v3 缓存键、四档有效期、并发单飞、`params.fresh` 强制刷新、个人会议豁免及用量/命中率日志分析。
- [automations.md](./features/automations.md)：自动化与报告投递——公司/个人自动化权限与版本历史、24 个 `REPORT_PROFILES`、`automation_report` 提交和确定性失败告警机制。
- [data-jobs.md](./features/data-jobs.md)：确定性数据任务层——`manifest.yaml` 契约、容器/宿主机双执行器、内存准入和完成后触发面板自动发布。
- [settings.md](./features/settings.md)：设置页重组——个人设置与团队管理双分组、`minTier` 权限隔离、兼容路由重映射及只读/管理员专属卡片说明。
- [brand.md](./features/brand.md)：品牌规则——助手名称 Dalton 与产品名称 Everflow 的前后端注入、构建校验，以及链接预览和分享页适配。
- [openclaw-retirement.md](./features/openclaw-retirement.md)：OpenClaw 退役现状——已完成的迁移映射、现行唯一运行时路径，以及沿用历史命名的在用组件说明。

## 专项部署指南（`dev-guide/`）

> 注：原 `dev-guide/CONVERSATION-ARCHIVE.md` 里的现行运维参数和回填步骤已经并入 [OPERATIONS.md](./OPERATIONS.md)，原文件已删除。

- [AUTH-DEPLOY.md](./dev-guide/AUTH-DEPLOY.md)：Supabase 登录接入、团队共享凭据和邀请注册部署说明。
- [PUBLIC-DEPLOY.md](./dev-guide/PUBLIC-DEPLOY.md)：Tailscale Funnel 与 Caddy 反向代理的公网暴露边界和安全校验清单。
- [AUDIO-STORAGE.md](./dev-guide/AUDIO-STORAGE.md)：语音转写（ASR）专用 MinIO 对象存储及聊天大文件上传持久化配置。

## 版本说明与归档

- [release-notes/](./release-notes/2026-10-28.md)：历史阶段发布记录（2026-10-26、2026-10-28）。
- [archive/README.md](./archive/README.md)：早期开发简报、审查清单、多轮审计和试运行过程文档归档（只读参考，不再更新）。

## 其他目录 README

- [config/dalton/README.md](../../config/dalton/README.md)：标准化公司覆盖名单 `coverage.yaml` 的维护与生成说明。
- [scripts/dalton/ops/README.md](../../scripts/dalton/ops/README.md)：GitHub 配置加密备份和 Google Drive 流式加密备份运维手册。
- [plugins/dalton_data/README.md](../../plugins/dalton_data/README.md)：Dalton 投研 MCP 插件（`researchdata` HTTP 桥接与 `dalton_sec` EDGAR stdio 服务）。
- [integrations/dalton-channel-gateway/README.md](../../integrations/dalton-channel-gateway/README.md)：多渠道接入网关（飞书、Discord、微信）与原生命令菜单注册说明。
