/**
 * Playwright E2E suite for External Member Permissions Batch 3:
 *   1. Shared chat (/s/:token) HTML preview in-frame relative link navigation A -> B -> C,
 *      opening C in its own tab (verifying the new tab actually loads C), and scripted
 *      location.href navigation without ?inject=view falling into unbridged (hiding all
 *      file actions and "Open in new tab"), verified on desktop (1280px) and mobile (390px)
 *      using the real page bridge scripts.
 *   2. Logged-in external member preview (/chat/t/:threadId) on a Dalton build
 *      (VITE_DALTON_UI=true, automatically spawned on a worker-local Vite dev server
 *      when not already active): waits for GET /api/v1/dalton/credentials/status to return
 *      caller_tier: 'external', verifies external-member UI restrictions (ShareButton hidden,
 *      read-only ?inject=view preview without comment controls), and runs A -> B -> C ->
 *      open C in own tab -> scripted location.href unbridged on desktop & mobile.
 *   3. Pathless public monitor (/m/:slug -> /api/v1/dalton/monitors/public/:slug/v/:seq/)
 *      in an opaque sandbox renders its page content, treats root: true as unrecognized
 *      (unbridged), keeps Copy link visible, and hides Open in new window on desktop & mobile.
 *   4. Misconfigured Dalton frontend (VITE_DALTON_AUTH intentionally unset) connected to a
 *      REAL core/local backend: waits for an unmocked HTTP 401 from
 *      GET /api/v1/dalton/credentials/status, then verifies Publish, Share, Audience
 *      modification (fieldset + radio with organization audience enabled), and Copy link
 *      controls all fail closed while keeping Unshare / Stop sharing available.
 *
 * Run outside sandbox via Playwright:
 *
 *   # Suites 1-3 (Playwright built-in dev server + worker-local Dalton server for Suite 2 + mock SSE server):
 *   cd web && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
 *     npx playwright test e2e/external-member-perms-b3.spec.js
 *
 *   # Suite 4 (real core/local backend 401 verification):
 *   # Prerequisites:
 *   #   1. A real Dalton core/local backend running (e.g. DALTON_PROFILE=core DALTON_AUTH_MODE=local
 *   #      listening on http://127.0.0.1:8000 so unauthenticated GET /api/v1/dalton/credentials/status
 *   #      returns HTTP 401).
 *   #   2. A Dalton frontend server started WITHOUT VITE_DALTON_AUTH, e.g.:
 *   #      cd web && VITE_DALTON_UI=true VITE_DALTON_PROFILE=core VITE_HOST_MODE=oss \
 *   #        VITE_DALTON_AUTH= VITE_API_BASE_URL=http://127.0.0.1:8000 \
 *   #        pnpm dev -- --port 5178
 *   #   3. Run Playwright pointing Suite 4 at that frontend:
 *   cd web && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
 *     E2E_DALTON_REAL_FRONTEND_URL=http://127.0.0.1:5178 \
 *     npx playwright test e2e/external-member-perms-b3.spec.js
 */
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test as baseTest } from '@playwright/test';
import { test, expect, mockAPI, configureSSE, resetMockServer } from './fixtures.js';
import { sampleWorkspace, sampleThread, sseEvents, defaultResponses } from './helpers/mockResponses.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB_DIR = resolve(HERE, '..');
const BRIDGE_DIR = resolve(HERE, '../../src/server/preview_proxy/bridge');
const VIEW_BRIDGE_PARTS = ['opaque', 'core', 'links', 'loads', 'requests'];
const VIEW_BRIDGE_SCRIPT = [
  "(function () {\n'use strict';\nif (!window.parent || window.parent === window) return;",
  ...VIEW_BRIDGE_PARTS.map((name) => readFileSync(resolve(BRIDGE_DIR, `${name}.js`), 'utf8')),
  'hello();\n})();',
].join('\n');

const SHARE_TOKEN = 'b3-share-tok';
const THREAD_ID = 'b0000003-0000-4000-8000-000000000003';
const WS_ID = 'a0000003-0000-4000-8000-000000000003';
const APP_API_PORT = Number(process.env.E2E_MOCK_PORT) || 4100;
const REAL_DALTON_FRONTEND_URL = (process.env.E2E_DALTON_REAL_FRONTEND_URL || '').replace(/\/+$/, '');

function wrapHtml(title, body, injectBridge) {
  const script = injectBridge ? `<script>${VIEW_BRIDGE_SCRIPT}</script>` : '';
  return `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title>${script}</head><body>${body}</body></html>`;
}

async function findFreePort() {
  return new Promise((resolvePort, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      server.close((err) => (err ? reject(err) : resolvePort(port)));
    });
  });
}

async function waitForHttpReady(url, child, stderrChunks, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Dalton Vite dev server exited early (${child.exitCode}): ${stderrChunks.join('')}`);
    }
    const ok = await new Promise((resolveOk) => {
      const req = http.get(url, (res) => {
        res.resume();
        resolveOk(res.statusCode !== undefined && res.statusCode < 500);
      });
      req.on('error', () => resolveOk(false));
      req.setTimeout(1000, () => {
        req.destroy();
        resolveOk(false);
      });
    });
    if (ok) return;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`Timed out waiting for Dalton Vite dev server at ${url}: ${stderrChunks.join('')}`);
}

const daltonTest = test.extend({
  daltonOrigin: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, provide) => {
      if (/^(true|1|yes|on)$/i.test((process.env.VITE_DALTON_UI || '').trim())) {
        await provide('');
        return;
      }
      const port = await findFreePort();
      const origin = `http://127.0.0.1:${port}`;
      const stderrChunks = [];
      const env = { ...process.env };
      for (const key of ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']) {
        delete env[key];
      }
      env.VITE_HOST_MODE = 'oss';
      env.VITE_DALTON_UI = 'true';
      env.VITE_SUPABASE_URL = '';
      env.VITE_SUPABASE_PUBLISHABLE_KEY = '';
      env.VITE_API_BASE_URL = `http://127.0.0.1:${APP_API_PORT}`;

      const viteBin = resolve(WEB_DIR, 'node_modules/vite/bin/vite.js');
      const child = spawn(process.execPath, [viteBin, '--port', String(port), '--strictPort'], {
        cwd: WEB_DIR,
        env,
        stdio: ['ignore', 'ignore', 'pipe'],
      });
      child.stderr?.on('data', (chunk) => stderrChunks.push(String(chunk)));
      try {
        await waitForHttpReady(origin, child, stderrChunks);
        await provide(origin);
      } finally {
        if (child.exitCode === null) {
          child.kill('SIGTERM');
          await new Promise((r) => {
            const timer = setTimeout(() => {
              if (child.exitCode === null) child.kill('SIGKILL');
              r();
            }, 3000);
            child.once('exit', () => {
              clearTimeout(timer);
              r();
            });
          });
        }
      }
    },
    { scope: 'worker' },
  ],
});

function serveReportHtml(url, hasBridge) {
  if (url.pathname.endsWith('/reports/a.html')) {
    return wrapHtml('Report A', '<h1>Doc A</h1><a id="to-b" href="b.html">Go to B</a>', hasBridge);
  }
  if (url.pathname.endsWith('/reports/b.html')) {
    return wrapHtml('Report B', '<h1>Doc B</h1><a id="to-c" href="c.html">Go to C</a>', hasBridge);
  }
  if (url.pathname.endsWith('/reports/c.html')) {
    return wrapHtml(
      'Report C',
      '<h1>Doc C</h1><button id="script-nav" onclick="location.href=\'raw.html\'">Script nav</button>',
      hasBridge,
    );
  }
  // raw.html reached via scripted location.href drops ?inject=view -> served without bridge -> unbridged.
  return wrapHtml('Raw Unbridged', '<h1>Unbridged Page</h1>', hasBridge);
}

function makeArtifactRecord(filePath, { shared = false, audiences = ['public', 'organization'] } = {}) {
  const name = filePath.split('/').pop() || filePath;
  const suffix = name.replace(/[^a-z0-9]/gi, '').slice(0, 6) || 'doc';
  return {
    artifact_id: `6f1c2a7e-3b4d-4e5f-8a9b-0c1d2e3f${suffix.padEnd(4, '0').slice(0, 4)}`,
    code: `b3${suffix}`,
    url: `/a/b3${suffix}`,
    workspace_id: WS_ID,
    path: filePath,
    name,
    shared,
    audience: 'public',
    audiences,
    shared_at: shared ? '2026-11-25T00:00:00Z' : null,
    shared_seq: shared ? 1 : null,
    keep_updated: false,
    latest_seq: shared ? 1 : 0,
    bound_thread_id: null,
    open_comments: 0,
    unread: 0,
    created_at: '2026-11-25T00:00:00Z',
  };
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

async function openReportAFromTree(page) {
  const filesBtn = page
    .locator('button[title="Workspace Files"], button[aria-label="Workspace Files"], button[title="工作区文件"]')
    .first();
  await expect(filesBtn).toBeVisible({ timeout: 10000 });
  await filesBtn.click();

  // In a clean browser the `reports` directory starts collapsed, so expand it before selecting `reports/a.html`.
  const reportsDir = page.locator('[data-tree-row][data-row-kind="dir"][data-row-path="reports"]').first();
  await expect(reportsDir).toBeVisible({ timeout: 10000 });
  if ((await reportsDir.getAttribute('aria-expanded')) === 'false') {
    await reportsDir.dispatchEvent('click');
  }

  const fileRow = page.locator('[data-tree-row][data-row-kind="file"][data-row-path="reports/a.html"]').first();
  await expect(fileRow).toBeVisible({ timeout: 10000 });
  await fileRow.dispatchEvent('click');
}

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

  await page.route(`**/api/v1/public/shared/${SHARE_TOKEN}/files/serve/**`, async (route) => {
    const url = new URL(route.request().url());
    const hasViewBridge = url.searchParams.get('inject') === 'view';
    await route.fulfill({
      status: 200,
      contentType: 'text/html; charset=utf-8',
      body: serveReportHtml(url, hasViewBridge),
    });
  });
}

async function setupExternalMemberReportRoutes(page) {
  const ws = sampleWorkspace({ workspace_id: WS_ID, name: 'External Member B3' });
  const thread = sampleThread({ thread_id: THREAD_ID, workspace_id: WS_ID, title: 'External Member Thread' });

  await mockAPI(page, {
    'GET /workspaces': { workspaces: [ws], total: 1, limit: 20, offset: 0 },
    [`GET /workspaces/${WS_ID}`]: ws,
    'GET /threads': { threads: [thread], total: 1 },
    [`GET /threads/${THREAD_ID}`]: thread,
    [`GET /threads/${THREAD_ID}/status`]: { can_reconnect: false, status: 'idle' },
    [`GET /threads/${THREAD_ID}/turns`]: {
      thread_id: THREAD_ID,
      turns: [{ turn_index: 0, edit_checkpoint_id: 'cp-0', regenerate_checkpoint_id: 'cp-0' }],
      retry_checkpoint_id: 'cp-0',
    },
    [`GET /threads/${THREAD_ID}/share`]: {
      is_shared: false,
      share_token: null,
      share_url: null,
      permissions: { allow_files: false, allow_download: false },
    },
    [`GET /workspaces/${WS_ID}/share-links`]: { links: [] },
    [`GET /workspaces/${WS_ID}/files`]: {
      path: '.',
      files: ['reports/a.html', 'reports/b.html', 'reports/c.html', 'reports/raw.html'],
    },
    [`GET /workspaces/${WS_ID}/files/read`]: (route) => {
      const filePath = new URL(route.request().url()).searchParams.get('path') || 'reports/a.html';
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          workspace_id: WS_ID,
          path: filePath,
          content: `<h1>${filePath}</h1>`,
          mime: 'text/html',
          truncated: false,
        }),
      });
    },
    [`POST /workspaces/${WS_ID}/file-grant`]: {
      prefix: '/api/v1/wsfiles/g/ext-grant/',
      expires_in: 3600,
    },
    [`GET /workspaces/${WS_ID}/artifacts`]: (route) => {
      const filePath = new URL(route.request().url()).searchParams.get('path') || 'reports/a.html';
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ artifact: makeArtifactRecord(filePath) }),
      });
    },
    [`POST /workspaces/${WS_ID}/artifacts`]: (route) => {
      const body = JSON.parse(route.request().postData() || '{}');
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(makeArtifactRecord(body.path || 'reports/a.html')),
      });
    },
    'GET /dalton/monitors/by-artifact/*': (route) =>
      route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Not found' }) }),
    'GET /dalton/capabilities': {
      profile: 'everflow',
      capabilities: [],
      brand_name: 'Everflow',
    },
    // Logged-in external member identity (consumed when VITE_DALTON_UI=true).
    'GET /dalton/credentials/status': {
      enabled: true,
      caller_tier: 'external',
      auth_mode: 'local',
      can_invite: false,
      is_pm: false,
      colleague_sharing_enabled: false,
    },
  });

  await configureSSE({
    method: 'GET',
    path: `/api/v1/threads/${THREAD_ID}/messages/replay`,
    events: [sseEvents.replayDone(THREAD_ID)],
    delay: 10,
  });

  const servedInjectModes = [];
  await page.route('**/api/v1/wsfiles/g/ext-grant/**', async (route) => {
    const url = new URL(route.request().url());
    const inject = url.searchParams.get('inject');
    servedInjectModes.push({ path: url.pathname, inject });
    const hasBridge = inject === 'view' || inject === 'comments';
    await route.fulfill({
      status: 200,
      contentType: 'text/html; charset=utf-8',
      body: serveReportHtml(url, hasBridge),
    });
  });
  return { servedInjectModes };
}

async function verifyRelativeLinksNewTabAndUnbridged(page, mode, { expectPublish = false } = {}) {
  const frame = page.frameLocator('iframe.bridged-frame-doc').last();
  await expect(frame.locator('h1')).toHaveText('Doc A', { timeout: 10000 });

  // State: current (Doc A) -> primary file actions & Open in new tab are available.
  if (mode.isMobile) {
    await page.getByTestId('file-header-mobile-more').click();
    await expect(page.getByTestId('file-header-mobile-open-url')).toBeVisible();
    await expect(page.getByTestId('file-header-mobile-download')).toBeVisible();
    if (expectPublish) {
      await expect(page.getByTestId('file-header-mobile-publish')).toBeVisible();
    }
    await expect(page.getByTestId('file-header-mobile-open-doc-tab')).toHaveCount(0);
    await page.keyboard.press('Escape');
  } else {
    await expect(page.getByRole('button', { name: /Open in new tab|在新标签页打开/i })).toBeEnabled();
    await expect(page.getByRole('button', { name: /Download options|下载选项/i })).toBeEnabled();
    if (expectPublish) {
      await expect(page.getByRole('button', { name: /^Publish$|^发布$/i })).toBeEnabled();
    }
    await expect(page.getByTestId('file-header-open-doc-tab')).toHaveCount(0);
  }

  // Click relative link A -> B (links.js preserves ?inject=..., core.js reports reports/b.html).
  await frame.locator('#to-b').click();
  await expect(frame.locator('h1')).toHaveText('Doc B', { timeout: 10000 });

  // State: linked (Doc B) -> doc A actions & original openUrl hidden, Open-in-own-tab shown.
  if (mode.isMobile) {
    await page.getByTestId('file-header-mobile-more').click();
    await expect(page.getByTestId('file-header-mobile-open-doc-tab')).toBeVisible();
    await expect(page.getByTestId('file-header-mobile-open-url')).toHaveCount(0);
    await expect(page.getByTestId('file-header-mobile-download')).toHaveCount(0);
    await expect(page.getByTestId('file-header-mobile-publish')).toHaveCount(0);
    await page.keyboard.press('Escape');
  } else {
    await expect(page.getByTestId('file-header-open-doc-tab')).toBeVisible();
    await expect(page.getByRole('button', { name: /Open in new tab|在新标签页打开/i })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /Download options|下载选项/i })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^Publish$|^发布$/i })).toHaveCount(0);
  }

  // Continuous relative link B -> C stays linked and targets C; clicking "Open in its own tab" actually opens C.
  await frame.locator('#to-c').click();
  await expect(frame.locator('h1')).toHaveText('Doc C', { timeout: 10000 });
  if (mode.isMobile) {
    await page.getByTestId('file-header-mobile-more').click();
    await page.getByTestId('file-header-mobile-open-doc-tab').click();
  } else {
    await page.getByTestId('file-header-open-doc-tab').click();
  }

  // Verify the new tab actually opened reports/c.html and restored doc C's current actions.
  await expect(page.locator('nav.file-panel-crumb-trail[aria-label="reports/c.html"]')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('.file-panel-crumb.is-file')).toHaveText('c.html');
  const tabCFrame = page.frameLocator('iframe.bridged-frame-doc').last();
  await expect(tabCFrame.locator('h1')).toHaveText('Doc C', { timeout: 10000 });
  if (mode.isMobile) {
    await page.getByTestId('file-header-mobile-more').click();
    await expect(page.getByTestId('file-header-mobile-open-doc-tab')).toHaveCount(0);
    await expect(page.getByTestId('file-header-mobile-download')).toBeVisible();
    await expect(page.getByTestId('file-header-mobile-open-url')).toBeVisible();
    await page.keyboard.press('Escape');
  } else {
    await expect(page.getByTestId('file-header-open-doc-tab')).toHaveCount(0);
    await expect(page.getByRole('button', { name: /Open in new tab|在新标签页打开/i })).toBeEnabled();
    await expect(page.getByRole('button', { name: /Download options|下载选项/i })).toBeEnabled();
  }

  // Scripted location.href drops ?inject=... -> enters unbridged after UNBRIDGED_GRACE_MS (2000ms).
  await tabCFrame.locator('#script-nav').click();
  await expect(tabCFrame.locator('h1')).toHaveText('Unbridged Page', { timeout: 10000 });

  // State: unbridged (currentUrl: null) -> hides ALL file actions AND hides Open-in-new-tab & Open-in-tab.
  if (mode.isMobile) {
    await expect(page.getByTestId('file-header-mobile-more')).toHaveCount(0, { timeout: 6000 });
  } else {
    await expect(page.getByTestId('file-header-open-doc-tab')).toHaveCount(0, { timeout: 6000 });
    await expect(page.getByRole('button', { name: /Open in new tab|在新标签页打开/i })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /Download options|下载选项/i })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /View source|查看源代码/i })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^Publish$|^发布$/i })).toHaveCount(0);
  }
}

const VIEWPORT_MODES = [
  { name: 'desktop (1280px)', viewport: { width: 1280, height: 800 }, isMobile: false },
  { name: 'mobile (390px)', viewport: { width: 390, height: 844 }, isMobile: true },
];

for (const mode of VIEWPORT_MODES) {
  test.describe(`Preview bridge doc state machine on ${mode.name}`, () => {
    test.use({ viewport: mode.viewport, isMobile: mode.isMobile, hasTouch: mode.isMobile });

    test.beforeEach(async () => {
      await resetMockServer();
    });

    test('shared chat: expands reports/, navigates A -> B -> C, opens C in own tab, and hides all actions on scripted unbridged nav', async ({ page }) => {
      await setupSharedReportRoutes(page);
      await page.goto(`/s/${SHARE_TOKEN}`);
      await openReportAFromTree(page);
      await verifyRelativeLinksNewTabAndUnbridged(page, mode, { expectPublish: false });
    });

    test('pathless public monitor in opaque sandbox treats root: true as unbridged, hides Open in new window, and keeps Copy link', async ({ page }) => {
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

      await page.addInitScript(() => {
        if (window === window.top) {
          const log = { ready: [], pong: [] };
          window.__b3MonitorHandshake = log;
          window.addEventListener(
            'message',
            (event) => {
              const data = event.data;
              if (!data || typeof data !== 'object') return;
              const fromMonitorFrame = Array.from(document.querySelectorAll('iframe.bridged-frame-doc')).some(
                (el) => el.contentWindow === event.source,
              );
              if (!fromMonitorFrame) return;
              if (data.type === 'page:ready') {
                log.ready.push({
                  root: data.root === true,
                  docId: typeof data.docId === 'string' ? data.docId : null,
                  origin: event.origin,
                });
              } else if (data.type === 'page:pong') {
                log.pong.push({
                  seq: typeof data.seq === 'number' ? data.seq : null,
                  docId: typeof data.docId === 'string' ? data.docId : null,
                  origin: event.origin,
                });
              }
            },
            true,
          );
        } else {
          const pings = [];
          window.__b3MonitorPings = pings;
          window.addEventListener(
            'message',
            (event) => {
              const data = event.data;
              if (event.source !== window.parent || !data || typeof data !== 'object') return;
              if (data.type === 'page:ping' && typeof data.seq === 'number') {
                pings.push(data.seq);
              }
            },
            true,
          );
        }
      });

      await page.goto(`/m/${slug}`);
      const frame = page.frameLocator('iframe.bridged-frame-doc').first();
      await expect(frame.locator('h1')).toHaveText('Public Monitor Root', { timeout: 10000 });

      const iframeHandle = await page.locator('iframe.bridged-frame-doc').first().elementHandle();
      const childFrame = await iframeHandle?.contentFrame();
      expect(childFrame).toBeTruthy();

      // Verify the root entry sent page:ready (root: true, valid docId) and replied to page:ping
      // with a page:pong matching both seq and docId before asserting UI state.
      await expect
        .poll(
          async () => {
            const [hostLog, childPings] = await Promise.all([
              page.evaluate(() => window.__b3MonitorHandshake ?? { ready: [], pong: [] }),
              childFrame.evaluate(() => window.__b3MonitorPings ?? []),
            ]);
            const ready =
              hostLog.ready.find((m) => m.root && typeof m.docId === 'string' && m.docId.length > 0) ?? null;
            const matchedPong = ready
              ? hostLog.pong.find(
                  (m) =>
                    m.docId === ready.docId &&
                    typeof m.seq === 'number' &&
                    m.seq >= 1 &&
                    childPings.includes(m.seq),
                ) ?? null
              : null;
            return {
              hasReady: ready !== null,
              hasPing: childPings.length > 0,
              hasMatchedPong: matchedPong !== null,
            };
          },
          { timeout: 5000 },
        )
        .toEqual({ hasReady: true, hasPing: true, hasMatchedPong: true });

      await expect(page.locator('.monitors-frame-host')).toHaveAttribute('data-doc-state', 'unbridged', { timeout: 5000 });
      await expect(page.getByRole('button', { name: /Copy link|复制链接/i })).toBeVisible();
      await expect(page.getByRole('link', { name: /Open in new window|新窗口打开/i })).toHaveCount(0);
    });
  });

  daltonTest.describe(`Logged-in external member preview (Dalton build) on ${mode.name}`, () => {
    daltonTest.use({ viewport: mode.viewport, isMobile: mode.isMobile, hasTouch: mode.isMobile });
    daltonTest.setTimeout(60_000);

    daltonTest.beforeEach(async () => {
      await resetMockServer();
    });

    daltonTest('confirms external caller_tier via /credentials/status, hides ShareButton & comment controls, navigates A -> B -> C -> own tab -> unbridged', async ({ page, daltonOrigin }) => {
      const { servedInjectModes } = await setupExternalMemberReportRoutes(page);

      const credsResPromise = page.waitForResponse(
        (res) => res.url().includes('/api/v1/dalton/credentials/status') && res.request().method() === 'GET',
        { timeout: 15000 },
      );

      await page.goto(`${daltonOrigin}/chat/t/${THREAD_ID}`);

      const credsRes = await credsResPromise;
      expect(credsRes.status()).toBe(200);
      const credsBody = await credsRes.json();
      expect(credsBody.caller_tier).toBe('external');

      // External-member-specific UI restriction: ShareButton is hidden when no active share exists.
      await expect(page.getByRole('button', { name: /Share conversation|分享对话/i })).toHaveCount(0);

      await openReportAFromTree(page);
      await verifyRelativeLinksNewTabAndUnbridged(page, mode, { expectPublish: true });

      // External member HTML preview uses read-only ?inject=view (never owner ?inject=comments) and exposes no Comment toggle.
      expect(servedInjectModes.some((entry) => entry.path.endsWith('/reports/a.html') && entry.inject === 'view')).toBe(true);
      expect(servedInjectModes.some((entry) => entry.inject === 'comments')).toBe(false);
      await expect(page.getByRole('button', { name: /^Comment$|^评论$/i })).toHaveCount(0);
    });
  });
}

/**
 * Real core/local backend 401 fail-closed suite.
 *
 * Uses Playwright's raw test runner (NOT the mock-server fixture) against a separately
 * started Dalton frontend (E2E_DALTON_REAL_FRONTEND_URL) built with VITE_DALTON_UI=true,
 * VITE_DALTON_PROFILE=core, VITE_HOST_MODE=oss, and VITE_DALTON_AUTH intentionally unset,
 * connected to a real core/local backend.
 *
 * GET /api/v1/dalton/credentials/status is NEVER intercepted: the test asserts that the
 * real backend returns HTTP 401 and that Publish, Share, Audience modification, and Copy
 * link controls all fail closed.
 */
baseTest.describe('Real core/local backend 401 fail-closed (misconfigured VITE_DALTON_AUTH)', () => {
  baseTest.skip(
    !REAL_DALTON_FRONTEND_URL,
    'Set E2E_DALTON_REAL_FRONTEND_URL to a Dalton frontend (VITE_DALTON_UI=true, VITE_DALTON_PROFILE=core, VITE_HOST_MODE=oss, VITE_DALTON_AUTH unset) connected to a real core/local backend.',
  );
  baseTest.use({ viewport: { width: 1280, height: 800 } });

  async function installNonIdentityShellFixtures(page, { sharedThread, sharedFile }) {
    const ws = sampleWorkspace({ workspace_id: WS_ID, name: 'B3 Real 401 Workspace' });
    const thread = sampleThread({ thread_id: THREAD_ID, workspace_id: WS_ID, title: 'B3 Real 401 Thread' });
    const artifact = makeArtifactRecord('reports/a.html', { shared: sharedFile });

    // Intercept only non-identity chat/file shell endpoints so the unauthenticated browser
    // can render the chat header & file panel while /api/v1/dalton/credentials/status hits
    // the real core/local backend untouched.
    await page.route('**/api/v1/**', async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname;
      const method = req.method();

      // NEVER intercept credentials/status — must hit the real core/local backend and return 401.
      if (path === '/api/v1/dalton/credentials/status') {
        return route.continue();
      }
      if (path === '/api/v1/users/me') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(defaultResponses['GET /users/me']) });
      }
      if (path === '/api/v1/users/me/preferences') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(defaultResponses['GET /users/me/preferences']) });
      }
      if (path === '/api/v1/workspaces' && method === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ workspaces: [ws], total: 1, limit: 20, offset: 0 }) });
      }
      if (path === `/api/v1/workspaces/${WS_ID}` && method === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(ws) });
      }
      if (path === '/api/v1/threads' && method === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ threads: [thread], total: 1 }) });
      }
      if (path === `/api/v1/threads/${THREAD_ID}` && method === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(thread) });
      }
      if (path === `/api/v1/threads/${THREAD_ID}/status` && method === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ can_reconnect: false, status: 'idle' }) });
      }
      if (path === `/api/v1/threads/${THREAD_ID}/turns` && method === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ thread_id: THREAD_ID, turns: [], retry_checkpoint_id: null }) });
      }
      if (path === `/api/v1/threads/${THREAD_ID}/messages/replay` && method === 'GET') {
        const sseBody = `event: replay_done\ndata: ${JSON.stringify({ thread_id: THREAD_ID })}\n\n`;
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: sseBody });
      }
      if (path === `/api/v1/threads/${THREAD_ID}/share` && method === 'GET') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(
            sharedThread
              ? {
                  is_shared: true,
                  share_token: 'existing-share-tok',
                  share_url: '/s/existing-share-tok',
                  permissions: { allow_files: true, allow_download: false, audience: 'public' },
                  audiences: ['public', 'organization'],
                }
              : {
                  is_shared: false,
                  share_token: null,
                  share_url: null,
                  permissions: { allow_files: false, allow_download: false, audience: 'public' },
                  audiences: ['public', 'organization'],
                },
          ),
        });
      }
      if (path === `/api/v1/workspaces/${WS_ID}/share-links` && method === 'GET') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ links: sharedFile ? [{ code: artifact.code, path: artifact.path, shared: true }] : [] }),
        });
      }
      if (path === `/api/v1/workspaces/${WS_ID}/files` && method === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ path: '.', files: ['reports/a.html'] }) });
      }
      if (path === `/api/v1/workspaces/${WS_ID}/files/read` && method === 'GET') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ workspace_id: WS_ID, path: 'reports/a.html', content: '<h1>Doc A</h1>', mime: 'text/html', truncated: false }),
        });
      }
      if (path === `/api/v1/workspaces/${WS_ID}/file-grant` && method === 'POST') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ prefix: '/api/v1/wsfiles/g/real401-grant/', expires_in: 3600 }) });
      }
      if (path.startsWith('/api/v1/wsfiles/g/real401-grant/')) {
        return route.fulfill({
          status: 200,
          contentType: 'text/html; charset=utf-8',
          body: serveReportHtml(url, url.searchParams.get('inject') === 'view'),
        });
      }
      if (path === `/api/v1/workspaces/${WS_ID}/artifacts` && method === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ artifact }) });
      }
      if (path === `/api/v1/artifacts/${artifact.artifact_id}` && method === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(artifact) });
      }
      if (path.startsWith('/api/v1/dalton/monitors/by-artifact/')) {
        return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Not found' }) });
      }
      if (path === '/api/v1/features') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ features: [] }) });
      }
      if (path === '/api/v1/models') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(defaultResponses['GET /models']) });
      }
      return route.continue();
    });
  }

  baseTest('unshared thread & file: real 401 disables Share (Confirming account permissions…) and blocks Publish', async ({ page }) => {
    await installNonIdentityShellFixtures(page, { sharedThread: false, sharedFile: false });

    const credsResPromise = page.waitForResponse(
      (res) => res.url().includes('/api/v1/dalton/credentials/status') && res.request().method() === 'GET',
      { timeout: 15000 },
    );

    await page.goto(`${REAL_DALTON_FRONTEND_URL}/chat/t/${THREAD_ID}`);

    const credsRes = await credsResPromise;
    expect(credsRes.status()).toBe(401);

    // Share trigger is disabled fail-closed and announces "Confirming account permissions…".
    const disabledShareBtn = page.getByRole('button', {
      name: /Confirming account permissions…|正在确认账号权限…/i,
    });
    await expect(disabledShareBtn).toBeVisible({ timeout: 10000 });
    await expect(disabledShareBtn).toBeDisabled();

    // Open reports/a.html: Publish action must be blocked (not rendered) while caller identity is unknown/error.
    await openReportAFromTree(page);
    const frame = page.frameLocator('iframe.bridged-frame-doc').last();
    await expect(frame.locator('h1')).toHaveText('Doc A', { timeout: 10000 });
    await expect(page.getByRole('button', { name: /^Publish$|^发布$/i })).toHaveCount(0);
  });

  baseTest('active share & shared file: real 401 blocks Publish, Audience modification, and Copy link while keeping Stop sharing available', async ({ page }) => {
    await installNonIdentityShellFixtures(page, { sharedThread: true, sharedFile: true });

    const credsResPromise = page.waitForResponse(
      (res) => res.url().includes('/api/v1/dalton/credentials/status') && res.request().method() === 'GET',
      { timeout: 15000 },
    );

    await page.goto(`${REAL_DALTON_FRONTEND_URL}/chat/t/${THREAD_ID}`);

    const credsRes = await credsResPromise;
    expect(credsRes.status()).toBe(401);

    // Share popover opens solely to allow stopping an already-active share.
    const shareBtn = page.getByRole('button', { name: /Share conversation|分享对话/i }).first();
    await expect(shareBtn).toBeVisible({ timeout: 10000 });
    await expect(shareBtn).toBeEnabled();
    await shareBtn.click();

    // ThreadSharePanel shows fail-closed identity error and hides Copy link & AudiencePicker (fieldset + radio).
    await expect(page.getByRole('alert')).toBeVisible({ timeout: 10000 });
    await expect(page.getByRole('button', { name: /Copy link|复制链接/i })).toHaveCount(0);
    await expect(page.getByRole('group', { name: /Who can open it|谁可以打开/i })).toHaveCount(0);
    await expect(page.getByRole('radio')).toHaveCount(0);
    await expect(page.getByRole('switch', { name: /Share conversation|分享对话/i })).toBeEnabled();
    await page.keyboard.press('Escape');

    // FilePanel: Publish is absent; Stop sharing opens unshareOnly dialog with no Publish/Update, Audience, or Copy link.
    await openReportAFromTree(page);
    const frame = page.frameLocator('iframe.bridged-frame-doc').last();
    await expect(frame.locator('h1')).toHaveText('Doc A', { timeout: 10000 });
    await expect(page.getByRole('button', { name: /^Publish$|^发布$/i })).toHaveCount(0);

    const stopShareBtn = page.getByRole('button', { name: /Stop sharing|停止分享/i }).first();
    await expect(stopShareBtn).toBeVisible({ timeout: 10000 });
    await stopShareBtn.click();

    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible({ timeout: 10000 });
    await expect(dialog.getByRole('button', { name: /^Publish$|^Update$|^发布$|^更新$/i })).toHaveCount(0);
    await expect(dialog.getByRole('button', { name: /^Copy$|^Copy link$|^复制$|^复制链接$/i })).toHaveCount(0);
    await expect(dialog.getByRole('group', { name: /Who can open it|谁可以打开/i })).toHaveCount(0);
    await expect(dialog.getByRole('radio')).toHaveCount(0);
    await expect(dialog.getByRole('button', { name: /Unshare|Stop sharing|停止分享/i })).toBeVisible();
  });
});
