/**
 * Mobile Web P1 (390x844 portrait) layout & interaction E2E suite.
 *
 * Covers both capabilities-all-on (Everflow profile) and capabilities-all-off
 * (Core profile) across:
 *   - Chat topbar (compact tracking & sharing, capability gating, non-overlapping tap targets, <= 1 Share2 icon)
 *   - Mobile nav drawer (coverage tree & team shared workspaces gated by capabilities)
 *   - HTML report breadcrumb row at 390px (no button clipping) + PublishDialog 2x2 visibility grid
 *   - BottomTabBar "More" Radix DropdownMenu (ArrowDown roving focus & Escape)
 *   - Coarse-pointer Enter inserts newline instead of sending; Send button submits
 *   - SharedChatView (/s/:token with long workspace name + file button) & OwnerArtifactView (/a/:token with More menu & version status)
 *   - Settings sub-panels waiting for concrete cards under both capability modes
 *
 * Run outside sandbox via Playwright (`cd web && pnpm test:e2e e2e/mobile-p1-layout.spec.js`).
 */
import { test, expect, mockAPI, configureSSE, resetMockServer } from './fixtures.js';
import { sampleWorkspace, sampleThread, sseEvents } from './helpers/mockResponses.js';

const VIEWPORT = { width: 390, height: 844 };

const WS_ID = 'a0000001-0000-4000-8000-000000000001';
const THREAD_ID = 'b0000001-0000-4000-8000-000000000001';
const ARTIFACT_ID = '6f1c2a7e-3b4d-4e5f-8a9b-0c1d2e3f4a5b';
const SHARE_TOKEN = 'mob-p1-tok';
const ARTIFACT_CODE = 'mob-p1-art';

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

const SAMPLE_ARTIFACT = {
  artifact_id: ARTIFACT_ID,
  code: ARTIFACT_CODE,
  url: `/a/${ARTIFACT_CODE}`,
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

const SAMPLE_ARTIFACT_STATUS = {
  latest_seq: 3,
  live_sha256: 'a'.repeat(64),
  page_sha256: 'a'.repeat(64),
  files: [
    { path: 'reports/q3_margin_deep_dive.html', size: 2048, reason: 'entry', optional: false },
  ],
  left_out: [
    { path: 'reports/appendix_notes.md', size: 512, reason: 'markdown', optional: true },
  ],
  total_size: 2048,
  changed: false,
  drift: null,
};

test.use({
  viewport: VIEWPORT,
  isMobile: true,
  hasTouch: true,
});

async function assertNoHorizontalOverflow(page, label) {
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(scrollWidth, `${label}: horizontal overflow (${scrollWidth}px > ${VIEWPORT.width}px)`).toBeLessThanOrEqual(
    VIEWPORT.width,
  );
}

function capabilityMocks(enabled) {
  return {
    'GET /dalton/capabilities': {
      profile: enabled ? 'everflow' : 'core',
      capabilities: enabled ? ALL_CAPABILITIES : [],
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
      colleague_sharing_enabled: enabled,
    },
    'GET /dalton/invites': {
      invites: [
        {
          invite_id: 'inv-1234567890abcdef',
          status: 'pending',
          tier: 'internal',
          created_at: '2026-11-20T08:00:00Z',
          expires_at: '2026-11-27T08:00:00Z',
        },
      ],
      active_count: 1,
      max_active: 10,
    },
    'GET /dalton/feishu/read-scopes': {
      scopes: [{ space_id: '7398217398127398123', name: '核心投研知识库空间' }],
    },
    'GET /dalton/feishu/spaces': {
      spaces: [{ space_id: '7398217398127398123', name: '核心投研知识库空间' }],
    },
    'GET /dalton/notify/feishu-groups': {
      ok: true,
      groups: [{ chat_id: 'oc_test_group_001', name: 'TMT 研究周会群' }],
    },
    'GET /dalton/notify/feishu-bot': {
      ok: true,
      app_name: 'Dalton Notify Bot',
    },
    'GET /dalton/channels/bindings': {
      user_tier: 'admin',
      bindings: {
        feishu: { linked: true, platform_user_id: 'ou_1234567890abcdef', platform_username: 'Analyst' },
        discord: { linked: true, platform_user_id: '123456789012345678', platform_username: 'analyst#0001' },
      },
      settings: {
        version: 'v1',
        apps: {
          discord: {
            chats: {
              'discord:123456789012345678': { name: 'macro-alerts' },
            },
          },
        },
      },
      chat_bindings: [
        { platform: 'discord', chat_address: 'discord:123456789012345678', chat_name: 'macro-alerts' },
      ],
    },
    'GET /dalton/budget': {
      daily_cap_usd: 25,
      default_task_cap_usd: 5,
      timezone: 'Asia/Shanghai',
      task_caps: [
        { scope_kind: 'workspace', scope_id: WS_ID, scope_name: 'Semiconductor Deep Dive', cap_usd: 8 },
      ],
    },
    'GET /dalton/budget/today': {
      spent_usd: 4.2,
      cap_usd: 25,
      subscription_notional_usd: 1.5,
      budget_gate_enabled: true,
      runs: [],
    },
    'GET /dalton/coverage/units': enabled
      ? {
          enabled: true,
          units: [
            {
              unit_id: 'unit-sec-1',
              kind: 'sector',
              slug: 'semiconductors',
              display_name: 'Semiconductors',
              readable: true,
              delegatable: true,
              auto_tracked: true,
              workspace_id: WS_ID,
              members: [
                {
                  member_id: 'm-nvda',
                  company_unit_id: 'unit-nvda',
                  name: 'NVIDIA Corp',
                  ticker: 'NVDA',
                  workspace_id: WS_ID,
                  workspace_created: true,
                  scope: 'in',
                  reason: '',
                  valid_until: null,
                },
              ],
            },
          ],
          unclassified: [],
        }
      : { enabled: false },
    'GET /dalton/system-jobs': { jobs: [] },
    'GET /dalton/sharing/user-settings': {
      cos_rw_enabled: true,
      can_edit: true,
      colleague_dispatch_enabled: true,
      caller_tier: 'admin',
    },
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
    'GET /dalton/workspaces/team': {
      caller_tier: 'admin',
      members: [
        {
          user_id: 'u-colleague',
          display_name: 'Colleague A',
          email: 'colleague@example.com',
          workspaces: [
            {
              workspace_id: 'ws-colleague-1',
              user_id: 'u-colleague',
              owner_name: 'Colleague A',
              owner_email: 'colleague@example.com',
              name: 'Battery Supply Chain',
              access: 'ro',
              web_access: 'ro',
              updated_at: '2026-11-20T00:00:00Z',
            },
          ],
        },
      ],
    },
    'GET /workspaces/*/files': {
      files: ['reports/q3_margin_deep_dive.html', 'reports/notes.md'],
    },
    'GET /workspaces/*/files/read': (route) => {
      const reqUrl = new URL(route.request().url());
      const filePath = reqUrl.searchParams.get('path') || '';
      const isMd = filePath.endsWith('.md');
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          isMd
            ? {
                content: '# Q3 Research Notes\n\nGross margin expanded by 240bps in Q3.\n',
                mime: 'text/markdown',
                truncated: false,
              }
            : {
                content: '<html><body><h1>Q3 Gross Margin Report</h1></body></html>',
                mime: 'text/html',
                truncated: false,
              },
        ),
      });
    },
    'POST /workspaces/*/file-grant': {
      prefix: '/api/v1/wsfiles/g/grant-e2e/',
      expires_in: 3600,
    },
    'GET /workspaces/*/artifacts': {
      artifact: SAMPLE_ARTIFACT,
    },
    'POST /workspaces/*/artifacts': SAMPLE_ARTIFACT,
    'POST /artifacts/*/status': SAMPLE_ARTIFACT_STATUS,
    'GET /artifacts/*/versions': {
      versions: [
        { seq: 3, published_at: '2026-11-20T08:00:00Z', label: 'Q3 update', origin: 'owner', entry_path: SAMPLE_ARTIFACT.path, title: SAMPLE_ARTIFACT.name, byte_len: 2048 },
      ],
    },
    'GET /artifacts/*/comments': { comments: [] },
    'GET /dalton/monitors/by-artifact/*': { monitor: null },
  };
}

test.beforeEach(async () => {
  await resetMockServer();
});

for (const [modeLabel, capabilitiesOn] of [
  ['capabilities-all-on', true],
  ['capabilities-all-off', false],
]) {
  test.describe(`Mobile 390x844 layout scan (${modeLabel})`, () => {
    test('chat topbar verifies compact tracking/sharing controls, capability gating, tap spacing, and drawer sections', async ({ page }) => {
      const ws = sampleWorkspace({ workspace_id: WS_ID, name: 'NVIDIA Earnings & Supply Chain' });
      const thread = sampleThread({ thread_id: THREAD_ID, workspace_id: WS_ID });
      await mockAPI(page, {
        ...capabilityMocks(capabilitiesOn),
        'GET /workspaces': { workspaces: [ws], total: 1, limit: 20, offset: 0 },
        'GET /workspaces/*': ws,
        'GET /threads': { threads: [thread], total: 1, limit: 20, offset: 0 },
        'GET /threads/*': thread,
        'GET /threads/*/messages': { messages: [], has_more: false },
      });

      const capsLoaded = page.waitForResponse((res) => res.url().includes('/api/v1/dalton/capabilities') && res.status() === 200);
      await page.goto(`/chat/t/${THREAD_ID}`);
      await capsLoaded;
      const topbar = page.locator('[data-handoff="header"]').first();
      const heading = topbar.locator('h1').first();
      await expect(heading).toBeVisible({ timeout: 10000 });
      const headingBox = await heading.boundingBox();
      expect(headingBox?.width ?? 0).toBeGreaterThan(40);

      // Compact tracking control is present and marked compact
      const tracking = topbar.getByTestId('workspace-tracking-toggle');
      await expect(tracking).toBeVisible({ timeout: 10000 });
      await expect(tracking).toHaveAttribute('data-compact', 'true');

      // Thread public ShareButton must still be present (exact count === 1, never 0 or duplicate 2)
      const share2Count = await topbar.locator('svg.lucide-share-2').count();
      expect(share2Count).toBe(1);

      if (capabilitiesOn) {
        const sharing = topbar.getByTestId('workspace-sharing-control');
        await expect(sharing).toBeVisible({ timeout: 10000 });
        await expect(sharing).toHaveAttribute('data-compact', 'true');

        // Tracking & Sharing controls have clear horizontal separation so their 44px tap areas do not overlap
        const trackingBox = await tracking.boundingBox();
        const sharingBox = await sharing.boundingBox();
        expect((sharingBox?.x ?? 0) - ((trackingBox?.x ?? 0) + (trackingBox?.width ?? 0))).toBeGreaterThanOrEqual(8);
      } else {
        await expect(topbar.getByTestId('workspace-sharing-control')).toHaveCount(0);
      }

      await assertNoHorizontalOverflow(page, `chat topbar (${modeLabel})`);

      // Open mobile nav drawer and wait for coverage response before asserting presence/absence
      const drawerToggle = topbar.locator('button svg.lucide-menu').locator('..');
      const coverageLoaded = page.waitForResponse((res) => res.url().includes('/api/v1/dalton/coverage/units') && res.status() === 200);
      await drawerToggle.click();
      await coverageLoaded;
      if (capabilitiesOn) {
        await expect(page.getByTestId('coverage-tree-section')).toBeVisible({ timeout: 10000 });
        await expect(page.getByTestId('team-workspaces-section')).toHaveCount(0);
      } else {
        await expect(page.getByTestId('coverage-tree-section')).toHaveCount(0);
        await expect(page.getByTestId('team-workspaces-section')).toHaveCount(0);
      }
    });

    test('settings panels wait for concrete cards and have zero horizontal overflow', async ({ page }) => {
      const ws = sampleWorkspace({ workspace_id: WS_ID, name: 'Semiconductor Deep Dive' });
      await mockAPI(page, {
        ...capabilityMocks(capabilitiesOn),
        'GET /workspaces': { workspaces: [ws], total: 1, limit: 20, offset: 0 },
      });

      // OSS E2E server exposes general, budget, integrations, coverage, and teamChannels
      // (DaltonTeamMembersPanel / FeishuReadScopesCard / DaltonInvitesCard require isDaltonAuth=true and are covered in Vitest).
      const tabs = ['general', 'budget', 'integrations', 'coverage', 'teamChannels'];
      for (const tab of tabs) {
        const capsWait = page.waitForResponse((res) => res.url().includes('/api/v1/dalton/capabilities') && res.status() === 200);
        const coverageWait = tab === 'coverage'
          ? page.waitForResponse((res) => res.url().includes('/api/v1/dalton/coverage/units') && res.status() === 200)
          : null;
        await page.goto(`/settings?tab=${tab}`);
        await capsWait;
        if (coverageWait) await coverageWait;
        await page.waitForSelector('.settings-column', { timeout: 10000 });

        if (tab === 'general') {
          const helpLinks = page.getByTestId('settings-mobile-help-links');
          await expect(helpLinks).toBeVisible();
          expect((await helpLinks.boundingBox())?.width ?? 0).toBeGreaterThan(0);
        } else if (tab === 'budget') {
          await expect(page.getByTestId('dalton-budget-save')).toBeVisible({ timeout: 10000 });
        } else if (tab === 'integrations') {
          if (capabilitiesOn) {
            await expect(page.getByTestId('dalton-notify-test-feishu')).toBeVisible({ timeout: 10000 });
            await expect(page.getByTestId('dalton-channel-generate-feishu')).toBeVisible({ timeout: 10000 });
          } else {
            await expect(page.getByTestId('dalton-notify-test-feishu')).toHaveCount(0);
            await expect(page.getByTestId('dalton-channel-generate-feishu')).toHaveCount(0);
          }
        } else if (tab === 'coverage') {
          if (capabilitiesOn) {
            await expect(page.getByTestId('dalton-coverage-register-btn')).toBeVisible({ timeout: 10000 });
          } else {
            await expect(page.getByTestId('dalton-coverage-register-btn')).toHaveCount(0);
          }
        } else if (tab === 'teamChannels') {
          if (capabilitiesOn) {
            await expect(page.getByTestId('dalton-discord-channel-id-input')).toBeVisible({ timeout: 10000 });
            await expect(page.getByTestId('dalton-notify-manual-input')).toBeVisible({ timeout: 10000 });
          } else {
            await expect(page.getByTestId('dalton-discord-channel-id-input')).toHaveCount(0);
            await expect(page.getByTestId('dalton-notify-manual-input')).toHaveCount(0);
          }
        }

        await assertNoHorizontalOverflow(page, `settings?tab=${tab} (${modeLabel})`);
      }
    });

    test('SharedChatView /s/:token truncates long workspace title without pushing file toggle off-screen', async ({ page }) => {
      await mockAPI(page, capabilityMocks(capabilitiesOn));
      await configureSSE({
        method: 'GET',
        path: `/api/v1/public/shared/${SHARE_TOKEN}`,
        status: 200,
        json: {
          kind: 'thread',
          thread_id: 'th-shared-mob',
          title: 'Long Shared Research Thread Title For Mobile Verification',
          workspace_name: 'Global Semiconductor Foundry Capacity & EUV Lithography Supply Chain Quarterly Tracker 2026',
          permissions: { allow_files: true, allow_download: false },
        },
      });
      await configureSSE({
        method: 'GET',
        path: `/api/v1/public/shared/${SHARE_TOKEN}/replay`,
        delay: 10,
        events: [
          {
            event: 'user_message',
            data: {
              thread_id: 'th-shared-mob',
              turn_index: 0,
              content: 'Compare https://example.com/very/long/unbreakable/url/segment/for/mobile/overflow/testing',
              timestamp: '2026-11-20T00:00:00Z',
              metadata: { msg_type: 'ptc', workspace_id: 'ws-shared' },
            },
          },
          {
            event: 'message_chunk',
            data: {
              thread_id: 'th-shared-mob',
              agent: 'model:test',
              id: 'lc_run--mob',
              role: 'assistant',
              content: 'Summary of Q3 gross margin and capex trajectory.',
              content_type: 'text',
              turn_index: 0,
            },
          },
          {
            event: 'message_chunk',
            data: {
              thread_id: 'th-shared-mob',
              agent: 'model:test',
              id: 'lc_run--mob',
              role: 'assistant',
              content: '',
              content_type: 'text',
              finish_reason: 'stop',
              turn_index: 0,
            },
          },
          { event: 'replay_done', data: { thread_id: 'th-shared-mob' } },
        ],
      });

      await page.goto(`/s/${SHARE_TOKEN}`);
      const heading = page.locator('h1').first();
      await expect(heading).toBeVisible({ timeout: 10000 });
      const fileToggle = page.locator('[data-panel-toggle="header"]').first();
      await expect(fileToggle).toBeVisible({ timeout: 10000 });

      const h1Box = await heading.boundingBox();
      const btnBox = await fileToggle.boundingBox();
      expect(h1Box?.width ?? 0).toBeGreaterThan(40);
      expect((btnBox?.x ?? 0) + (btnBox?.width ?? 0)).toBeLessThanOrEqual(VIEWPORT.width);
      expect((h1Box?.x ?? 0) + (h1Box?.width ?? 0)).toBeLessThanOrEqual((btnBox?.x ?? VIEWPORT.width) + 2);

      await expect(page.getByText('Summary of Q3 gross margin')).toBeVisible({ timeout: 10000 });
      await assertNoHorizontalOverflow(page, `/s/:token (${modeLabel})`);
    });

    test('OwnerArtifactView /a/:token shows version status marker, More menu, and zero overflow', async ({ page }) => {
      await mockAPI(page, capabilityMocks(capabilitiesOn));
      await configureSSE({
        method: 'GET',
        path: `/api/v1/public/shared/${ARTIFACT_CODE}`,
        status: 200,
        json: {
          kind: 'file',
          access: 'owner',
          artifact_id: ARTIFACT_ID,
          workspace_id: WS_ID,
          path: SAMPLE_ARTIFACT.path,
          name: SAMPLE_ARTIFACT.name,
          shared: true,
          latest_seq: 3,
          frame_base: '/api/v1/public/artifacts/frame-demo/',
          expires_in: 3600,
        },
      });

      await page.goto(`/a/${ARTIFACT_CODE}`);
      const title = page.locator('.share-topbar-name');
      await expect(title).toBeVisible({ timeout: 10000 });
      expect((await title.boundingBox())?.width ?? 0).toBeGreaterThan(40);

      // Version picker retains status marker on mobile
      const versionBtn = page.locator('[data-artifact-version-picker]').first();
      await expect(versionBtn).toBeVisible({ timeout: 10000 });
      await expect(versionBtn).toHaveAttribute('data-version-status', 'published');

      // Secondary actions (Reload, Save as PDF) are folded into the mobile More menu
      const moreBtn = page.getByTestId('owner-artifact-mobile-more');
      await expect(moreBtn).toBeVisible();
      await moreBtn.click();
      await expect(page.getByRole('menuitem').first()).toBeVisible();
      await page.keyboard.press('Escape');

      await assertNoHorizontalOverflow(page, `/a/:token (${modeLabel})`);
    });
  });
}

test.describe('Mobile 390x844 interactive flows (HTML crumbs + Publish 2x2, BottomTabBar More, coarse Enter)', () => {
  test('HTML file breadcrumb row fits 390px without clipping and opens PublishDialog 2x2 visibility grid', async ({ page }) => {
    const ws = sampleWorkspace({ workspace_id: WS_ID, name: 'NVIDIA Earnings & Supply Chain' });
    const thread = sampleThread({ thread_id: THREAD_ID, workspace_id: WS_ID });
    await mockAPI(page, {
      ...capabilityMocks(true),
      'GET /workspaces': { workspaces: [ws], total: 1, limit: 20, offset: 0 },
      'GET /workspaces/*': ws,
      'GET /threads': { threads: [thread], total: 1, limit: 20, offset: 0 },
      'GET /threads/*': thread,
      'GET /threads/*/messages': { messages: [], has_more: false },
    });

    await page.goto(`/chat/t/${THREAD_ID}`);
    await page.locator('[data-panel-toggle="header"]').first().click();

    // Expand the collapsed reports/ folder in the file tree before opening the HTML report
    const reportsDir = page.locator('[role="treeitem"][data-row-kind="dir"][data-row-path="reports"]').first();
    await expect(reportsDir).toBeVisible({ timeout: 10000 });
    await reportsDir.click();

    // Open the HTML report from the expanded file tree
    const fileItem = page.locator('[role="treeitem"][data-row-kind="file"][data-row-path="reports/q3_margin_deep_dive.html"]').first();
    await expect(fileItem).toBeVisible({ timeout: 10000 });
    await fileItem.click();

    const crumbs = page.locator('.file-panel-crumbs').first();
    await expect(crumbs).toBeVisible({ timeout: 10000 });
    const moreBtn = crumbs.getByTestId('file-header-mobile-more');
    await expect(moreBtn).toBeVisible({ timeout: 10000 });
    // Breadcrumb row on mobile HTML does not render the standalone version picker chip (folded into More)
    await expect(crumbs.locator('.artifact-version-chip')).toHaveCount(0);
    await expect(crumbs.locator('[data-artifact-version-picker]:not([data-testid="file-header-mobile-more"])')).toHaveCount(0);

    // Measure every visible control inside .file-panel-crumbs: strictly zero horizontal overflow at 390px
    const crumbMetrics = await crumbs.evaluate((row) => {
      const rowRect = row.getBoundingClientRect();
      const buttons = Array.from(row.querySelectorAll('button')).map((btn) => {
        const r = btn.getBoundingClientRect();
        return { label: btn.getAttribute('aria-label') || btn.textContent?.trim() || '', right: r.right, width: r.width };
      });
      return {
        scrollWidth: row.scrollWidth,
        clientWidth: row.clientWidth,
        rowRight: rowRect.right,
        buttons: buttons.filter((b) => b.width > 0),
      };
    });
    expect(crumbMetrics.scrollWidth).toBeLessThanOrEqual(crumbMetrics.clientWidth);
    for (const btn of crumbMetrics.buttons) {
      expect(btn.right, `Breadcrumb button "${btn.label}" clipped past row right`).toBeLessThanOrEqual(crumbMetrics.rowRight + 1);
      expect(btn.right, `Breadcrumb button "${btn.label}" clipped past 390px`).toBeLessThanOrEqual(VIEWPORT.width);
    }

    // Open More menu -> verify folded owner controls (versions, dashboard pin) -> expand inline versions and assert bounds inside 0..390px -> open Publish -> assert real 2x2 grid geometry
    await moreBtn.click();
    const versionsTrigger = page.getByTestId('file-header-mobile-versions');
    await expect(versionsTrigger).toBeVisible();
    await expect(page.getByTestId('file-header-mobile-pin-dashboard')).toBeVisible();
    await versionsTrigger.click();
    const versionsList = page.getByTestId('file-header-mobile-versions-list');
    await expect(versionsList).toBeVisible();
    const versionsBox = await versionsList.boundingBox();
    expect(versionsBox).not.toBeNull();
    expect(versionsBox.x).toBeGreaterThanOrEqual(0);
    expect(versionsBox.x + versionsBox.width).toBeLessThanOrEqual(VIEWPORT.width);

    // Inline PDF options also expand inside the menu without overflowing 0..390px
    const pdfOptionsTrigger = page.getByTestId('file-header-mobile-pdf-options');
    await expect(pdfOptionsTrigger).toBeVisible();
    await pdfOptionsTrigger.click();
    const pdfOptionsList = page.getByTestId('file-header-mobile-pdf-options-list');
    await expect(pdfOptionsList).toBeVisible();
    const pdfBox = await pdfOptionsList.boundingBox();
    expect(pdfBox).not.toBeNull();
    expect(pdfBox.x).toBeGreaterThanOrEqual(0);
    expect(pdfBox.x + pdfBox.width).toBeLessThanOrEqual(VIEWPORT.width);

    await page.getByTestId('file-header-mobile-publish').click();

    const grid = page.getByTestId('publish-visibility-grid');
    await expect(grid).toBeVisible({ timeout: 10000 });
    const visButtons = grid.locator('button');
    expect(await visButtons.count()).toBe(4);
    const boxes = await Promise.all([0, 1, 2, 3].map((i) => visButtons.nth(i).boundingBox()));
    for (const b of boxes) expect(b).not.toBeNull();
    // Row 1: two side-by-side columns
    expect(Math.abs(boxes[0].y - boxes[1].y)).toBeLessThanOrEqual(4);
    expect(boxes[1].x).toBeGreaterThan(boxes[0].x + 10);
    // Row 2: below row 1, two side-by-side columns
    expect(boxes[2].y).toBeGreaterThan(boxes[0].y + 16);
    expect(Math.abs(boxes[2].y - boxes[3].y)).toBeLessThanOrEqual(4);
    expect(boxes[3].x).toBeGreaterThan(boxes[2].x + 10);

    await assertNoHorizontalOverflow(page, 'PublishDialog 2x2');
  });

  test('BottomTabBar More menu supports ArrowDown roving focus and Escape close', async ({ page }) => {
    const ws = sampleWorkspace({ workspace_id: WS_ID, name: 'Semiconductor Deep Dive' });
    await mockAPI(page, {
      ...capabilityMocks(true),
      'GET /workspaces': { workspaces: [ws], total: 1, limit: 20, offset: 0 },
    });

    await page.goto('/settings?tab=general');
    const moreBtn = page.getByTestId('bottom-tab-more');
    await expect(moreBtn).toBeVisible({ timeout: 10000 });
    await expect(moreBtn).toHaveAttribute('aria-haspopup', 'menu');
    // /settings sits inside the More overflow menu when Dashboard is present
    await expect(moreBtn).toHaveAttribute('aria-current', 'page');

    await moreBtn.click();
    const menu = page.getByTestId('bottom-tab-more-menu');
    await expect(menu).toBeVisible();
    const items = menu.getByRole('menuitem');
    await expect(items.nth(1)).toBeVisible();
    // Wait for Radix DropdownMenuContent onOpenAutoFocus to move focus into the menu
    await expect(menu).toBeFocused();

    // Drive roving focus with web-first auto-retrying assertions (immune to rAF focus commit timing)
    await page.keyboard.press('ArrowDown');
    await expect(items.nth(0)).toBeFocused();
    await page.keyboard.press('ArrowDown');
    await expect(items.nth(1)).toBeFocused();

    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
  });

  test('coarse-pointer Enter inserts newline in ChatInput (preserved in POST body), SelectionQuote, and CommentComposer', async ({ page }) => {
    const ws = sampleWorkspace({ workspace_id: WS_ID, name: 'NVIDIA Earnings & Supply Chain' });
    const thread = sampleThread({ thread_id: THREAD_ID, workspace_id: WS_ID });
    const submittedBodies = [];

    await mockAPI(page, {
      ...capabilityMocks(true),
      'GET /workspaces': { workspaces: [ws], total: 1, limit: 20, offset: 0 },
      'GET /workspaces/*': ws,
      'GET /threads': { threads: [thread], total: 1, limit: 20, offset: 0 },
      'GET /threads/*': thread,
      'GET /threads/*/messages': { messages: [], has_more: false },
      'POST /threads/*/messages': (route) => {
        submittedBodies.push(route.request().postDataJSON());
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
      },
    });
    await configureSSE({
      method: 'GET',
      path: `/api/v1/threads/${THREAD_ID}/messages/replay`,
      events: [
        sseEvents.userMessage('Summarize Q3 gross margin.', 0),
        sseEvents.messageChunk('Gross margin expanded by 240bps in Q3 driven by data center mix.'),
        sseEvents.finishStop(),
        sseEvents.replayDone(),
      ],
      delay: 10,
    });
    await configureSSE({
      method: 'POST',
      path: `/api/v1/threads/${THREAD_ID}/messages`,
      status: 200,
      events: [{ event: 'done', data: {} }],
    });

    await page.goto(`/chat/t/${THREAD_ID}`);
    const textarea = page.locator('textarea').first();
    await expect(textarea).toBeVisible({ timeout: 10000 });

    // 1. SelectionQuote: run BEFORE ChatInput send (sending triggers turnPin smooth-scroll,
    // which pins the new user bubble at the top and glides earlier messages offscreen, hiding
    // SelectionToolbar via useAnchoredPosition's offscreen visibility check).
    // Trigger selection with a real pointer drag across the first line of the assistant reply.
    const assistantMsg = page.getByText('Gross margin expanded by 240bps in Q3 driven by data center mix.').first();
    await expect(assistantMsg).toBeVisible({ timeout: 10000 });
    const line = await assistantMsg.evaluate((el) => new Promise((resolve) => {
      const text = document.createTreeWalker(el, NodeFilter.SHOW_TEXT).nextNode();
      if (!text) throw new Error('Expected text node in assistant message');
      const first = document.createRange();
      first.setStart(text, 0);
      first.setEnd(text, 1);
      let last = '';
      let still = 0;
      const frame = () => {
        const ch = first.getBoundingClientRect();
        const box = el.getBoundingClientRect();
        const at = `${ch.left},${ch.top},${box.width}`;
        still = at === last ? still + 1 : 0;
        last = at;
        if (still < 2) return requestAnimationFrame(frame);
        resolve({
          x: ch.left + 1,
          y: ch.top + ch.height / 2,
          endX: Math.min(box.right - 8, ch.left + 160),
        });
      };
      requestAnimationFrame(frame);
    }));
    await page.mouse.move(line.x, line.y);
    await page.mouse.down();
    await page.mouse.move((line.x + line.endX) / 2, line.y, { steps: 4 });
    await page.mouse.move(line.endX, line.y, { steps: 4 });
    await page.mouse.up();

    const quoteBtn = page.locator('button.selection-quote-icon').first();
    await expect(quoteBtn).toBeVisible({ timeout: 5000 });
    await quoteBtn.click();
    const quoteTextarea = page.locator('.selection-quote-popover textarea').first();
    await expect(quoteTextarea).toBeVisible({ timeout: 5000 });
    await quoteTextarea.click();
    await quoteTextarea.type('Quote 1');
    await page.keyboard.press('Enter');
    await quoteTextarea.type('Quote 2');
    await expect(quoteTextarea).toHaveValue('Quote 1\nQuote 2');
    await page.keyboard.press('Escape');
    await expect(quoteTextarea).toBeHidden();

    // 2. ChatInput: Enter inserts newline (no submit), Send button posts body containing the newline
    await textarea.click();
    await textarea.type('Line 1');
    await page.keyboard.press('Enter');
    await textarea.type('Line 2');

    await expect(textarea).toHaveValue('Line 1\nLine 2');
    expect(submittedBodies).toHaveLength(0);

    const sendBtn = page.locator('button[type="submit"], button[aria-label*="Send" i], button[aria-label*="发送" i]').first();
    await expect(sendBtn).toBeEnabled();
    await sendBtn.click();
    await expect.poll(() => submittedBodies.length, { timeout: 5000 }).toBeGreaterThan(0);
    expect(JSON.stringify(submittedBodies[0])).toContain('Line 1\\nLine 2');

    // 3. CommentComposer: open Markdown file -> enable Comment mode -> click paragraph -> Enter inserts newline
    await page.locator('[data-panel-toggle="header"]').first().click();
    const reportsDir = page.locator('[role="treeitem"][data-row-kind="dir"][data-row-path="reports"]').first();
    await expect(reportsDir).toBeVisible({ timeout: 10000 });
    await reportsDir.click();
    const mdItem = page.locator('[role="treeitem"][data-row-kind="file"][data-row-path="reports/notes.md"]').first();
    await expect(mdItem).toBeVisible({ timeout: 10000 });
    await mdItem.click();

    const commentModeBtn = page.locator('.page-comment-mode-btn').first();
    await expect(commentModeBtn).toBeVisible({ timeout: 10000 });
    await commentModeBtn.click();
    // Wait until LazyCommentLayer + ViewerPage attach and enter comment mode before clicking a paragraph
    await expect(page.locator('.file-panel-content [data-comment-cursor="crosshair"]')).toBeVisible({ timeout: 10000 });
    const mdParagraph = page.locator('.file-panel-content').getByText('Gross margin expanded by 240bps in Q3.').first();
    await expect(mdParagraph).toBeVisible({ timeout: 10000 });
    await mdParagraph.click();

    const commentTextarea = page.locator('textarea[data-page-draft]').first();
    await expect(commentTextarea).toBeVisible({ timeout: 5000 });
    await commentTextarea.click();
    await commentTextarea.type('Comment 1');
    await page.keyboard.press('Enter');
    await commentTextarea.type('Comment 2');
    await expect(commentTextarea).toHaveValue('Comment 1\nComment 2');
  });
});
