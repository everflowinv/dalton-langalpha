# Dalton 开发与运维文档目录

这个目录面向维护 Dalton 的工程师和开发智能体，汇总当前代码库的真实结构、运维入口和开发约束（面向分析师的 Everflow 产品手册源码在 `docs-site/`，经生产构建输出到 `web/dist/help/` 并通过 `/help/` 对外提供）。想看系统全貌先读架构文档，做上线变更或查告警看运维手册，提代码或同步上游前对照开发规范。

## 核心文档

- [ARCHITECTURE.md](./ARCHITECTURE.md)：讲清 Dalton 与上游 LangAlpha 的叠加关系、每个 `src/dalton/*` 子包职责、前后端请求与数据流、Dalton 数据库表与 `d001`–`d044` 迁移链路、宿主机 `launchd` 服务入口和外部依赖。
- [OPERATIONS.md](./OPERATIONS.md)：讲清容器构建启停、生产部署（`deploy_main.sh`）、`.env` 改动生效步骤、宿主机 14 项 `launchd` 常驻服务安装与重启、运维脚本、异地备份、Tailscale Funnel 对外入口及故障排查。
- [DEVELOPMENT.md](./DEVELOPMENT.md)：讲清上游同步流程、`DALTON-HOOK` 叠加层规则、`pr_baseline.sh` 基线测试、前端首屏体积预算（490 KiB）、双品牌规范、迁移编号规则（当前头节点 `d053`）和代码审查红线。
- [blueprint.md](./blueprint.md)：2026-10-18 立项产品蓝图，顶部附「现状对照」节，标出各目标的落地、调整和退役状态。

## 功能专题（`features/`）

- [antigravity-effort-picker.md](./features/antigravity-effort-picker.md)：Antigravity 系列合并与思考强度——界面按系列折叠、系列默认强度集中配置、定时任务思考强度（`d042`）与 Opus 回填脚本、厂商级额度冷却，以及周报按系列汇总。
- [external-onboarding.md](./features/external-onboarding.md)：新用户注册引导全链路——邀请兑换、团队共享凭据首登路由裁剪、5 档精选模型“智能 / 速度 / 说人话”评分、自动任务 6 档默认备用模型链与旧进度兼容。
- [alphaengine-cache.md](./features/alphaengine-cache.md)：AlphaEngine 检索缓存与额度管理——双独立额度核算、团队级 v4 缓存键、单家公司一次检索与同词重搜软提醒、按页计额规则、单进程熔断、7 天旧缓存兜底、`dalton-thesis-delta` 按日期续采及用量日志分析。
- [automations.md](./features/automations.md)：自动化与报告投递——公司/个人自动化权限与版本历史、26 个 `REPORT_PROFILES`、`automation_report` 提交、订阅失败自动回退、执行历史投递状态聚合、按计算机范围查询（`d043`）与确定性失败告警机制。
- [data-jobs.md](./features/data-jobs.md)：确定性数据任务层——`manifest.yaml` 契约（42 项任务）、容器/宿主机双执行器（含宿主机 `outbox.json` 投递读取）、内存准入保护和完成后触发面板自动发布。
- [monitors.md](./features/monitors.md)：监控面板平台——四档可见权限、6 项 `MONITOR_DATA_JOB_REGISTRY` 绑定、实时数据内部写入与短时签名授权（grant），以及 `DALTON_ARTIFACT_PUBLISHING_ENABLED` 发布总开关。
- [monitor-feeds.md](./features/monitor-feeds.md)：本机看板迁入与采集守护进程——美股/CN 板块轮动、A 股盘中监测、港股动量三个采集源、交易时段控制和东财限流节流策略。
- [sentiment-dashboard.md](./features/sentiment-dashboard.md)：市场情绪看板拆分——6 个全公司可见面板与共享静态资源的生成方式，以及 `seed_openclaw_data.sh` 数据资产安装机制。
- [settings.md](./features/settings.md)：设置页结构与权限边界——个人设置（8 项）与团队管理（3 项）双分组、`minTier` 权限隔离、历史路由重映射及只读/管理员专属卡片说明。
- [brand.md](./features/brand.md)：品牌规则——助手名称 Dalton 与产品名称 Everflow 的前后端注入、静态帮助文档 `/help/` 构建，以及链接预览和分享页适配。
- [openclaw-retirement.md](./features/openclaw-retirement.md)：OpenClaw 退役现状——已完成的迁移映射、现行唯一运行时路径，以及沿用历史命名的在用组件说明。

## 专项部署指南（`dev-guide/`）

> 注：原 `dev-guide/CONVERSATION-ARCHIVE.md` 随团队对话归档转为截至 2026-11-18 的只读历史卡片（见 [ARCHITECTURE.md](./ARCHITECTURE.md) §2.4 / §4）已退役删除。

- [AUTH-DEPLOY.md](./dev-guide/AUTH-DEPLOY.md)：Supabase 登录接入、团队共享凭据和邀请注册部署说明。
- [PUBLIC-DEPLOY.md](./dev-guide/PUBLIC-DEPLOY.md)：Tailscale Funnel 与 Caddy 反向代理的公网暴露边界和安全校验清单。
- [AUDIO-STORAGE.md](./dev-guide/AUDIO-STORAGE.md)：语音转写（ASR）专用 MinIO 对象存储及聊天大文件上传持久化配置。

## 版本说明与归档

- [release-notes/](./release-notes/2026-10-28.md)：历史阶段发布记录（2026-10-26、2026-10-28）。
- [archive/README.md](./archive/README.md)：早期开发简报、审查清单、多轮审计和试运行过程文档归档（只读参考，不再更新）。

## 其他目录 README

- [config/dalton/README.md](../../config/dalton/README.md)：标准化公司覆盖名单 `coverage.yaml` 的维护与生成说明。
- [scripts/dalton/ops/README.md](../../scripts/dalton/ops/README.md)：GitHub 配置加密备份和 Google Drive 流式加密备份运维手册。
- [plugins/dalton_data/README.md](../../plugins/dalton_data/README.md)：Dalton 投研 MCP 插件（`researchdata` HTTP 桥接）。
- [plugins/dalton_sec/README.md](../../plugins/dalton_sec/README.md)：公开美股 SEC EDGAR XBRL 财报 MCP 插件（`dalton_sec` stdio 服务，Apache-2.0）。
- [integrations/dalton-channel-gateway/README.md](../../integrations/dalton-channel-gateway/README.md)：多渠道接入网关（飞书、Discord、微信）与原生命令菜单注册说明。
