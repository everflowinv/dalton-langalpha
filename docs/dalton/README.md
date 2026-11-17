# Dalton 开发与运维文档目录

本目录面向维护 Dalton 的工程师与后续开发智能体，全面反映当前代码库（基于 LangAlpha 的 Dalton 叠加层架构、监控面板、数据任务与自动化体系）的真实结构、运维入口与开发约束；面向分析师的产品使用手册位于前端 `web/src/pages/Help/`，不在此列。需要快速定位系统全貌时先读架构文档，上线变更或排查告警看运维手册，提交代码或同步上游前对照开发规范。

## 核心文档

- [ARCHITECTURE.md](./ARCHITECTURE.md)：说明 Dalton 与上游 LangAlpha 的叠加关系、每个 `src/dalton/*` 子包职责、前后端请求与数据流、Dalton 数据库表与 `d001`–`d039` 迁移链路、宿主机 `launchd` 服务及外部依赖。
- [OPERATIONS.md](./OPERATIONS.md)：说明容器构建与启停、`.env` 变更生效步骤、宿主机 `launchd` 常驻服务安装与重启、会话归档增量/回填与断点续跑、异地备份、Tailscale Funnel 对外入口及故障排查。
- [DEVELOPMENT.md](./DEVELOPMENT.md)：说明上游同步流程、`DALTON-HOOK` 叠加层规则、`pr_baseline.sh` 基线测试、前端首屏体积预算、双品牌规范、迁移编号规范以及代码审查硬性约定。
- [blueprint.md](./blueprint.md)：2026-10-18 立项产品蓝图，顶部新增“现状对照”节标注各目标的落地、调整与放弃状态。

## 功能专题（`features/`）

- [monitors.md](./features/monitors.md)：监控面板平台——四档可见性权限、`MONITOR_DATA_JOB_REGISTRY` 绑定、实时数据内部写入与短时签名授权（grant），以及 `DALTON_ARTIFACT_PUBLISHING_ENABLED` 发布总开关。
- [monitor-feeds.md](./features/monitor-feeds.md)：本机看板迁入与采集守护进程——美股/CN 板块轮动、A 股盘中监测、港股动量三个采集源、交易时段控制与东财限流节流策略。
- [sentiment-dashboard.md](./features/sentiment-dashboard.md)：市场情绪看板拆分——6 个全公司可见面板与共享静态资源的生成方式，以及 `seed_openclaw_data.sh` 覆盖层安装机制。
- [alphaengine-cache.md](./features/alphaengine-cache.md)：AlphaEngine 检索系统缓存——v3 缓存键、四档有效期、并发单飞、`params.fresh` 强制刷新、个人会议豁免及用量/命中率日志分析。
- [automations.md](./features/automations.md)：自动化与报告投递——公司/个人自动化权限与版本历史、24 个 `REPORT_PROFILES`、`automation_report` 提交与确定性失败告警机制。
- [data-jobs.md](./features/data-jobs.md)：确定性数据任务层——`manifest.yaml` 契约、容器/宿主机双执行器、内存准入与完成后触发面板自动发布。
- [settings.md](./features/settings.md)：设置页重组——个人设置与团队管理双分组、`minTier` 权限隔离、兼容路由重映射及只读/管理员专属卡片说明。
- [brand.md](./features/brand.md)：品牌规则——助手名称 Dalton 与产品名称 Everflow 的前后端注入、构建校验及链接预览/分享页适配。
- [openclaw-retirement.md](./features/openclaw-retirement.md)：OpenClaw 退役现状——已迁移能力与已拆除入口清单、仍保留只读关联的历史文件路径，以及安全停用剩余组件核对单。

## 专项部署指南（`dev-guide/`）

> 注：原 `dev-guide/CONVERSATION-ARCHIVE.md` 的现行运维参数与回填步骤已并入 [OPERATIONS.md](./OPERATIONS.md) 并删除原文件。

- [AUTH-DEPLOY.md](./dev-guide/AUTH-DEPLOY.md)：Supabase 登录接入、团队共享凭据与邀请注册部署说明。
- [PUBLIC-DEPLOY.md](./dev-guide/PUBLIC-DEPLOY.md)：Tailscale Funnel 与 Caddy 反向代理公网暴露边界与安全校验清单。
- [AUDIO-STORAGE.md](./dev-guide/AUDIO-STORAGE.md)：语音转写（ASR）专用 MinIO 对象存储及聊天大文件上传持久化配置。

## 版本说明与归档

- [release-notes/](./release-notes/2026-10-28.md)：历史阶段发布记录（2026-10-26、2026-10-28）。
- [archive/README.md](./archive/README.md)：早期开发简报、审查清单、多轮审计与试运行过程文档归档（只读参考，不再更新）。

## 其他目录 README

- [config/dalton/README.md](../../config/dalton/README.md)：标准化公司覆盖名单 `coverage.yaml` 维护与生成说明。
- [scripts/dalton/ops/README.md](../../scripts/dalton/ops/README.md)：GitHub 配置加密备份与 Google Drive 流式加密备份运维手册。
- [plugins/dalton_data/README.md](../../plugins/dalton_data/README.md)：Dalton 投研 MCP 插件（`researchdata` HTTP 桥接与 `dalton_sec` EDGAR stdio 服务）。
- [integrations/dalton-channel-gateway/README.md](../../integrations/dalton-channel-gateway/README.md)：多渠道接入网关（飞书/Discord/微信）与原生命令菜单注册说明。
- [integrations/openclaw/dalton-langalpha-broker/README.md](../../integrations/openclaw/dalton-langalpha-broker/README.md)：已停用的 OpenClaw 模型代理插件说明及替代方案索引。
