export const meta = {
  name: 'weekly-coverage-review',
  description: 'Dalton 周会：全名单筛选 → Core 逐家并行五源 → Watch 命中定向检索 → EPS vs 倍数归因 → 观点变化汇总 → call',
}

const { home_dir: K, end: E, coverage_path: L } = args
const R = `${K}/research/weekly-${E}`
const S = '.agents/skills/dalton-weekly-review'
const base = '先读 .agents/skills/dalton-conventions/SKILL.md 与 ' + S + '/SKILL.md。'

// Note: schema_max_depth is 5 (root=1, properties=2, field=3, items=4).
// Element object keys ({ticker, workspace_dir, held} for core; {ticker, event_hint, workspace_dir} for flagged)
// are enforced via the prompt contract while keeping schema nesting depth <= 4.
const SCREEN = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    base: { type: 'string' },
    core: { type: 'array', items: { type: 'object' } },
    flagged: { type: 'array', items: { type: 'object' } },
    warnings: { type: 'array', items: { type: 'string' } },
  },
  required: ['ok', 'core', 'flagged'],
}

const CO = {
  type: 'object',
  properties: {
    ticker: { type: 'string' },
    file: { type: 'string' },
    move_pp: { type: 'number' },
    excess_pp: { type: 'number' },
    driver: { type: 'string' },
    thesis_impact: { type: 'string' },
    verdict: { type: 'string' },
    call_candidate: { type: 'boolean' },
  },
  required: ['ticker', 'file'],
}

phase('筛选与归因')
const scr = await agent(
  `${base} 用覆盖名单 ${L}，窗口终点 ${E}：跑 python ${S}/scripts/screen_movers.py 与 python ${S}/scripts/attribution.py，结果写 ${R}/screen.json、${R}/attribution.json。返回 JSON：ok（boolean）、base（窗口起点日期）、core（数组，每项含 ticker, workspace_dir, held）、flagged（Watch 命中数组，每项含 ticker, event_hint, workspace_dir）、warnings（字符串数组）。`,
  { agentType: 'general-purpose', label: '筛选', phase: '筛选与归因', schema: SCREEN }
)
if (scr === null || !scr.ok) {
  return { ok: false, reason: '筛选失败或数据未更新，不下结论', warnings: scr && scr.warnings }
}
const core = [...scr.core].sort((a, b) => (b.held ? 1 : 0) - (a.held ? 1 : 0))
// 平台上限：每次运行最多派 64 个子智能体（config.yaml 的 workflow 段）；扣掉筛选 1、call 3、周会稿 1
const room = Math.max(0, 64 - 5 - core.length)
const watch = scr.flagged.slice(0, room)
if (scr.flagged.length > room) {
  log(
    `Watch 命中 ${scr.flagged.length} 家，超出平台派发上限，余下 ${
      scr.flagged.length - room
    } 家写进“口径与缺口”，下次优先`
  )
}

phase('Core 逐家')
const coreRes = await parallel(
  core.map((c) => () =>
    agent(
      `${base} 公司 ${c.ticker}${c.held ? '（持仓）' : ''}，知识层在 ${c.workspace_dir || `../${c.ticker}`}。按 ${S}/references/core-five-sources.md 做本周（${scr.base} 至 ${E}）五源检索，归因数据见 ${R}/screen.json 与 ${R}/attribution.json。写 ${R}/core-${c.ticker}.md；若有知识层修改建议，写提案 ${R}/proposals/${c.ticker}.json（不直接改公司工作区知识层）。`,
      { agentType: 'dalton-evidence', label: String(c.ticker), phase: 'Core 逐家', schema: CO }
    )
  )
)

phase('Watch 命中')
const watchRes = await parallel(
  watch.map((w) => () =>
    agent(
      `${base} Watch 公司 ${w.ticker}（提示：${w.event_hint || '异动命中'}）。按 ${S}/references/watch-rules.md 定向检索，写 ${R}/watch-${w.ticker}.md。`,
      { agentType: 'dalton-evidence', label: String(w.ticker), phase: 'Watch 命中', schema: CO }
    )
  )
)

phase('汇总与 call')
const okCore = coreRes.filter((x) => x !== null)
const cands = okCore.filter((x) => x.call_candidate).map((x) => x.ticker)
const calls =
  cands.length === 0
    ? []
    : await parallel(
        cands.slice(0, 3).map((t) => () => {
          const c = core.find((y) => y.ticker === t) || { ticker: t, workspace_dir: `../${t}` }
          return agent(
            `按 .agents/skills/dalton-call/SKILL.md 对 ${t} 做 conviction 检查（知识层 ${
              c.workspace_dir || `../${t}`
            }，本周材料 ${R}/core-${t}.md）。证据门槛不过就输出“暂无高 conviction”及缺什么。写 ${R}/call-${t}.md。`,
            { agentType: 'general-purpose', label: `call ${t}`, phase: '汇总与 call', schema: CO }
          )
        })
      )

const doc = await agent(
  `${base} 按 ${S}/templates/weekly.md 汇总 ${R}/ 下全部文件为周会稿 ${K}/drafts/${E}-weekly.md。观点变化部分读各 Core 工作区本周 verdicts.md 与 ${R}/proposals/。Core 失败的公司：${
    core
      .filter((_, i) => coreRes[i] === null)
      .map((c) => c.ticker)
      .join(',') || '无'
  }，在“口径与缺口”写明。`,
  { agentType: 'general-purpose', label: '周会稿', phase: '汇总与 call', schema: CO }
)

return {
  ok: doc !== null,
  draft: `${K}/drafts/${E}-weekly.md`,
  core_done: okCore.length,
  core_total: core.length,
  watch_done: watchRes.filter((x) => x !== null).length,
  calls: calls.filter((x) => x !== null).map((x) => x.ticker),
  proposals_dir: `${R}/proposals`,
}
