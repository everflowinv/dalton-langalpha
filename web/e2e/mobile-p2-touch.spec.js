/**
 * Mobile Web P2 (390x844 portrait, hasTouch: true) touch-target & polish audit.
 *
 * Inspects visible icon buttons across:
 *   - Chat page (/chat/t/:id): topbar, message bubble actions, code block horizontal scroll, floating composer safe-area
 *   - Monitors pages (/monitors and /monitors/:slug): list refresh, detail topbar wrapping, 36px version select, action buttons
 *   - File panel (tree view + opened HTML report): TabStrip, TreeColumn, breadcrumb actions
 *   - Settings pages (/settings?tab=general and /settings?tab=model): search toggle, 34px pills, scrollbar-gutter: auto, PerModelMatrix sticky first column & reset buttons
 *
 * Asserts every visible icon button is >= 44px on both axes OR carries `.tap-target`,
 * and every scanned page stays within 390px horizontally (`scrollWidth <= 390`).
 *
 * Run outside sandbox via Playwright (`cd web && pnpm test:e2e e2e/mobile-p2-touch.spec.js`).
 */
import { test, expect, mockAPI, configureSSE, resetMockServer } from './fixtures.js';
import { defaultResponses, sampleWorkspace, sampleThread, sseEvents } from './helpers/mockResponses.js';

const VIEWPORT = { width: 390, height: 844 };

const WS_ID = 'a0000002-0000-4000-8000-000000000001';
const THREAD_ID = 'b0000002-0000-4000-8000-000000000001';
const ARTIFACT_ID = '6f1c2a7e-3b4d-4e5f-8a9b-0c1d2e3f4a5c';
const MONITOR_SLUG = 'portfolio-beta';

const ALL_CAPABILITIES = [
  'researchdata_bridge',
  'alphaengine',
  'guidepoint',
  'feishu_docs',
  'channel_gateway',
  'channel_feishu',
  'channel_discord',
  'channel_weixin',
  'data_jobs',
  'conversation_archive',
  'native_oauth_sync',
  'transcription',
  'colleague_sharing',
];

const SAMPLE_MONITOR_SUMMARY = {
  monitor_id: '60000000-0000-4000-8000-000000000002',
  slug: MONITOR_SLUG,
  artifact_id: ARTIFACT_ID,
  artifact_code: 'mob-p2-mon',
  workspace_id: WS_ID,
  entry_path: 'reports/q3_margin_deep_dive.html',
  owner_user_id: 'local-dev-user',
  owner_name: 'Alice',
  owner_email: 'alice@example.com',
  title: 'Portfolio Beta & Cross-Asset Risk Monitor',
  category: 'portfolio',
  sort_order: 0,
  visibility: 'company',
  member_user_ids: [],
  published_seq: 3,
  keep_updated: true,
  source_kind: 'workspace',
  source_ref: `${WS_ID}:reports/q3_margin_deep_dive.html`,
  acl_revision: 1,
  last_publish_error: null,
  last_publish_failed_at: null,
  data_as_of: '2026-11-24T10:00:00Z',
  created_at: '2026-11-20T08:00:00Z',
  updated_at: '2026-11-24T10:00:00Z',
  can_manage: true,
};

const SAMPLE_MONITOR_DETAIL = {
  monitor: SAMPLE_MONITOR_SUMMARY,
  versions: [
    {
      seq: 3,
      entry_path: 'reports/q3_margin_deep_dive.html',
      origin: 'owner',
      title: SAMPLE_MONITOR_SUMMARY.title,
      label: 'Latest',
      byte_len: 2048,
      published_at: '2026-11-24T10:00:00Z',
    },
    {
      seq: 2,
      entry_path: 'reports/q3_margin_deep_dive.html',
      origin: 'owner',
      title: SAMPLE_MONITOR_SUMMARY.title,
      label: 'Previous',
      byte_len: 1900,
      published_at: '2026-11-23T10:00:00Z',
    },
  ],
  monitor_grant: 'm1.e2e-grant-token',
  frame_base: '/api/v1/dalton/monitors/g/m1.e2e-grant-token/',
  data_base: '/api/v1/dalton/monitors/g/m1.e2e-grant-token/data',
  expires_in: 43200,
};

const SAMPLE_ARTIFACT = {
  artifact_id: ARTIFACT_ID,
  code: 'mob-p2-art',
  url: '/a/mob-p2-art',
  workspace_id: WS_ID,
  path: 'reports/q3_margin_deep_dive.html',
  name: 'q3_margin_deep_dive.html',
  shared: true,
  shared_at: '2026-11-20T08:00:00Z',
  shared_seq: 3,
  keep_updated: false,
  latest_seq: 3,
  bound_thread_id: null,
  open_comments: 0,
  unread: 0,
  created_at: '2026-11-20T08:00:00Z',
};

test.use({
  viewport: VIEWPORT,
  isMobile: true,
  hasTouch: true,
});

async function assertNoHorizontalOverflow(page, label) {
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(
    scrollWidth,
    `${label}: horizontal overflow (${scrollWidth}px > ${VIEWPORT.width}px)`,
  ).toBeLessThanOrEqual(VIEWPORT.width);
}

/**
 * Inspects every visible, enabled icon button (a button or icon-only link
 * containing an `<svg>` whose visible text is empty or whose layout box is
 * smaller than 44x44) inside `scopeSelector` (defaults to `body`).
 * Every such icon button must either have `width >= 44 && height >= 44` or
 * carry `.tap-target` (directly or via a dedicated `.tap-target` child).
 */
async function assertIconButtonsMeetTouchTarget(page, label, scopeSelector = 'body') {
  const result = await page.evaluate((selector) => {
    const root = document.querySelector(selector);
    if (!root) return { checked: 0, violations: [`Scope not found: ${selector}`] };
    const candidates = Array.from(root.querySelectorAll('button, a[href]'));
    const violations = [];
    let checked = 0;

    for (const el of candidates) {
      if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') continue;
      if (el.closest('[aria-hidden="true"], [inert]')) continue;
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      // Ignore off-screen items inside hidden drawers or scrolled regions
      if (rect.right <= 0 || rect.left >= window.innerWidth || rect.bottom <= 0 || rect.top >= window.innerHeight) {
        continue;
      }

      const hasSvg = Boolean(el.querySelector('svg'));
      if (!hasSvg) continue;

      // Determine whether this control has any visible non-SVG text label
      let visibleText = '';
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      let node = walker.nextNode();
      while (node) {
        const text = (node.textContent || '').trim();
        if (text) {
          const parent = node.parentElement;
          const hiddenParent = parent && (
            parent.closest('svg, .sr-only') ||
            window.getComputedStyle(parent).display === 'none' ||
            window.getComputedStyle(parent).visibility === 'hidden'
          );
          if (!hiddenParent) visibleText += text;
        }
        node = walker.nextNode();
      }
      if (visibleText.length > 0) continue;

      checked += 1;
      const meetsSize = rect.width >= 44 && rect.height >= 44;
      const hasTapTarget =
        el.classList.contains('tap-target') || Boolean(el.querySelector('.tap-target'));

      if (!meetsSize && !hasTapTarget) {
        const desc =
          el.getAttribute('aria-label') ||
          el.getAttribute('title') ||
          el.getAttribute('data-testid') ||
          el.className ||
          el.tagName;
        violations.push(
          `${desc} (${Math.round(rect.width)}x${Math.round(rect.height)}px, classes="${String(el.className)}")`,
        );
        continue;
      }

      // Hit-test center of the button to verify no neighbor's ::after overlaps its center
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      if (cx >= 1 && cx <= window.innerWidth - 1 && cy >= 1 && cy <= window.innerHeight - 1) {
        const hit = document.elementFromPoint(cx, cy);
        if (hit && !el.contains(hit) && !hit.contains(el)) {
          const desc =
            el.getAttribute('aria-label') ||
            el.getAttribute('title') ||
            el.getAttribute('data-testid') ||
            el.tagName;
          const hitDesc =
            hit.closest('button, a[href]')?.getAttribute('aria-label') ||
            hit.closest('button, a[href]')?.getAttribute('title') ||
            hit.tagName;
          violations.push(`${desc} center (${Math.round(cx)},${Math.round(cy)}) occluded by ${hitDesc}`);
        }
      }
    }

    return { checked, violations };
  }, scopeSelector);

  expect(result.checked, `${label}: expected at least one visible icon button`).toBeGreaterThan(0);
  expect(
    result.violations,
    `${label}: icon buttons under 44px without .tap-target:\n${result.violations.join('\n')}`,
  ).toEqual([]);
}

function baseMocks() {
  const ws = sampleWorkspace({ workspace_id: WS_ID, name: 'NVIDIA Earnings & Supply Chain' });
  const thread = sampleThread({
    thread_id: THREAD_ID,
    workspace_id: WS_ID,
    title: 'NVIDIA FY2026 Gross Margin Bridge & Supply Chain Deep Dive Analysis',
  });
  return {
    'GET /dalton/capabilities': {
      profile: 'everflow',
      capabilities: ALL_CAPABILITIES,
      brand_name: 'Dalton',
    },
    'GET /dalton/credentials/status': {
      sharing: 'team',
      enabled: true,
      holder_user_id: 'local-dev-user',
      credential_user_id: 'local-dev-user',
      is_pm: true,
      can_disconnect: true,
      can_invite: true,
      caller_tier: 'admin',
      colleague_sharing_enabled: true,
    },
    'GET /workspaces': { workspaces: [ws], total: 1, limit: 20, offset: 0 },
    'GET /workspaces/*': ws,
    'GET /threads': { threads: [thread], total: 1, limit: 20, offset: 0 },
    'GET /threads/*': thread,
    'GET /threads/*/messages': { messages: [], has_more: false },
    'GET /dalton/tracking/*': {
      ok: true,
      workspace_id: WS_ID,
      ticker: 'NVDA',
      tier: 'tier1',
      timezone: 'America/New_York',
      status: 'enabled',
      enabled: true,
      paused_by_user: false,
      label: 'NVDA',
      missing_tasks: [],
      paused_tasks: [],
      active_tasks: [],
      can_edit: true,
      tasks: [],
    },
    'GET /dalton/workspaces/*/sharing': {
      workspace_id: WS_ID,
      cos_access: 'ro',
      web_access: 'ro',
      is_home: false,
      can_edit: true,
      colleague_dispatch_enabled: true,
    },
    'GET /dalton/workspaces/*/settings': {
      workspace_id: WS_ID,
      team_memory_enabled: false,
      is_home: false,
      can_edit: true,
      owner_external: false,
    },
    'GET /dalton/coverage/units': { enabled: false },
    'GET /dalton/monitors': { monitors: [SAMPLE_MONITOR_SUMMARY] },
    [`GET /dalton/monitors/${MONITOR_SLUG}`]: SAMPLE_MONITOR_DETAIL,
    'GET /dalton/monitors/by-artifact/*': { monitor: null },
    'GET /workspaces/*/files': {
      files: ['reports/q3_margin_deep_dive.html', 'reports/notes.md'],
    },
    'GET /workspaces/*/files/read': {
      content: '<html><body><h1>Q3 Margin Deep Dive</h1></body></html>',
      mime: 'text/html',
      truncated: false,
    },
    'POST /workspaces/*/file-grant': {
      prefix: '/api/v1/wsfiles/g/grant-p2/',
      expires_in: 3600,
    },
    'GET /workspaces/*/artifacts': { artifact: SAMPLE_ARTIFACT },
    'POST /workspaces/*/artifacts': SAMPLE_ARTIFACT,
    'GET /artifacts/*/versions': { versions: SAMPLE_MONITOR_DETAIL.versions },
    'GET /artifacts/*/comments': { comments: [] },
  };
}

test.beforeEach(async () => {
  await resetMockServer();
});

test.describe('Mobile P2 touch targets & layout polish (390x844, hasTouch)', () => {
  test('chat page icon buttons meet 44px/.tap-target, composer has safe-area class, code blocks scroll horizontally, and drawer row actions do not overlap', async ({ page }) => {
    await mockAPI(page, baseMocks());
    await configureSSE({
      method: 'GET',
      path: `/api/v1/threads/${THREAD_ID}/messages/replay`,
      delay: 10,
      events: [
        sseEvents.userMessage('Show the margin bridge table in Python.', 0),
        sseEvents.toolCalls([
          {
            name: 'Task',
            args: { description: 'Margin sensitivity check', prompt: 'Check margin sensitivity' },
            id: 'toolu_p2_task',
          },
        ]),
        sseEvents.finishToolCalls(),
        {
          event: 'artifact',
          data: {
            thread_id: THREAD_ID,
            agent: 'main',
            artifact_id: 'task:sub-margin',
            artifact_type: 'task',
            tool_call_id: 'toolu_p2_task',
            status: 'completed',
            payload: {
              task_id: 'sub-margin',
              action: 'init',
              status: 'completed',
              last_activity_ms: Date.now() + 60_000,
              description: 'Margin sensitivity check',
              prompt: 'Check margin sensitivity',
              type: 'research',
            },
          },
        },
        sseEvents.toolCallResult('toolu_p2_task', 'Background subagent deployed: Task-sub-margin'),
        sseEvents.messageChunk(
          'Here is the margin bridge script:\n\n```python\nEBITDA_BRIDGE_TABLE_FY2026 = {"gross_margin_bps": 240, "operating_leverage_bps": 185, "net_margin_expansion_bps": 425, " guidance_consensus_delta_bps": 110}\n```\n',
        ),
        sseEvents.finishStop(),
        sseEvents.replayDone(THREAD_ID),
      ],
    });

    await page.goto(`/chat/t/${THREAD_ID}`);
    const topbar = page.locator('[data-handoff="header"]').first();
    await expect(topbar).toBeVisible({ timeout: 10000 });
    const codeScroller = page.locator('[data-code-scroll="horizontal"]').first();
    await expect(codeScroller).toBeVisible({ timeout: 10000 });

    // Perform real horizontal scroll on the Markdown code block without widening the page
    const codeScrollResult = await codeScroller.evaluate((el) => {
      el.scrollLeft = 80;
      return {
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
        scrollLeft: el.scrollLeft,
      };
    });
    expect(codeScrollResult.scrollWidth).toBeGreaterThan(codeScrollResult.clientWidth);
    expect(codeScrollResult.scrollLeft).toBeGreaterThan(0);

    // Floating composer wrapper carries safe-area-inset-bottom padding on mobile
    const composerWrap = page.locator('.absolute.inset-x-0.bottom-0.pointer-events-none').first();
    await expect(composerWrap).toBeVisible();
    await expect(composerWrap).toHaveAttribute('class', /safe-area-inset-bottom/);

    await assertNoHorizontalOverflow(page, 'chat page');
    await assertIconButtonsMeetTouchTarget(page, 'chat page');

    // Materialize the replayed subagent card into sidebarAgentRows before opening the drawer
    const subagentBtn = page.getByRole('button', { name: /Margin sensitivity check/ }).first();
    await expect(subagentBtn).toBeVisible({ timeout: 10000 });
    await subagentBtn.click();
    await expect(page).toHaveURL(new RegExp(`/chat/t/${THREAD_ID}/sub-margin`), { timeout: 10000 });
    await topbar.locator('button').first().click();
    await expect(page).toHaveURL(new RegExp(`/chat/t/${THREAD_ID}$`), { timeout: 10000 });

    // Open mobile navigation drawer and verify adjacent row buttons (new-thread vs options, agents vs pin vs archive) never overlap
    const drawerToggle = topbar.locator('button svg.lucide-menu').locator('..');
    await drawerToggle.click();
    const drawer = page.locator('.nav-panel').first();
    await expect(drawer).toBeVisible({ timeout: 10000 });
    await expect.poll(async () => (await drawer.boundingBox())?.x ?? -999, { timeout: 5000 }).toBeGreaterThanOrEqual(0);

    const newThreadBtn = drawer.locator('button[data-nav-key="new-thread"]').first();
    const optionsBtn = drawer.locator('button[data-nav-key="options"]').first();
    const agentsBtn = drawer.locator(`button[data-nav-key="thread:${THREAD_ID}:agents"]`).first();
    const pinBtn = drawer.locator(`button[data-nav-key="thread:${THREAD_ID}:pin"]`).first();
    const archiveBtn = drawer.locator(`button[data-nav-key="thread:${THREAD_ID}:archive"]`).first();

    await expect(newThreadBtn).toBeVisible({ timeout: 10000 });
    await expect(optionsBtn).toBeVisible({ timeout: 10000 });
    await expect(agentsBtn).toBeVisible({ timeout: 10000 });
    await expect(pinBtn).toBeVisible({ timeout: 10000 });
    await expect(archiveBtn).toBeVisible({ timeout: 10000 });

    const navHitCheck = await drawer.evaluate((drawerEl, tid) => {
      function checkPair(selA, selB) {
        const a = drawerEl.querySelector(selA);
        const b = drawerEl.querySelector(selB);
        if (!a || !b) return { ok: false, reason: `Missing ${selA} or ${selB}` };
        const rectA = a.getBoundingClientRect();
        const rectB = b.getBoundingClientRect();
        const afterA = window.getComputedStyle(a, '::after');
        const afterB = window.getComputedStyle(b, '::after');
        const hitWA = Math.max(rectA.width, parseFloat(afterA.width) || rectA.width);
        const hitWB = Math.max(rectB.width, parseFloat(afterB.width) || rectB.width);
        const rightA = rectA.left + rectA.width / 2 + hitWA / 2;
        const leftB = rectB.left + rectB.width / 2 - hitWB / 2;
        const cxA = rectA.left + rectA.width / 2;
        const cyA = rectA.top + rectA.height / 2;
        const cxB = rectB.left + rectB.width / 2;
        const cyB = rectB.top + rectB.height / 2;
        const hitAtA = document.elementFromPoint(cxA, cyA);
        const hitAtB = document.elementFromPoint(cxB, cyB);
        return {
          ok: rightA <= leftB + 0.5 && a.contains(hitAtA) && b.contains(hitAtB),
          rightA,
          leftB,
          hitAOk: Boolean(hitAtA && a.contains(hitAtA)),
          hitBOk: Boolean(hitAtB && b.contains(hitAtB)),
        };
      }

      const agentsEl = drawerEl.querySelector(`button[data-nav-key="thread:${tid}:agents"]`);
      const titleEl = drawerEl.querySelector(`[data-nav-key="thread:${tid}:title"]`);
      let agentsTitleClear = false;
      let agentsTitleDetail = null;
      if (agentsEl && titleEl) {
        const agentsRect = agentsEl.getBoundingClientRect();
        const titleRect = titleEl.getBoundingClientRect();
        const agentsAfter = window.getComputedStyle(agentsEl, '::after');
        const agentsHitW = Math.max(agentsRect.width, parseFloat(agentsAfter.width) || agentsRect.width);
        const agentsHitLeft = agentsRect.left + agentsRect.width / 2 - agentsHitW / 2;
        agentsTitleClear = titleRect.right <= agentsHitLeft + 0.5 && agentsRect.width >= 36;
        agentsTitleDetail = {
          titleRight: Math.round(titleRect.right),
          agentsHitLeft: Math.round(agentsHitLeft),
          agentsWidth: Math.round(agentsRect.width),
        };
      }

      return {
        wsPair: checkPair('button[data-nav-key="new-thread"]', 'button[data-nav-key="options"]'),
        agentsPinPair: checkPair(
          `button[data-nav-key="thread:${tid}:agents"]`,
          `button[data-nav-key="thread:${tid}:pin"]`,
        ),
        threadPair: checkPair(
          `button[data-nav-key="thread:${tid}:pin"]`,
          `button[data-nav-key="thread:${tid}:archive"]`,
        ),
        agentsTitleClear,
        agentsTitleDetail,
      };
    }, THREAD_ID);

    expect(navHitCheck.wsPair.ok, `Workspace new-thread/options overlap: ${JSON.stringify(navHitCheck.wsPair)}`).toBe(true);
    expect(navHitCheck.agentsPinPair.ok, `Thread agents/pin overlap: ${JSON.stringify(navHitCheck.agentsPinPair)}`).toBe(true);
    expect(navHitCheck.threadPair.ok, `Thread pin/archive overlap: ${JSON.stringify(navHitCheck.threadPair)}`).toBe(true);
    expect(navHitCheck.agentsTitleClear, `Thread agents button overlaps title: ${JSON.stringify(navHitCheck.agentsTitleDetail)}`).toBe(true);
    await assertIconButtonsMeetTouchTarget(page, 'mobile nav drawer', '.nav-panel');
  });

  test('monitors list and detail pages meet 44px/.tap-target, version select is 36px, and zero overflow', async ({ page }) => {
    await mockAPI(page, baseMocks());

    // 1. Monitors list
    await page.goto('/monitors');
    await expect(page.getByText(SAMPLE_MONITOR_SUMMARY.title)).toBeVisible({ timeout: 10000 });
    await assertNoHorizontalOverflow(page, 'monitors list');
    await assertIconButtonsMeetTouchTarget(page, 'monitors list');

    // 2. Monitor detail
    await page.goto(`/monitors/${MONITOR_SLUG}`);
    const topbar = page.locator('.monitors-topbar').first();
    await expect(topbar).toBeVisible({ timeout: 10000 });
    const versionSelectWrapper = topbar.locator('.monitors-version-select').first();
    await expect(versionSelectWrapper).toBeVisible({ timeout: 10000 });
    const versionSelect = versionSelectWrapper.locator('select').first();
    await expect(versionSelect).toBeVisible({ timeout: 10000 });
    const selectBox = await versionSelect.boundingBox();
    expect(selectBox?.height ?? 0).toBeGreaterThanOrEqual(36);

    await assertNoHorizontalOverflow(page, 'monitor detail');
    await assertIconButtonsMeetTouchTarget(page, 'monitor detail', '.monitors-topbar');
  });

  test('file panel tree and opened HTML report meet 44px/.tap-target with zero horizontal overflow', async ({ page }) => {
    await mockAPI(page, baseMocks());

    await page.goto(`/chat/t/${THREAD_ID}`);
    await page.locator('[data-panel-toggle="header"]').first().click();

    // 1. File tree view inside panel
    const reportsDir = page.locator('[role="treeitem"][data-row-kind="dir"][data-row-path="reports"]').first();
    await expect(reportsDir).toBeVisible({ timeout: 10000 });
    await assertNoHorizontalOverflow(page, 'file panel tree');
    await assertIconButtonsMeetTouchTarget(page, 'file panel tree', '.file-panel');

    // 2. Open HTML report
    await reportsDir.click();
    const htmlItem = page
      .locator('[role="treeitem"][data-row-kind="file"][data-row-path="reports/q3_margin_deep_dive.html"]')
      .first();
    await expect(htmlItem).toBeVisible({ timeout: 10000 });
    await htmlItem.click();

    await expect(page.locator('.file-panel-crumbs').first()).toBeVisible({ timeout: 10000 });

    // Verify tab close button does not overlap tab title and .file-panel-tabs scrollport is at least 44px tall
    const tabCheck = await page.evaluate(() => {
      const tabsEl = document.querySelector('.file-panel-tabs');
      const tabName = document.querySelector('.file-panel-tab-name');
      const closeBtn = document.querySelector('.file-panel-tab-close');
      const crumbsEl = document.querySelector('.file-panel-crumbs');
      if (!tabsEl || !tabName || !closeBtn || !crumbsEl) return { ok: false, reason: 'missing tab/crumb elements' };
      const tabsRect = tabsEl.getBoundingClientRect();
      const nameRect = tabName.getBoundingClientRect();
      const closeRect = closeBtn.getBoundingClientRect();
      const crumbsRect = crumbsEl.getBoundingClientRect();
      const closeAfter = window.getComputedStyle(closeBtn, '::after');
      const closeHitW = Math.max(closeRect.width, parseFloat(closeAfter.width) || closeRect.width);
      const closeHitLeft = closeRect.left + closeRect.width / 2 - closeHitW / 2;
      const hitOnClose = document.elementFromPoint(
        closeRect.left + closeRect.width / 2,
        closeRect.top + closeRect.height / 2,
      );
      const hitOnNameEnd = document.elementFromPoint(
        Math.max(nameRect.left + 2, nameRect.right - 2),
        nameRect.top + nameRect.height / 2,
      );
      return {
        ok:
          tabsRect.height >= 44 &&
          crumbsRect.height >= 44 &&
          nameRect.right <= closeHitLeft + 0.5 &&
          closeBtn.contains(hitOnClose) &&
          !closeBtn.contains(hitOnNameEnd),
        tabsHeight: Math.round(tabsRect.height),
        crumbsHeight: Math.round(crumbsRect.height),
        nameRight: Math.round(nameRect.right),
        closeHitLeft: Math.round(closeHitLeft),
      };
    });
    expect(tabCheck.ok, `Tab strip / crumbs hit-box check failed: ${JSON.stringify(tabCheck)}`).toBe(true);

    await assertNoHorizontalOverflow(page, 'file panel html');
    await assertIconButtonsMeetTouchTarget(page, 'file panel html', '.file-panel');
  });

  test('settings pages have 34px pills, scrollbar-gutter: auto, sticky PerModelMatrix first column, and 44px/.tap-target icon buttons', async ({ page }) => {
    const basePrefs = defaultResponses['GET /users/me/preferences'];
    await mockAPI(page, {
      ...baseMocks(),
      'GET /users/me/preferences': {
        ...basePrefs,
        model_preference: {
          preferred_model: 'claude-sonnet-4-20250514',
          profiles: {
            'claude-sonnet-4-20250514': { reasoning_effort: 'high' },
          },
        },
      },
      'GET /models': {
        models: {
          anthropic: {
            display_name: 'Anthropic',
            models: ['claude-sonnet-4-20250514'],
          },
        },
        model_metadata: {
          'claude-sonnet-4-20250514': {
            display_name: 'Claude Sonnet 4 (Very Long Display Name)',
            provider: 'anthropic',
            reasoning_efforts: ['low', 'medium', 'high'],
          },
        },
      },
    });

    // 1. Settings General
    await page.goto('/settings?tab=general');
    await expect(page.locator('.settings-column').first()).toBeVisible({ timeout: 10000 });

    const scrollGutter = await page
      .locator('.settings-scroll')
      .first()
      .evaluate((el) => window.getComputedStyle(el).scrollbarGutter);
    expect(scrollGutter).toBe('auto');

    const pill = page.locator('.settings-rail .page-rail-link').first();
    await expect(pill).toBeVisible();
    const pillBox = await pill.boundingBox();
    expect(pillBox?.height ?? 0).toBeGreaterThanOrEqual(34);
    await expect(pill).toHaveClass(/tap-target/);

    const railNavBox = await page.locator('.settings-rail-nav').first().boundingBox();
    expect(railNavBox?.height ?? 0).toBeGreaterThanOrEqual(44);

    await assertNoHorizontalOverflow(page, 'settings general');
    await assertIconButtonsMeetTouchTarget(page, 'settings general');

    // 2. Settings Model (PerModelMatrix sits under Advanced + daltonRouting disclosure; #per-model auto-expands both)
    await page.goto('/settings?tab=model#per-model');
    const matrixFirstHeader = page.locator('#per-model table thead th:first-child').first();
    await expect(matrixFirstHeader).toBeVisible({ timeout: 10000 });
    const headerSticky = await matrixFirstHeader.evaluate((el) => {
      const cs = window.getComputedStyle(el);
      return { position: cs.position, left: cs.left };
    });
    expect(headerSticky.position).toBe('sticky');
    expect(headerSticky.left).toBe('0px');

    const matrixFirstCell = page.locator('#per-model table tbody tr td:first-child').first();
    await expect(matrixFirstCell).toBeVisible({ timeout: 10000 });
    const stickyStyle = await matrixFirstCell.evaluate((el) => {
      const cs = window.getComputedStyle(el);
      return { position: cs.position, left: cs.left };
    });
    expect(stickyStyle.position).toBe('sticky');
    expect(stickyStyle.left).toBe('0px');

    // Scroll PerModelMatrix horizontally and verify first column th & td remain pinned at left edge
    const matrixScrollCheck = await page.locator('#per-model').first().evaluate((sectionEl) => {
      const scroller = sectionEl.querySelector('.overflow-x-auto');
      const th1 = sectionEl.querySelector('table thead th:first-child');
      const th2 = sectionEl.querySelector('table thead th:nth-child(2)');
      const td1 = sectionEl.querySelector('table tbody tr td:first-child');
      if (!scroller || !th1 || !th2 || !td1) return { ok: false, reason: 'missing table elements' };
      const th1Before = th1.getBoundingClientRect().left;
      const td1Before = td1.getBoundingClientRect().left;
      const th2Before = th2.getBoundingClientRect().left;
      scroller.scrollLeft = 180;
      const th1After = th1.getBoundingClientRect().left;
      const td1After = td1.getBoundingClientRect().left;
      const th2After = th2.getBoundingClientRect().left;
      return {
        ok:
          scroller.scrollLeft > 0 &&
          Math.abs(th1After - th1Before) <= 1 &&
          Math.abs(td1After - td1Before) <= 1 &&
          th2After < th2Before - 100,
        scrollLeft: scroller.scrollLeft,
        th1Delta: th1After - th1Before,
        td1Delta: td1After - td1Before,
        th2Delta: th2After - th2Before,
      };
    });
    expect(matrixScrollCheck.ok, `PerModelMatrix sticky scroll check failed: ${JSON.stringify(matrixScrollCheck)}`).toBe(
      true,
    );

    const resetBtn = page.locator('#per-model table tbody tr td:last-child button').first();
    await expect(resetBtn).toHaveClass(/tap-target/);

    await assertNoHorizontalOverflow(page, 'settings model');
    await assertIconButtonsMeetTouchTarget(page, 'settings model');
  });
});

test.describe('Tablet coarse-pointer sidebar (>=768px, hasTouch)', () => {
  test.use({
    viewport: { width: 820, height: 1180 },
    isMobile: true,
    hasTouch: true,
  });

  test('row actions stay inline and never occlude thread agents toggle at 820px width', async ({ page }) => {
    const mocks = baseMocks();
    const longThread = {
      ...sampleThread({
        thread_id: THREAD_ID,
        workspace_id: WS_ID,
        title: 'Very Long Q3 Margin Bridge Sensitivity & Cross-Asset Risk Thread Title That Must Truncate Cleanly',
      }),
      is_pinned: false,
    };
    mocks['GET /threads'] = { threads: [longThread], total: 1, limit: 20, offset: 0 };
    mocks['GET /threads/*'] = longThread;
    await mockAPI(page, mocks);
    await configureSSE({
      method: 'GET',
      path: `/api/v1/threads/${THREAD_ID}/messages/replay`,
      delay: 10,
      events: [
        sseEvents.userMessage('Show the margin bridge table.', 0),
        sseEvents.toolCalls([
          {
            name: 'Task',
            args: { description: 'Margin sensitivity check', prompt: 'Check margin sensitivity' },
            id: 'toolu_tablet_task',
          },
        ]),
        sseEvents.finishToolCalls(),
        {
          event: 'artifact',
          data: {
            thread_id: THREAD_ID,
            agent: 'main',
            artifact_id: 'task:sub-margin',
            artifact_type: 'task',
            tool_call_id: 'toolu_tablet_task',
            status: 'completed',
            payload: {
              task_id: 'sub-margin',
              action: 'init',
              status: 'completed',
              last_activity_ms: Date.now() + 60_000,
              description: 'Margin sensitivity check',
              prompt: 'Check margin sensitivity',
              type: 'research',
            },
          },
        },
        sseEvents.toolCallResult('toolu_tablet_task', 'Background subagent deployed: Task-sub-margin'),
        sseEvents.finishStop(),
        sseEvents.replayDone(THREAD_ID),
      ],
    });

    await page.goto(`/chat/t/${THREAD_ID}`);
    const topbar = page.locator('[data-handoff="header"]').first();
    await expect(topbar).toBeVisible({ timeout: 10000 });

    const subagentBtn = page.getByRole('button', { name: /Margin sensitivity check/ }).first();
    await expect(subagentBtn).toBeVisible({ timeout: 10000 });
    await subagentBtn.click();
    await expect(page).toHaveURL(new RegExp(`/chat/t/${THREAD_ID}/sub-margin`), { timeout: 10000 });
    await topbar.locator('button').first().click();
    await expect(page).toHaveURL(new RegExp(`/chat/t/${THREAD_ID}$`), { timeout: 10000 });

    const sidebarNav = page.locator('.sidebar .nav-panel').first();
    await expect(sidebarNav).toBeVisible({ timeout: 10000 });

    const agentsBtn = sidebarNav.locator(`button[data-nav-key="thread:${THREAD_ID}:agents"]`).first();
    const pinBtn = sidebarNav.locator(`button[data-nav-key="thread:${THREAD_ID}:pin"]`).first();
    const archiveBtn = sidebarNav.locator(`button[data-nav-key="thread:${THREAD_ID}:archive"]`).first();

    await expect(agentsBtn).toBeVisible({ timeout: 10000 });
    await expect(pinBtn).toBeVisible({ timeout: 10000 });
    await expect(archiveBtn).toBeVisible({ timeout: 10000 });

    const tabletHitCheck = await sidebarNav.evaluate((panelEl, tid) => {
      const agents = panelEl.querySelector(`button[data-nav-key="thread:${tid}:agents"]`);
      const pin = panelEl.querySelector(`button[data-nav-key="thread:${tid}:pin"]`);
      const archive = panelEl.querySelector(`button[data-nav-key="thread:${tid}:archive"]`);
      const titleEl = panelEl.querySelector(`[data-nav-key="thread:${tid}:title"]`);
      if (!agents || !pin || !archive || !titleEl) {
        return { ok: false, reason: 'missing sidebar buttons or title' };
      }
      const rAgents = agents.getBoundingClientRect();
      const rPin = pin.getBoundingClientRect();
      const rArchive = archive.getBoundingClientRect();
      const rTitle = titleEl.getBoundingClientRect();
      const hitAgents = document.elementFromPoint(rAgents.left + rAgents.width / 2, rAgents.top + rAgents.height / 2);
      const hitPin = document.elementFromPoint(rPin.left + rPin.width / 2, rPin.top + rPin.height / 2);
      const hitArchive = document.elementFromPoint(rArchive.left + rArchive.width / 2, rArchive.top + rArchive.height / 2);
      const agentsOpacity = Number(window.getComputedStyle(agents).opacity);
      return {
        ok:
          agentsOpacity === 1 &&
          rTitle.right <= rAgents.left + 0.5 &&
          rAgents.right <= rPin.left + 0.5 &&
          rPin.right <= rArchive.left + 0.5 &&
          agents.contains(hitAgents) &&
          pin.contains(hitPin) &&
          archive.contains(hitArchive),
        agentsOpacity,
        titleRight: Math.round(rTitle.right),
        agentsLeft: Math.round(rAgents.left),
        agentsRight: Math.round(rAgents.right),
        pinLeft: Math.round(rPin.left),
        pinRight: Math.round(rPin.right),
        archiveLeft: Math.round(rArchive.left),
        hitAgentsKey: hitAgents?.closest?.('[data-nav-key]')?.getAttribute('data-nav-key') ?? null,
        hitPinKey: hitPin?.closest?.('[data-nav-key]')?.getAttribute('data-nav-key') ?? null,
        hitArchiveKey: hitArchive?.closest?.('[data-nav-key]')?.getAttribute('data-nav-key') ?? null,
      };
    }, THREAD_ID);

    expect(tabletHitCheck.ok, `Tablet row buttons overlap/occlude check failed: ${JSON.stringify(tabletHitCheck)}`).toBe(true);

    const beforeExpandedCount = await sidebarNav.locator('.nav-panel-agent-group').count();
    const agentsBox = await agentsBtn.boundingBox();
    expect(agentsBox).not.toBeNull();
    await page.touchscreen.tap(agentsBox.x + agentsBox.width / 2, agentsBox.y + agentsBox.height / 2);
    if (beforeExpandedCount === 0) {
      await expect(sidebarNav.locator('.nav-panel-agent-group')).toBeVisible({ timeout: 10000 });
    } else {
      await expect(sidebarNav.locator('.nav-panel-agent-group')).toHaveCount(0, { timeout: 10000 });
      await page.touchscreen.tap(agentsBox.x + agentsBox.width / 2, agentsBox.y + agentsBox.height / 2);
      await expect(sidebarNav.locator('.nav-panel-agent-group')).toBeVisible({ timeout: 10000 });
    }
    await expect(agentsBtn).toBeVisible();
  });

  const memberSectorUnitsResponse = () => ({
    enabled: true,
    units: [
      {
        unit_id: 'unit-sec-long',
        kind: 'sector',
        slug: 'long-sector',
        display_name: '全球半导体设备与先进封装超长行业分类名称',
        owner_user_id: 'local-dev-user',
        workspace_id: 'ws-sector-long',
        readable: true,
        delegatable: true,
        auto_tracked: true,
        members: [
          {
            member_id: 'm-member-nvda',
            name: 'NVDA',
            ticker: 'NVDA',
            company_unit_id: 'unit-member-nvda',
            workspace_created: true,
            workspace_id: WS_ID,
            scope: 'core',
            reason: '',
            valid_until: null,
          },
        ],
      },
      {
        unit_id: 'unit-member-nvda',
        kind: 'company',
        slug: 'nvda',
        display_name: 'NVDA',
        ticker: 'NVDA',
        owner_user_id: 'local-dev-user',
        workspace_id: WS_ID,
        readable: true,
        delegatable: false,
        auto_tracked: true,
      },
    ],
    unclassified: [],
  });

  const narrowWorkspaceListResponse = () => ({
    workspaces: [
      {
        workspace_id: WS_ID,
        user_id: 'local-dev-user',
        name: 'NVDA Semiconductor Research & Margin Architecture Workspace',
        status: 'running',
        is_home: false,
        is_pinned: false,
        created_at: '2026-10-01T00:00:00Z',
        updated_at: '2026-10-02T00:00:00Z',
      },
    ],
    total: 1,
    limit: 20,
    offset: 0,
  });

  for (const width of [220, 260]) {
    test(`member long sector chip truncates cleanly and options button opens menu at ${width}px sidebar width`, async ({ page }) => {
      const mocks = baseMocks();
      mocks['GET /workspaces'] = narrowWorkspaceListResponse();
      mocks['GET /dalton/coverage/units'] = memberSectorUnitsResponse();
      await mockAPI(page, mocks);
      await page.addInitScript((w) => {
        window.localStorage.setItem('app-sidebar-width', String(w));
      }, width);
      await configureSSE({
        method: 'GET',
        path: `/api/v1/threads/${THREAD_ID}/messages/replay`,
        delay: 10,
        events: [sseEvents.replayDone(THREAD_ID)],
      });

      await page.goto(`/chat/t/${THREAD_ID}`);
      const sidebar = page.locator('.sidebar').first();
      const sidebarNav = page.locator('.sidebar .nav-panel').first();
      await expect(sidebarNav).toBeVisible({ timeout: 10000 });

      const sidebarBox = await sidebar.boundingBox();
      expect(sidebarBox).not.toBeNull();
      expect(Math.abs(sidebarBox.width - width)).toBeLessThanOrEqual(4);

      const wsRow = sidebarNav.locator('[data-nav-key="header"]').first();
      const chip = wsRow.getByTestId('coverage-role-chip');
      const newThreadBtn = wsRow.locator('button[data-nav-key="new-thread"]');
      const optionsBtn = wsRow.locator('button[data-nav-key="options"]');
      await expect(chip).toHaveText('全球半导体设备与先进封装超长行业分类名称', { timeout: 10000 });
      await expect(newThreadBtn).toBeVisible({ timeout: 10000 });
      await expect(optionsBtn).toBeVisible({ timeout: 10000 });

      const layout = await wsRow.evaluate((rowEl) => {
        const chipEl = rowEl.querySelector('[data-testid="coverage-role-chip"]');
        const ntEl = rowEl.querySelector('button[data-nav-key="new-thread"]');
        const optEl = rowEl.querySelector('button[data-nav-key="options"]');
        if (!chipEl || !ntEl || !optEl) return { ok: false, reason: 'missing elements' };
        const rRow = rowEl.getBoundingClientRect();
        const rChip = chipEl.getBoundingClientRect();
        const rNt = ntEl.getBoundingClientRect();
        const rOpt = optEl.getBoundingClientRect();
        const hitOpt = document.elementFromPoint(rOpt.left + rOpt.width / 2, rOpt.top + rOpt.height / 2);
        return {
          ok:
            rChip.width <= 92 &&
            chipEl.scrollWidth > chipEl.clientWidth &&
            rChip.right <= rNt.left + 1 &&
            rNt.right <= rOpt.left + 1 &&
            rOpt.right <= rRow.right + 1 &&
            Boolean(hitOpt && optEl.contains(hitOpt)),
          rowRight: Math.round(rRow.right),
          chipWidth: Math.round(rChip.width),
          chipRight: Math.round(rChip.right),
          ntLeft: Math.round(rNt.left),
          optRight: Math.round(rOpt.right),
          hitOptOk: Boolean(hitOpt && optEl.contains(hitOpt)),
        };
      });
      expect(layout.ok, `Sidebar ${width}px chip/actions check failed: ${JSON.stringify(layout)}`).toBe(true);

      const optBox = await optionsBtn.boundingBox();
      expect(optBox).not.toBeNull();
      await page.touchscreen.tap(optBox.x + optBox.width / 2, optBox.y + optBox.height / 2);
      await expect(page.getByRole('menuitem').first()).toBeVisible({ timeout: 5000 });
      await page.keyboard.press('Escape');
    });
  }
});

test.describe('Narrow 320px phone drawer workspace chip & options fit (320x640, hasTouch)', () => {
  test.use({
    viewport: { width: 320, height: 640 },
    isMobile: true,
    hasTouch: true,
  });

  test('workspace long sector chip truncates cleanly inside 272px drawer and options button opens menu', async ({ page }) => {
    const mocks = baseMocks();
    mocks['GET /workspaces'] = {
      workspaces: [
        {
          workspace_id: WS_ID,
          user_id: 'local-dev-user',
          name: 'NVDA Semiconductor Research & Margin Architecture Workspace',
          status: 'running',
          is_home: false,
          is_pinned: false,
          created_at: '2026-10-01T00:00:00Z',
          updated_at: '2026-10-02T00:00:00Z',
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    };
    mocks['GET /dalton/coverage/units'] = {
      enabled: true,
      units: [
        {
          unit_id: 'unit-sec-long',
          kind: 'sector',
          slug: 'long-sector',
          display_name: '全球半导体设备与先进封装超长行业分类名称',
          owner_user_id: 'local-dev-user',
          workspace_id: 'ws-sector-long',
          readable: true,
          delegatable: true,
          auto_tracked: true,
          members: [
            {
              member_id: 'm-member-nvda',
              name: 'NVDA',
              ticker: 'NVDA',
              company_unit_id: 'unit-member-nvda',
              workspace_created: true,
              workspace_id: WS_ID,
              scope: 'core',
              reason: '',
              valid_until: null,
            },
          ],
        },
        {
          unit_id: 'unit-member-nvda',
          kind: 'company',
          slug: 'nvda',
          display_name: 'NVDA',
          ticker: 'NVDA',
          owner_user_id: 'local-dev-user',
          workspace_id: WS_ID,
          readable: true,
          delegatable: false,
          auto_tracked: true,
        },
      ],
      unclassified: [],
    };
    await mockAPI(page, mocks);
    await configureSSE({
      method: 'GET',
      path: `/api/v1/threads/${THREAD_ID}/messages/replay`,
      delay: 10,
      events: [sseEvents.replayDone(THREAD_ID)],
    });

    await page.goto(`/chat/t/${THREAD_ID}`);
    const topbar = page.locator('[data-handoff="header"]').first();
    await expect(topbar).toBeVisible({ timeout: 10000 });
    await topbar.locator('button svg.lucide-menu').locator('..').click();

    const drawer = page.locator('.nav-panel').first();
    await expect(drawer).toBeVisible({ timeout: 10000 });
    await expect.poll(async () => (await drawer.boundingBox())?.x ?? -999, { timeout: 5000 }).toBeGreaterThanOrEqual(0);

    const wsRow = drawer.locator('[data-nav-key="header"]').first();
    const chip = wsRow.getByTestId('coverage-role-chip');
    const optionsBtn = wsRow.locator('button[data-nav-key="options"]');
    await expect(chip).toHaveText('全球半导体设备与先进封装超长行业分类名称', { timeout: 10000 });
    await expect(optionsBtn).toBeVisible({ timeout: 10000 });

    const fit = await wsRow.evaluate((rowEl) => {
      const chipEl = rowEl.querySelector('[data-testid="coverage-role-chip"]');
      const ntEl = rowEl.querySelector('button[data-nav-key="new-thread"]');
      const optEl = rowEl.querySelector('button[data-nav-key="options"]');
      if (!chipEl || !ntEl || !optEl) return { ok: false, reason: 'missing elements' };
      const rRow = rowEl.getBoundingClientRect();
      const rChip = chipEl.getBoundingClientRect();
      const rNt = ntEl.getBoundingClientRect();
      const rOpt = optEl.getBoundingClientRect();
      const hitOpt = document.elementFromPoint(rOpt.left + rOpt.width / 2, rOpt.top + rOpt.height / 2);
      return {
        ok:
          rChip.width <= 92 &&
          chipEl.scrollWidth > chipEl.clientWidth &&
          rChip.right <= rNt.left + 1 &&
          rNt.right <= rOpt.left + 1 &&
          rOpt.right <= rRow.right + 1 &&
          optEl.contains(hitOpt),
        rowRight: Math.round(rRow.right),
        chipWidth: Math.round(rChip.width),
        optRight: Math.round(rOpt.right),
        hitOptOk: Boolean(hitOpt && optEl.contains(hitOpt)),
      };
    });
    expect(fit.ok, `320px drawer workspace chip/options check failed: ${JSON.stringify(fit)}`).toBe(true);

    const optBox = await optionsBtn.boundingBox();
    expect(optBox).not.toBeNull();
    await page.touchscreen.tap(optBox.x + optBox.width / 2, optBox.y + optBox.height / 2);
    await expect(page.getByRole('menuitem').first()).toBeVisible({ timeout: 5000 });
  });
});
