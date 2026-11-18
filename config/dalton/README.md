# Dalton Canonical Coverage Roster (`coverage.yaml`)

`config/dalton/coverage.yaml` 是 Dalton 团队的规范覆盖基线清单（Core 27 + Watch 78 = 105 家公司），直接随 Git 仓库维护；公司名单、代码、层级（tier）或分组变更统一通过 PR 审阅直接编辑本文件。

服务端已引入覆盖单元表 `dalton_coverage_units`（迁移 `d023`，受 `src/dalton/dalton.yaml` 的 `coverage_units.enabled` 控制）与团队覆盖数据库表 `dalton_coverage`，系统全貌见 [ARCHITECTURE.md](../../docs/dalton/ARCHITECTURE.md) 与 [OpenClaw 退役现状](../../docs/dalton/features/openclaw-retirement.md)。

手工字段覆盖（如有）放置在 `config/dalton/coverage.overrides.yaml` 的 `companies.<TICKER>.<field>` 下，由覆盖同步与初始化流程合并读取。

## Schema Reference (`schema_version: 1`)

- `as_of`: Source roster date (`2026-10-09`).
- `generated_at`: UTC ISO-8601 timestamp when `coverage.yaml` was generated.
- `sources`: List of the 4 read-only upstream files and their `sha256` digests.
- `tiers`:
  - `core` (27): Weekly five-source review, daily tracking, full earnings preview and post-mortem chain.
  - `watch` (78): Screened by unusual price/volume move vs. peers; earnings covered via flash note only.
- `companies[]`:
  - `ticker`: Canonical Yahoo Finance symbol (`META`, `3690.HK`, `601100.SS`, `BRK-B`, etc.).
  - `name`: Canonical display name.
  - `tier`: `core` | `watch`.
  - `market`: `US` | `HK` | `CN`.
  - `peer_group`: Peer group key from `screen_movers.py` (used for relative move screening).
  - `sheet_section`: Section title from the Weekly Risk Reward sheet.
  - `peers`: Other tickers in the same `peer_group` (including peer-only tickers with empty tier in `TICKERS`).
  - `etfs`: Group benchmark ETFs/anchors from `EXTRA_PEERS`.
  - `aliases`: `{search_names: [...], adr: str | null, earnings_tracker: str}`.
  - `earnings`: `{group: "A"|"B"|"C"|"D", ir_url: str | null}`.
  - `workspace`: `{name: str, status: "active" | "planned" | "none"}`.
  - `coverage_stage`: `"none"` | `"legacy_imported"` | `"initial"` | `"full"`.
  - `note`: Special handling note or fiscal calendar remark.
