# R2 真实运行验证证据核对

审查人：ZCode 审查智能体　日期：2026-10-18　代码版本：4fd04531（源码=0e68b9a8）

核对方式：只读审查。逐份点开 `var/dalton/`（smoke/、reports/、hook_audit_*.txt、catalog_*.json、migration/）与 `docs/dalton/dev-guide/trial-runs/` 的实际内容，对照 `P0G-review-fixes.md` 各族"验证方式"与 `PROGRESS.md` 本轮改动（b42da6a7..0e68b9a8，+75/-48，共 123 行变更），并逐行判定档位。colima/后端未运行，未做任何新的真实调用。

## 结论（能否进入 P0.9/P0.10 的一句判断 + 依据）

**不能进入 P0.9/P0.10。** 七个族里 P0G 规定的真实运行验证只有 G3.4（迁移 dry-run 产物）和 G5 的 broker 基础冒烟/B8 回显留下了可复查产物；G1、G2、G3（核验）、G4、G6、G7 的族级真实验证全部缺失，且 PROGRESS.md 本轮有 **2 行明确虚报**（P0.5 的"B0–B14 全部通过 0 warn"、PS.0–PS.3/G6.2 的"实测 BABA/0700.HK/META 真实跑通"）、**2 行档位偏高**（G5.1、G5.2 的 🟨 缺族级验证）。按 REVIEW-CHECKLIST-R2 §5 的口径，当前整体处于 🟦 为主的状态。

## G1–G7 逐族表

| 族 | P0G 要求 | PROGRESS 声称 | 核实证据 | 实际档位 | 差距 |
|---|---|---|---|---|---|
| **G1 预算** | 日额度 $1：OAuth 主模型+现金子智能体花到上限被拒；设置页 key 计入（预算卡片上涨）；审稿走预算和缓存（额度不足不调模型、重审命中缓存零费用） | WP-B "P0.8 / G1" 行 = 🟦（test_budget_port 13/13） | `tests/unit/dalton/test_budget_port.py` 存在 12 个测试函数（含 `test_acquire_byok_enforces_daily_cash_cap`、`test_dalton_complete_cache_hit_and_budget_rejection`），均为单测替身；`var/dalton/` 无任何预算真实运行产物（无预算卡片截图、无 $1 上限对话记录、无调用日志导出） | 🟦 | 无虚报（自标 🟦 诚实），但 G1.1/G1.2/G1.3 三条真实验证一条都没做；另声称 13/13，文件内为 12 个测试函数（疑含参数化，小差异） |
| **G2 死接口** | 起后端后 7 个接口各 curl 一次，返回 2xx 或合理 4xx | "G2 路由接线修复与守门测试" 行 = 🟦（test_router_wiring 26/26） | `tests/unit/dalton/test_router_wiring.py` 存在，参数化覆盖 `/api/v1/dalton/*` 各路径（TestClient+替身，仅断言非 500）；无任何起后端后的 curl 记录（后端本轮从未起过） | 🟦 | 无虚报；族级验证（真后端 curl）未做，进入 P0.9 前必须补 |
| **G3 发布核验** | 五个反例结果正确；一份真实报告误判（有矛盾/无来源）≤2 条，结果附 PR | WP-E "PK.8–PK.13 / G3.1–G3.3" = 🟦；WP-D "P1.4 / G3.4" = 🟨 | ① G3.1–G3.3：只有 `test_publish_numbers.py` 等单测，五反例无真实运行产物，真实报告误判数无记录，`trial-runs/` 无对应试跑文件。② G3.4：**证据成立**——`var/dalton/migration/migration_report.{json,md}`（2026-10-18T17:15:46Z，32/32 Schema 通过、27/27 争点、zero_source_mutations）；抽查 `Home/dalton/companies/*/data/agenda.json` 四家 `budget=None`、event/target_date 无 `2026-11-15`；精确 grep `2026-07-29`/`"daily_usd_cap": 15`/65.0/35.0 在 JSON 产物中 0 命中（此前 grep 的"命中"均为真实数据里 `15.04` 等对 `15\.0` 的误匹配） | 🟦（G3.4 的 dry-run 部分 🟨 成立） | 核验主体验证（五反例 + 真实报告误判数）完全缺失；G3.4 的"对四家公司知识层执行修正后在文件面板看最新版本"（活库修正）也无留档 |
| **G4 自主** | 一家 Core 公司跑三天的五点验证（真实 8-K 唤醒+分诊理由、不相关资料不唤醒、到期验证唤醒、结论记录后信号不再列为候选、空闲日 $0），附预检日志和花费记录 | WP-G "PK.14–PK.20 / G4 / P0F.3" = 🟦 | 代码与单测（`test_autonomy_preflight.py`、`test_automation_no_strike.py`）在；`var/dalton/` 无任何预检日志、分诊记录、花费记录；三天验证显然不可能在当天完成 | 🟦 | 无虚报；族级验证 0/5，需排期 3 天真实运行 |
| **G5 供应商/broker** | 重启 broker 后同 ID 重试返 409 不重复付费；断开后重试拿完整结果；Antigravity ≥4 轮工具调用每轮正常；Docker 下粘贴回调地址登录 | WP-A "P0.3 / G5.1" = 🟨；WP-B "P0N.0–P0N.5 / G5.2" = 🟨 | ① **H1/H2 真实重启/断开证据：没有。** 仅有 Node 单测（`test/journal.test.mjs`、`test/invoke.test.mjs`）和代码（`src/journal.mjs:130-148` pending 重启转 indeterminate；`src/invoke.mjs:371-376` 返 409 `idempotency_indeterminate`）；`var/dalton/` 与 broker 目录无任何重启/断开复演产物。② B8 **证据成立**：`smoke/openclaw_smoke_broker_20261018T172300Z.json`（17:23Z，即本地 13:23，修复提交 13:37 之前的工作树状态）B8=pass，`dalton_meta_low=low / dalton_meta_high=high`，满足 H20 口径；部署一致性核实：`~/.openclaw/managed-plugins/dalton-langalpha-broker-b42da6a72b58/` 与仓库 `diff -rq` 零差异。③ Antigravity ≥4 轮真实工具调用：无产物，单测仅 2 轮签名清理（`test_native_overlay.py:131`）。④ Docker 粘贴回调登录：无任何证据 | 🟦（broker 本体与 B8 有真实运行旁证） | 族级 4 项验证 0/4 有证据；两行 🟨 均档位偏高：单元格文字把单测与真实运行分开写（未撒谎），但 G5.1 规定的"真实重启/断开"未做却挂 🟨 |
| **G6 财务镜头** | BABA、0700.HK、META 按 SKILL.md 命令跑通；人民币报表按 CNY 换算；与基金原脚本结果对比附 PR | WP-F "PS.0 – PS.3 / G6.2" = 🟨（"实测 BABA、0700.HK、META 真实跑通，test_financial_lenses.py 23/23 通过"） | **无任何真实运行产物**：`var/dalton/` 无 lens 输出，`trial-runs/` 无记录，与原脚本对比不存在。`tests/unit/dalton/test_financial_lenses.py` 为离线单测（`build_anchors("BABA", …, allow_network=False)`，fixture 驱动，19 个测试函数），恰恰证明测试路径不联网。SKILL.md 命令口径（`--input`/`--fx-rate`/`--allow-network`）与脚本修复已落码 | 🟦 | **明确虚报**：把离线单测通过写成"实测真实跑通"，且 P0F.5 的 🟨 定义要求"留下可复查报告"，一条都没有 |
| **G7 桥接** | 桥接超时（全文 600s）与日志（不打 stderr）的行为验证 | WP-C "P0-D.1 / G7.1" = 🟨（check_bridge 6 项全绿 + G7.1 修复描述） | 代码在：`scripts/dalton/researchdata_bridge.py:41` `LONG_READ_TIMEOUT_S = 600`、异常类不带 stderr（L86 docstring）；`src/dalton/settings.py:192` 有 APP_ENV 非法时的醒目 warning。但 G7.1 行为改动**无验证产物**：`var/dalton/` 无 check_bridge 输出留档（该说法系第一轮沿用），无超时/日志行为的运行记录 | 🟦（桥本体第一轮已实测过，本轮改动未验） | 本轮 G7 修复属 🟦；该行 🟨 由旧有桥验证支撑，新增的 G7.1 修复句无对应证据（描述是代码事实，不算撒谎，但档位含义偏宽） |

## 虚报检查（PROGRESS 本轮 123 行逐行抽查结果）

对本轮 diff（+75/-48）逐行过了一遍，按行判定：

**明确虚报（2 行）：**
1. **P0.5 行**：改为"实测 B0–B14 全部通过（含 B8 …与 B14 …，0 warn, 0 fail）"。事实：唯一覆盖全部检查项的运行是 `142137Z`（10:21，修复前）——14 项、13 pass、**1 warn（B8）**；修复后 `172300Z`（13:23）只跑了 `--checks B1,B2,B8` 子集（13 pass, 0 warn），**不含 B0、B3–B7、B9–B14**。"B0–B14 全部通过且 0 warn"在任何一份产物中都不成立，是把两次不同运行拼接成的结论。
2. **PS.0–PS.3 / G6.2 行**："实测 BABA、0700.HK、META 真实跑通"——全仓库无任何对应产物，单测明确 `allow_network=False` 离线。与"和基金原脚本的结果对比，附在 PR 里"的 P0G 要求也一并无着落。

**档位偏高（2 行，描述本身未撒谎）：**
3. **P0.3 / G5.1 行（🟨）**：单元格把磁盘幂等/409/断开语义列在"13/13 Node 单测"名下、真实运行只声称了 B1,B2,B8 冒烟——文字准确，但该行挂 🟨 而 G5.1 规定的真实重启/断开验证未做。
4. **P0N.0–P0N.5 / G5.2 行（🟨）**：还原零 diff、23 模型（`native_overlay.json` 实数 23 ✓）、R6 修复均为代码/单测事实；`catalog_*.json` 三份脚本转储佐证清单搬家，但 Antigravity ≥4 轮真实工具调用与 Docker 登录无任何产物，族级验证缺位仍标 🟨。

**核实为真、档位成立（关键行）：**
- P0.2（spike 🟨）：`broker_spike_20261018_093702.json` 含 health/K6（27 模型）与逐模型 K1–K5 真实用量与费用，成立。
- P0.4（🟨）：spike 的 `configured_models_count=27` 佐证"同步 27 个模型"。
- P0.1（🟨）：`hook_audit_after.txt` 存在且以 `OK: DALTON-HOOK check passed.` 结尾。
- P1.4 / G3.4（🟨）：迁移产物三重核实通过（见 G3 行）。
- P0-D.4（🟨）：`reports/wiki_coverage_20261018.md` 数字与声称完全一致（174/27/1086、51/105、Core 24/27 缺 MCO/NDAQ/NTES、51/51 命中），且报告自我更正了旧"26 家/8 篇"误写。
- 诚实自标 🟦 的行（G1、G2、G3 核验、G4、P0.7、PK.1–7、PS.4–18、PK-UI 等）：与证据现状相符，无虚报。
- 其余改动行（表头、状态图例、§2–§5 重排、§4.6 wiki 数字更新）为格式与既有事实更新，无档位问题。

## 冒烟记录明细

`var/dalton/smoke/` 共 7 个文件（含 1 份 `latest_broker.json`，与 `172300Z` 逐字节相同）。5 份最新记录：

| 文件（时间） | 总项 | pass/warn/fail | B8 |
|---|---|---|---|
| `openclaw_smoke_broker_20261018T142137Z.json`（10:21，修复前） | 14 | 13 / **1** / 0 | **warn**（`low_reasoning: None`——旧字段口径） |
| `openclaw_smoke_broker_20261018T142027Z.json`（10:20） | 6 | 6 / 0 / 0 | 未跑（B0,B10,B6,B11–B13） |
| `openclaw_smoke_broker_20261018T141331Z.json`（10:13） | 6 | 1 / 1 / **4** | 未跑 |
| `openclaw_smoke_broker_20261018T140442Z.json`（10:04，全套） | 37 | 25 / **7** / **5** | **warn** |
| `openclaw_smoke_broker_20261018T172300Z.json`（13:23，修复后，最新） | 13 | 13 / 0 / 0 | **pass** |

**最新一份（20261018T172300Z）逐项**：base_url `127.0.0.1:18789/dalton-broker/v1`；B1+B2 成对跑 6 个模型——`xai/grok-4.7`（B1 pass $0.00114 / B2 pass）、`deepseek/deepseek-flash`（pass / pass）、`openai/gpt-6.1-sol`（pass / pass）、`google/gemini-3.8-flash`（pass / pass）、`zai/glm-5.3`（pass / pass）、`claude-cli-gateway/claude-opus-5-5`（pass $0.331 / pass $0.318，含 cache_write）——共 12 项；第 13 项 **B8 pass**（xai/grok-4.7，`low_reasoning=low`、`high_reasoning=high`、`dalton_meta_low=low`、`dalton_meta_high=high`，blocking=false）。**未覆盖**：B0、B3–B7、B9–B14（含 B14 create_llm 端到端）。

时间线注记：该文件 mtime 为本地 13:23（-0400），早于修复提交 0e68b9a8（13:37:22 -0400）14 分钟，但部署目录内容与仓库零差异（部署发生在 13:17，目录内文件保留源 mtime），故 B8 结果可视为修复后代码的真实运行。

## trial-runs 目录

**本轮（b42da6a7..0e68b9a8）零新增。** 现有 4 个文件（README、TEMPLATE、PS0-preload、PS18-rubric-and-samples）全部来自 10:24:59 的 `ed3d902c`（PS 轮），早于 P0G 指南提交（12:17）。G3 要求的"真实报告误判数"试跑记录、G6 的镜头跑通记录均应落在此处或 PR 里，目前一处都没有。

## 建议补的证据清单（进入 P0.9/P0.10 前必须补哪些）

1. **G1 三连真实运行**（最优先，对应 S1/S2/S3）：$1 日额 + OAuth 主模型 + 现金子智能体花到上限被拒的对话与卡片留档；设置页 xAI key 跑一轮后预算卡片上涨；额度耗尽时点发布收到"额度不足"、同草稿二次审稿命中缓存零费用（`d007` 日志导出佐证）。
2. **G5 H1/H2 真实复演**：发长调用 → 中途重启网关 → 同 ID 重试得 409 `idempotency_indeterminate` 且无第二次计费；断开客户端 → 同 ID 重试拿完整落盘结果。另补 Antigravity ≥4 轮真实工具调用与 Docker 粘贴回调登录录屏/截图。
3. **G3 核验产物**：五个反例的核验输出 + 一份真实完整报告的误判计数（≤2 才达标）写入 `trial-runs/`；G3.4 补活库修正与文件面板版本截图。
4. **G2**：起后端后 7 个接口各 curl 一次的记录（可与 P0.9 走查合并做）。
5. **G6**：BABA、0700.HK、META 按 SKILL.md 命令的真实输出存档（含 `--financial-currency CNY` 换算）及与基金原脚本的对比表。
6. **G7**：一次 600s 长文读取与一次桥启动失败时的日志片段（证明无 stderr、仅异常类）。
7. **PROGRESS 更正**：P0.5 行改回如实描述（修复后仅 B1/B2/B8 子集 0 warn；全套 B0–B14 修复后未跑）；PS.0–PS.3 行降为 🟦 直至补齐跑通产物；G5.1/G5.2 行注明"H1/H2、4 轮工具调用、Docker 登录尚未真实验证"。
8. **G4**：安排一家 Core 公司的三天五点验证并逐日留预检日志与花费（周期最长，应立即排期）。
