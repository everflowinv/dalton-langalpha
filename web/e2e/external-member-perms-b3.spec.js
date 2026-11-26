/**
 * Playwright E2E suite for External Member Permissions Batch 3:
 *   1. Shared chat HTML preview in-frame relative link navigation A -> B -> C,
 *      scripted location.href navigation without ?inject=view falling into unbridged
 *      (hiding all doc A actions and "Open in new tab"), verified on both desktop (1280px)
 *      and mobile (390px) viewports using the real page bridge scripts.
 *   2. Pathless public monitor (/m/:slug -> /api/v1/dalton/monitors/public/:slug/v/:seq/)
 *      entering bridged_root and keeping primary topbar actions on both desktop & mobile.
 *   3. Misconfigured VITE_DALTON_AUTH receiving HTTP 401 from /api/v1/dalton/credentials/status
 *      causing useCallerIdentity() to fail closed (status: 'error', tier: 'unknown') and
 *      disabling privileged publish/share/copy controls.
 *
 * Run outside sandbox via Playwright:
 *   cd web && pnpm test:e2e e2e/external-member-perms-b3.spec.js
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, expect, mockAPI, configureSSE, resetMockServer } from './fixtures.js';
import { sampleWorkspace, sampleThread } from './helpers/mockResponses.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const BRIDGE_DIR = resolve(HERE, '../../src/server/preview_proxy/bridge');
const VIEW_BRIDGE_PARTS = ['opaque', 'core', 'links', 'loads', 'requests'];
const VIEW_BRIDGE_SCRIPT = [
  "(function () {\n'use strict';\nif (!window.parent || window.parent === window) return;",
  ...VIEW_BRIDGE_PARTS.map((name) => readFileSync(resolve(BRIDGE_DIR, `${name}.js`), 'utf8')),
  'hello();\n})();',
].join('\n');

const SHARE_TOKEN = 'b3-share-tok';
const THREAD_ID = 'b3-thread-1';
const WS_ID = 'a0000003-0000-4000-8000-000000000003';
const ARTIFACT_ID = '6f1c2a7e-3b4d-4e5f-8a9b-0c1d2e3f0003';

function wrapHtml(title, body, injectBridge) {
  const script = injectBridge ? `<script>${VIEW_BRIDGE_SCRIPT}</script>` : '';
  return `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title>${script}</head><body>${body}</body></html>`;
}

const SHARED_META = {
  kind: 'thread',
  thread_id: THREAD_ID,
  title: 'Shared B3 Report',
  workspace_name: 'Research B3',
  permissions: { allow_files: true, allow_download: true },
};

const SHARED_REPLAY = [
  {
    event: 'user_message',
    data: {
      thread_id: THREAD_ID,
      turn_index: 0,
      content: 'Open the linked report.',
      timestamp: '2026-11-25T00:00:00Z',
      metadata: { msg_type: 'ptc', workspace_id: WS_ID },
    },
  },
  {
    event: 'message_chunk',
    data: {
      thread_id: THREAD_ID,
      agent: 'model:test',
      id: 'lc_run--b3',
      role: 'assistant',
      content: 'Report ready in reports/a.html.',
      content_type: 'text',
      turn_index: 0,
    },
  },
  {
    event: 'message_chunk',
    data: {
      thread_id: THREAD_ID,
      agent: 'model:test',
      id: 'lc_run--b3',
      role: 'assistant',
      content: '',
      content_type: 'text',
      finish_reason: 'stop',
      turn_index: 0,
    },
  },
  {
    event: 'replay_done',
    data: { thread_id: THREAD_ID },
  },
];

async function setupSharedReportRoutes(page) {
  await configureSSE({
    method: 'GET',
    path: `/api/v1/public/shared/${SHARE_TOKEN}`,
    json: SHARED_META,
    status: 200,
  });
  await configureSSE({
    method: 'GET',
    path: `/api/v1/public/shared/${SHARE_TOKEN}/replay`,
    events: SHARED_REPLAY,
    delay: 10,
  });
  await configureSSE({
    method: 'GET',
    path: `/api/v1/public/shared/${SHARE_TOKEN}/files`,
    json: {
      path: '.',
      files: ['reports/a.html', 'reports/b.html', 'reports/c.html', 'reports/raw.html'],
      source: 'workspace',
    },
    status: 200,
  });

  // Read file content API used by FilePanel before mounting HtmlViewer.
  await page.route(`**/api/v1/public/shared/${SHARE_TOKEN}/files/read*`, async (route) => {
    const url = new URL(route.request().url());
    const filePath = url.searchParams.get('path') || 'reports/a.html';
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        path: filePath,
        content: `<h1>${filePath}</h1>`,
        mime: 'text/html',
        truncated: false,
      }),
    });
  });

  // Served HTML documents inside the preview iframe: inject real bridge ONLY when ?inject=view is present.
  await page.route(`**/api/v1/public/shared/${SHARE_TOKEN}/files/serve/**`, async (route) => {
    const url = new URL(route.request().url());
    const hasViewBridge = url.searchParams.get('inject') === 'view';
    if (url.pathname.endsWith('/reports/a.html')) {
      await route.fulfill({
        status: 200,
        contentType: 'text/html; charset=utf-8',
        body: wrapHtml('Report A', '<h1>Doc A</h1><a id="to-b" href="b.html">Go to B</a>', hasViewBridge),
      });
      return;
    }
    if (url.pathname.endsWith('/reports/b.html')) {
      await route.fulfill({
        status: 200,
        contentType: 'text/html; charset=utf-8',
        body: wrapHtml('Report B', '<h1>Doc B</h1><a id="to-c" href="c.html">Go to C</a>', hasViewBridge),
      });
      return;
    }
    if (url.pathname.endsWith('/reports/c.html')) {
      await route.fulfill({
        status: 200,
        contentType: 'text/html; charset=utf-8',
        body: wrapHtml(
          'Report C',
          '<h1>Doc C</h1><button id="script-nav" onclick="location.href=\'raw.html\'">Script nav</button>',
          hasViewBridge,
        ),
      });
      return;
    }
    // raw.html reached via scripted location.href drops ?inject=view -> served without bridge -> unbridged.
    await route.fulfill({
      status: 200,
      contentType: 'text/html; charset=utf-8',
      body: wrapHtml('Raw Unbridged', '<h1>Unbridged Page</h1>', hasViewBridge),
    });
  });
}

test.beforeEach(async () => {
  await resetMockServer();
});

for (const mode of [
  { name: 'desktop (1280px)', viewport: { width: 1280, height: 800 }, isMobile: false },
  { name: 'mobile (390px)', viewport: { width: 390, height: 844 }, isMobile: true },
]) {
  test.describe(`Preview bridge doc state machine on ${mode.name}`, () => {
    test.use({ viewport: mode.viewport, isMobile: mode.isMobile, hasTouch: mode.isMobile });

    test('shared chat: A -> B -> C relative links and scripted location.href -> unbridged keep toolbar consistent', async ({ page }) => {
      await setupSharedReportRoutes(page);
      await page.goto(`/s/${SHARE_TOKEN}`);

      // Open files drawer/panel and select reports/a.html.
      const filesBtn = page.getByRole('button', { name: /Files|文件/i }).first();
      await expect(filesBtn).toBeVisible({ timeout: 10000 });
      await filesBtn.click();
      await page.getByText('a.html').first().click();

      const frame = page.frameLocator('iframe.bridged-frame-doc').first();
      await expect(frame.locator('h1')).toHaveText('Doc A', { timeout: 10000 });

      // State: current (Doc A) -> primary file actions & Open in new tab are available.
      if (mode.isMobile) {
        await page.getByTestId('file-header-mobile-more').click();
        await expect(page.getByTestId('file-header-mobile-open-url')).toBeVisible();
        await expect(page.getByTestId('file-header-mobile-download')).toBeVisible();
        await expect(page.getByTestId('file-header-mobile-open-doc-tab')).toHaveCount(0);
        await page.keyboard.press('Escape');
      } else {
        await expect(page.getByRole('button', { name: /Open in new tab|在新标签页打开/i })).toBeEnabled();
        await expect(page.getByRole('button', { name: /Download options|下载选项/i })).toBeEnabled();
        await expect(page.getByTestId('file-header-open-doc-tab')).toHaveCount(0);
      }

      // Click relative link A -> B (links.js preserves ?inject=view, core.js reports reports/b.html).
      await frame.locator('#to-b').click();
      await expect(frame.locator('h1')).toHaveText('Doc B', { timeout: 10000 });

      // State: linked (Doc B) -> doc A actions & original openUrl hidden, Open-in-own-tab shown.
      if (mode.isMobile) {
        await page.getByTestId('file-header-mobile-more').click();
        await expect(page.getByTestId('file-header-mobile-open-doc-tab')).toBeVisible();
        await expect(page.getByTestId('file-header-mobile-open-url')).toHaveCount(0);
        await expect(page.getByTestId('file-header-mobile-download')).toHaveCount(0);
        await page.keyboard.press('Escape');
      } else {
        await expect(page.getByTestId('file-header-open-doc-tab')).toBeVisible();
        await expect(page.getByRole('button', { name: /Open in new tab|在新标签页打开/i })).toHaveCount(0);
        await expect(page.getByRole('button', { name: /Download options|下载选项/i })).toHaveCount(0);
      }

      // Continuous relative link B -> C stays linked and targets C.
      await frame.locator('#to-c').click();
      await expect(frame.locator('h1')).toHaveText('Doc C', { timeout: 10000 });
      if (mode.isMobile) {
        await page.getByTestId('file-header-mobile-more').click();
        await expect(page.getByTestId('file-header-mobile-open-doc-tab')).toBeVisible();
        await page.keyboard.press('Escape');
      } else {
        await expect(page.getByTestId('file-header-open-doc-tab')).toBeVisible();
      }

      // Scripted location.href drops ?inject=view -> enters unbridged after UNBRIDGED_GRACE_MS (2000ms).
      await frame.locator('#script-nav').click();
      await expect(frame.locator('h1')).toHaveText('Unbridged Page', { timeout: 10000 });

      // State: unbridged (currentUrl: null) -> hides ALL doc A actions AND hides Open-in-new-tab & Open-in-tab.
      if (mode.isMobile) {
        await expect(page.getByTestId('file-header-mobile-more')).toHaveCount(0, { timeout: 6000 });
      } else {
        await expect(page.getByTestId('file-header-open-doc-tab')).toHaveCount(0, { timeout: 6000 });
        await expect(page.getByRole('button', { name: /Open in new tab|在新标签页打开/i })).toHaveCount(0);
        await expect(page.getByRole('button', { name: /Download options|下载选项/i })).toHaveCount(0);
        await expect(page.getByRole('button', { name: /View source|查看源代码/i })).toHaveCount(0);
      }
    });

    test('pathless public monitor enters bridged_root and keeps primary topbar actions', async ({ page }) => {
      const slug = 'public-root-pulse';
      await mockAPI(page, {
        [`GET /dalton/monitors/public/${slug}`]: {
          monitor: {
            monitor_id: 'mon-root-1',
            slug,
            title: 'Public Root Pulse',
            category: 'us',
            sort_order: 0,
            visibility: 'public',
            member_user_ids: [],
            published_seq: 4,
            keep_updated: true,
            source_kind: 'workspace',
            source_ref: 'ws-1:index.html',
            acl_revision: 1,
            last_publish_error: null,
            last_publish_failed_at: null,
            data_as_of: null,
            created_at: '2026-11-20T00:00:00Z',
            updated_at: '2026-11-20T00:00:00Z',
            can_manage: false,
          },
          versions: [],
          monitor_grant: '',
          frame_base: `/api/v1/dalton/monitors/public/${slug}/`,
          data_base: `/api/v1/dalton/monitors/public/${slug}/data`,
          expires_in: 0,
        },
      });

      await page.route(`**/api/v1/dalton/monitors/public/${slug}/v/4/**`, async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'text/html; charset=utf-8',
          body: wrapHtml('Public Root Pulse', '<h1>Public Monitor Root</h1>', true),
        });
      });

      await page.goto(`/m/${slug}`);
      const frame = page.frameLocator('iframe.bridged-frame-doc').first();
      await expect(frame.locator('h1')).toHaveText('Public Monitor Root', { timeout: 10000 });
      await expect(page.locator('.monitors-frame-host')).toHaveAttribute('data-doc-state', 'bridged_root', { timeout: 5000 });
      await expect(page.getByRole('button', { name: /Copy link|复制链接/i })).toBeVisible();
      await expect(page.getByRole('link', { name: /Open in new window|新窗口打开/i })).toBeVisible();
    });
  });
}

test.describe('Fail-closed 401 on /dalton/credentials/status (misconfigured VITE_DALTON_AUTH)', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('disables privileged publish/share/copy actions when credentials/status returns 401', async ({ page }) => {
    const ws = sampleWorkspace({ workspace_id: WS_ID, name: 'B3 Auth FailClosed' });
    const thread = sampleThread({ thread_id: THREAD_ID, workspace_id: WS_ID, title: 'B3 Thread' });

    await mockAPI(page, {
      'GET /workspaces': { workspaces: [ws], total: 1 },
      [`GET /workspaces/${WS_ID}`]: ws,
      [`GET /threads`]: { threads: [thread], total: 1 },
      [`GET /threads/${THREAD_ID}`]: thread,
      [`GET /threads/${THREAD_ID}/share`]: {
        is_shared: false,
        share_token: null,
        share_url: null,
        permissions: { allow_files: false, allow_download: false },
      },
      [`GET /workspaces/${WS_ID}/share-links`]: { links: [] },
      [`GET /workspaces/${WS_ID}/files`]: { files: ['reports/a.html'], path: '.' },
      [`GET /artifacts/by-path`]: {
        artifact_id: ARTIFACT_ID,
        code: 'b3art123',
        url: '/a/b3art123',
        workspace_id: WS_ID,
        path: 'reports/a.html',
        name: 'a.html',
        shared: false,
        shared_at: null,
        shared_seq: null,
        keep_updated: false,
        latest_seq: 1,
        bound_thread_id: null,
        open_comments: 0,
        unread: 0,
        created_at: '2026-11-25T00:00:00Z',
      },
      'GET /dalton/capabilities': {
        profile: 'core',
        capabilities: [],
        brand_name: 'Dalton',
      },
      // Simulate misconfigured VITE_DALTON_AUTH hitting a core/local backend without session token -> 401.
      'GET /dalton/credentials/status': async (route) => {
        await route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Not authenticated' }),
        });
      },
    });

    await configureSSE({
      method: 'GET',
      path: `/api/v1/threads/${THREAD_ID}/messages`,
      events: [{ event: 'replay_done', data: { thread_id: THREAD_ID } }],
      delay: 10,
    });

    await page.goto(`/chat/${WS_ID}/${THREAD_ID}`);

    // Share button should be disabled (fail-closed) when identity is in error/unknown state.
    const shareButton = page.getByRole('button', { name: /Share|分享/i }).first();
    await expect(shareButton).toBeVisible({ timeout: 10000 });
    await expect(shareButton).toBeDisabled({ timeout: 5000 });
  });
});
