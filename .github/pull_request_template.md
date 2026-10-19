<!-- DALTON-HOOK: P0J.J4 Dalton PR description template with diff line mapping, check_report_claims scan, and principle self-check -->
<!--
流程要求（P0J-round4-fixes.md §4 / P0J-task-brief.md §5 J4 & §6）：
1. PR 描述里的每一条说法都要写明对应的文件和行号，且每一条都要能在 diff 里找到。
2. 提 PR 前，把本 PR 描述保存成文件，用 `scripts/dalton/check_report_claims.py` 扫描（`uv run python scripts/dalton/check_report_claims.py --strict <本 PR 描述保存的文件> <改动的文档>`），把实际输出贴在下方。
3. 一族一个分支、一个 PR，不夹带本族范围以外的改动；证据族的 PR 只包含文档。
4. 开发方不合并自己的 PR；lead 在 PR 里留下 `审查：通过` 的评论并逐项核对原则自查表后，由 lead 合并。
-->

## 本族范围
（对应 `docs/dalton/dev-guide/P0J-round4-fixes.md` 第几节）

## 改动清单
- 文件:行号 —— 改了什么、为什么（每一条都要写明对应的文件和行号，并且能在 diff 里找到；把本 PR 描述保存成文件并用 `scripts/dalton/check_report_claims.py` 扫描）

## 真实运行验证
（命令、输出、证据路径；J5 之前的族写真实后端上的手工验证）

## 基线命令输出（贴实际输出）
```text
./scripts/dalton/check_hooks.sh --strict --audit
uv run pytest tests/unit/dalton tests/unit/llms tests/unit/middleware/skills tests/unit/mcp_servers -q
node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs
pnpm --dir web typecheck && pnpm --dir web test -- --run
uv run python scripts/dalton/check_report_claims.py --strict <本 PR 描述保存的文件> <改动的文档>
```

## 原则自查
1. 新增了正则、词表、按词或按 ID 的判断吗？            是/否：
2. 新增了次数限制吗？                                    是/否：
3. 新增的测试锁的是修好的回归或钱、凭据的契约吗？        逐个列出：
4. src/ 下的净行数变化，以及删掉了什么：                  命令与输出：
5. 改了上游文件吗？DALTON-HOOK 是否齐全：                是/否：
6. 描述里每一条说法都对应到 diff 的文件和行号了吗：      是/否：
7. 包含本族范围以外的改动吗：                            是/否：
