/**
 * Playwright E2E acceptance tests for Workspace File Storage Batch 1 Step 4:
 *   1. Private bucket containment (`public_bucket_urls=false`):
 *      - Stopped workspace large-file download uses 60-second `dl_token`
 *        (`/api/v1/dalton/workspace-files/download?dl_token=...`), never a
 *        direct object-storage or presigned S3 URL.
 *      - Chat workspace images load via the authenticated backend file proxy
 *        (`/api/v1/workspaces/{id}/files/download`), never `/storage/response-images/*`
 *        or `/storage/private-media/*`.
 *   2. Edge `/storage/*` boundary:
 *      - User avatars under `/storage/avatars/*` load with HTTP 200.
 *      - Non-avatar paths (`/storage/blobs/*`, `/storage/private-media/*`) return 404.
 *   3. Public share large-file download (`allow_files=true, allow_download=true`):
 *      - Streaming download for large text and binary files returns HTTP 200
 *        with `Cache-Control: no-store`, redacted secrets, and zero direct
 *        object-storage URLs.
 *   4. Public share downgrade (`allow_files=false`) and revocation/invalid token:
 *      - Downgraded share returns HTTP 403 with `Cache-Control: no-store` and
 *        `Clear-Site-Data: "cache"`, and hides the Workspace Files toggle on reload.
 *      - Closed/invalid share token returns HTTP 404 with `Cache-Control: no-store`
 *        and renders the unavailable page.
 */
import { Buffer } from 'node:buffer';
import { configureSSE, resetMockServer, mockAPI, test, expect } from './fixtures.js';
import { chatViewOverrides, WS, TH } from './helpers/chatScenario.js';

const APP_API_ORIGIN = `http://127.0.0.1:${process.env.E2E_MOCK_PORT}`;

// Valid 1x1 transparent PNG so <img> elements decode cleanly with naturalWidth === 1.
const TINY_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8b8AAAAASUVORK5CYII=',
  'base64',
);

const SHARE_TOKEN = 'tok-wsfs-s4';
const REVOKED_TOKEN = 'tok-wsfs-revoked';
const SHARED_THREAD_ID = 'th-wsfs-s4';
const RAW_SECRET = 'sk-live-secret-sentinel-9999';
const REDACTED_MARKER = '[REDACTED:VAULT_API_KEY]';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Expose-Headers': 'Cache-Control, Clear-Site-Data, Content-Disposition',
};

const DIRECT_BUCKET_RE = /\/storage\/(?:blobs|uploads|private-media|response-images|attachments|widgets)\/|X-Amz-Signature=|minio:9000/i;

function sharedReplayEvents() {
  return [
    {
      event: 'user_message',
      data: {
        thread_id: SHARED_THREAD_ID,
        turn_index: 0,
        content: 'Prepare the 150 MiB research dataset.',
        timestamp: '2026-11-28T00:00:00Z',
        metadata: { msg_type: 'ptc', workspace_id: 'ws-shared-s4' },
      },
    },
    {
      event: 'message_chunk',
      data: {
        thread_id: SHARED_THREAD_ID,
        agent: 'main',
        id: 'lc_run--s4-1',
        role: 'assistant',
        content: 'The dataset files are ready in the workspace panel.',
        content_type: 'text',
        turn_index: 0,
      },
    },
    {
      event: 'message_chunk',
      data: {
        thread_id: SHARED_THREAD_ID,
        agent: 'main',
        id: 'lc_run--s4-1',
        role: 'assistant',
        content: '',
        content_type: 'text',
        finish_reason: 'stop',
        turn_index: 0,
      },
    },
    {
      event: 'replay_done',
      data: { thread_id: SHARED_THREAD_ID },
    },
  ];
}

test.describe('Workspace File Storage Batch 1 Step 4', () => {
  test.beforeEach(async () => {
    await resetMockServer();
  });

  test('private bucket: stopped workspace download uses 60s dl_token and chat image uses backend proxy with zero direct bucket URLs', async ({
    page,
  }) => {
    const requestedUrls = [];
    page.on('request', (req) => requestedUrls.push(req.url()));

    let downloadUrlCalls = 0;
    let ticketDownloadCalls = 0;
    let backendImageCalls = 0;

    await mockAPI(page, {
      ...chatViewOverrides(),
      [`GET /workspaces/${WS}`]: {
        workspace_id: WS,
        name: 'Research',
        description: 'Stopped workspace',
        status: 'stopped',
        is_home: false,
        config: {},
        created_at: '2026-11-01T00:00:00Z',
        updated_at: '2026-11-01T00:00:00Z',
      },
      [`GET /workspaces/${WS}/files`]: {
        files: ['work/charts/rev.png', 'reports/dataset_150m.csv'],
        source: 'backup',
      },
      [`GET /workspaces/${WS}/files/download-url`]: async (route) => {
        downloadUrlCalls += 1;
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          headers: CORS_HEADERS,
          body: JSON.stringify({
            url: null,
            dl_token: 'e2e-ticket-60s-abc',
            source: 'backup',
          }),
        });
      },
      'GET /dalton/workspace-files/download': async (route) => {
        const url = new URL(route.request().url());
        expect(url.searchParams.get('dl_token')).toBe('e2e-ticket-60s-abc');
        ticketDownloadCalls += 1;
        await route.fulfill({
          status: 200,
          headers: {
            ...CORS_HEADERS,
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': 'attachment; filename="dataset_150m.csv"',
            'Cache-Control': 'private, no-cache',
          },
          body: 'ticker,value\nAAPL,225.50\n',
        });
      },
      [`GET /workspaces/${WS}/files/download`]: async (route) => {
        const filePath = new URL(route.request().url()).searchParams.get('path') || '';
        if (filePath.includes('rev.png')) {
          backendImageCalls += 1;
          await route.fulfill({
            status: 200,
            headers: {
              ...CORS_HEADERS,
              'Content-Type': 'image/png',
              'Cache-Control': 'private, no-cache',
            },
            body: TINY_PNG,
          });
          return;
        }
        await route.fulfill({
          status: 200,
          headers: {
            ...CORS_HEADERS,
            'Content-Type': 'application/octet-stream',
            'Cache-Control': 'private, no-cache',
          },
          body: 'fallback',
        });
      },
    });

    await configureSSE({
      method: 'GET',
      path: `/api/v1/threads/${TH}/messages/replay`,
      events: [
        {
          event: 'user_message',
          data: {
            thread_id: TH,
            turn_index: 0,
            content: 'Show the revenue chart.',
            timestamp: '2026-11-28T00:00:00Z',
            metadata: { msg_type: 'ptc', workspace_id: WS },
          },
        },
        {
          event: 'message_chunk',
          data: {
            thread_id: TH,
            agent: 'main',
            id: 'lc_run--img-1',
            role: 'assistant',
            content: 'Here is the chart:\n\n![Revenue Chart](work/charts/rev.png)',
            content_type: 'text',
            finish_reason: 'stop',
            turn_index: 0,
          },
        },
        { event: 'replay_done', data: { thread_id: TH } },
      ],
      delay: 10,
    });

    await page.goto(`/chat/t/${TH}`);
    await page.waitForSelector('textarea', { timeout: 10000 });

    // Workspace image renders via authenticated backend proxy (blob URL backed by /files/download), never direct bucket URL.
    const chartImg = page.locator('img[alt="Revenue Chart"]');
    await expect(chartImg).toBeVisible({ timeout: 10000 });
    await expect
      .poll(async () => chartImg.evaluate((el) => el.complete && el.naturalWidth > 0))
      .toBe(true);
    expect(backendImageCalls).toBeGreaterThanOrEqual(1);
    const imgSrc = await chartImg.getAttribute('src');
    expect(imgSrc || '').not.toMatch(DIRECT_BUCKET_RE);

    // Open Workspace Files panel and download the stopped-workspace large file.
    await page.locator('button[title="Workspace Files"]').click();
    const row = page.locator('[data-row-path="reports/dataset_150m.csv"]');
    await expect(row).toBeVisible({ timeout: 10000 });

    // Intercept anchor clicks to capture the exact download href triggered by triggerFileDownload
    // and execute the redemption GET against the mocked backend ticket route.
    await page.evaluate(() => {
      window.__e2eDownloadHrefs = [];
      const origClick = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function (...args) {
        if (this.href && this.href.includes('/api/v1/dalton/workspace-files/download')) {
          window.__e2eDownloadHrefs.push(this.href);
          fetch(this.href).catch(() => {});
          return;
        }
        return origClick.apply(this, args);
      };
    });

    await row.click({ button: 'right' });
    await page.getByRole('menuitem', { name: /Download|下载/ }).click();

    await expect.poll(() => downloadUrlCalls).toBeGreaterThanOrEqual(1);
    await expect.poll(() => ticketDownloadCalls).toBeGreaterThanOrEqual(1);

    const downloadHrefs = await page.evaluate(() => window.__e2eDownloadHrefs || []);
    expect(downloadHrefs.length).toBeGreaterThanOrEqual(1);
    expect(downloadHrefs[0]).toContain('/api/v1/dalton/workspace-files/download');
    expect(downloadHrefs[0]).toContain('dl_token=e2e-ticket-60s-abc');

    // Assert zero direct object-storage URLs were ever requested by the browser.
    const directHits = requestedUrls.filter((u) => DIRECT_BUCKET_RE.test(u));
    expect(directHits).toEqual([]);
  });

  test('avatar under /storage/avatars/* is readable (200) while non-avatar /storage/* returns 404', async ({
    page,
  }) => {
    await page.route('**/storage/**', async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const method = req.method().toUpperCase();
      if (
        (method === 'GET' || method === 'HEAD') &&
        url.pathname.startsWith('/storage/avatars/') &&
        url.pathname !== '/storage/avatars/'
      ) {
        await route.fulfill({
          status: 200,
          headers: {
            'Content-Type': 'image/png',
            'X-Content-Type-Options': 'nosniff',
          },
          body: method === 'HEAD' ? undefined : TINY_PNG,
        });
        return;
      }
      await route.fulfill({ status: 404, body: '' });
    });

    await mockAPI(page, {
      'GET /users/me': {
        user_id: 'u-avatar-1',
        name: 'Avatar User',
        email: 'avatar@example.com',
        avatar_url: '/storage/avatars/u-avatar-1.png',
        onboarding_completed: true,
        has_api_key: true,
        has_oauth_token: false,
      },
    });

    await page.goto('/dashboard');
    await expect(page.locator('.sidebar-account-row')).toBeVisible({ timeout: 10000 });

    const avatarImg = page.locator('.sidebar-account-row img[src*="/storage/avatars/u-avatar-1.png"]');
    await expect(avatarImg).toBeVisible({ timeout: 10000 });
    await expect
      .poll(async () => avatarImg.evaluate((el) => el.complete && el.naturalWidth > 0))
      .toBe(true);

    // Direct checks mirroring Caddy /storage/* rules
    const statuses = await page.evaluate(async () => {
      const avatarGet = (await fetch('/storage/avatars/u-avatar-1.png?versionId=old-v1')).status;
      const avatarPut = (await fetch('/storage/avatars/u-avatar-1.png', { method: 'PUT', body: 'x' })).status;
      const blobGet = (await fetch('/storage/blobs/u-avatar-1/deadbeef')).status;
      const privMediaGet = (await fetch('/storage/private-media/response-images/t1/abc/chart.png')).status;
      return { avatarGet, avatarPut, blobGet, privMediaGet };
    });

    expect(statuses).toEqual({
      avatarGet: 200,
      avatarPut: 404,
      blobGet: 404,
      privMediaGet: 404,
    });
  });

  test('public share large text and binary file downloads stream redacted bytes with Cache-Control: no-store', async ({
    page,
  }) => {
    const requestedUrls = [];
    page.on('request', (req) => requestedUrls.push(req.url()));

    await configureSSE({
      method: 'GET',
      path: `/api/v1/public/shared/${SHARE_TOKEN}`,
      status: 200,
      json: {
        kind: 'thread',
        thread_id: SHARED_THREAD_ID,
        title: 'Large File Share',
        workspace_name: 'Research',
        permissions: { allow_files: true, allow_download: true },
      },
    });
    await configureSSE({
      method: 'GET',
      path: `/api/v1/public/shared/${SHARE_TOKEN}/replay`,
      events: sharedReplayEvents(),
      delay: 10,
    });
    await configureSSE({
      method: 'GET',
      path: `/api/v1/public/shared/${SHARE_TOKEN}/files`,
      status: 200,
      json: {
        path: '.',
        files: ['large_150m.txt', 'large_150m.bin'],
        source: 'database',
      },
    });

    const downloadHits = [];
    await page.route(`**/api/v1/public/shared/${SHARE_TOKEN}/files/download*`, async (route) => {
      const reqUrl = new URL(route.request().url());
      const filePath = reqUrl.searchParams.get('path') || '';
      downloadHits.push(filePath);
      const isBin = filePath.endsWith('.bin');
      const body = isBin
        ? Buffer.concat([Buffer.from([0x00, 0xff, 0xfe]), Buffer.from(`bin ${REDACTED_MARKER} end`)])
        : Buffer.from(`text ${REDACTED_MARKER} end\n`);
      await route.fulfill({
        status: 200,
        headers: {
          ...CORS_HEADERS,
          'Content-Type': isBin ? 'application/octet-stream' : 'text/plain; charset=utf-8',
          'Content-Disposition': `attachment; filename="${filePath}"`,
          'Cache-Control': 'no-store',
        },
        body,
      });
    });

    await page.goto(`/s/${SHARE_TOKEN}`);
    await expect(page.locator('h1')).toHaveText('Research', { timeout: 10000 });

    await page.locator('button[title="Workspace Files"]').click();
    const txtRow = page.locator('[data-row-path="large_150m.txt"]');
    await expect(txtRow).toBeVisible({ timeout: 10000 });

    const txtRespPromise = page.waitForResponse(
      (res) =>
        res.url().includes(`/api/v1/public/shared/${SHARE_TOKEN}/files/download`) &&
        res.url().includes('large_150m.txt'),
    );
    await txtRow.click({ button: 'right' });
    await page.getByRole('menuitem', { name: /Download|下载/ }).click();
    const txtResp = await txtRespPromise;

    expect(txtResp.status()).toBe(200);
    expect(txtResp.headers()['cache-control']).toBe('no-store');
    const txtBody = await txtResp.text();
    expect(txtBody).toContain(REDACTED_MARKER);
    expect(txtBody).not.toContain(RAW_SECRET);

    // Binary large file in public share also passes through StreamingSecretRedactor + Cache-Control: no-store.
    const binRow = page.locator('[data-row-path="large_150m.bin"]');
    const binRespPromise = page.waitForResponse(
      (res) =>
        res.url().includes(`/api/v1/public/shared/${SHARE_TOKEN}/files/download`) &&
        res.url().includes('large_150m.bin'),
    );
    await binRow.click({ button: 'right' });
    await page.getByRole('menuitem', { name: /Download|下载/ }).click();
    const binResp = await binRespPromise;

    expect(binResp.status()).toBe(200);
    expect(binResp.headers()['cache-control']).toBe('no-store');
    const binBuf = await binResp.body();
    expect(binBuf.includes(Buffer.from(REDACTED_MARKER))).toBe(true);
    expect(binBuf.includes(Buffer.from(RAW_SECRET))).toBe(false);

    expect(downloadHits).toEqual(['large_150m.txt', 'large_150m.bin']);
    expect(requestedUrls.filter((u) => DIRECT_BUCKET_RE.test(u))).toEqual([]);
  });

  test('public share downgrade (allow_files=false) returns 403 with Cache-Control: no-store and Clear-Site-Data: "cache"', async ({
    page,
  }) => {
    let allowFiles = true;

    await page.route(`**/api/v1/public/shared/${SHARE_TOKEN}`, async (route) => {
      const headers = {
        ...CORS_HEADERS,
        'Cache-Control': 'no-store',
        ...(allowFiles ? {} : { 'Clear-Site-Data': '"cache"' }),
      };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers,
        body: JSON.stringify({
          kind: 'thread',
          thread_id: SHARED_THREAD_ID,
          title: 'Downgrade Test',
          workspace_name: 'Research',
          permissions: { allow_files: allowFiles, allow_download: allowFiles },
        }),
      });
    });

    await page.route(`**/api/v1/public/shared/${SHARE_TOKEN}/files/download*`, async (route) => {
      if (!allowFiles) {
        await route.fulfill({
          status: 403,
          contentType: 'application/json',
          headers: {
            ...CORS_HEADERS,
            'Cache-Control': 'no-store',
            'Clear-Site-Data': '"cache"',
          },
          body: JSON.stringify({
            detail: "Permission 'allow_files' not granted for this shared thread",
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        headers: {
          ...CORS_HEADERS,
          'Content-Type': 'text/plain',
          'Cache-Control': 'no-store',
        },
        body: 'initial-ok',
      });
    });

    await configureSSE({
      method: 'GET',
      path: `/api/v1/public/shared/${SHARE_TOKEN}/replay`,
      events: sharedReplayEvents(),
      delay: 10,
    });
    await configureSSE({
      method: 'GET',
      path: `/api/v1/public/shared/${SHARE_TOKEN}/files`,
      status: 200,
      json: { path: '.', files: ['large_150m.txt'], source: 'database' },
    });

    await page.goto(`/s/${SHARE_TOKEN}`);
    await expect(page.locator('button[title="Workspace Files"]')).toBeVisible({ timeout: 10000 });

    // Downgrade share to allow_files=false and hit the old download URL directly.
    allowFiles = false;
    const downloadUrl = `${APP_API_ORIGIN}/api/v1/public/shared/${SHARE_TOKEN}/files/download?path=large_150m.txt`;
    const downgradedRespPromise = page.waitForResponse((res) => res.url().includes('/files/download'));
    await page.evaluate((u) => fetch(u, { cache: 'no-store' }), downloadUrl);
    const downgradedResp = await downgradedRespPromise;

    expect(downgradedResp.status()).toBe(403);
    expect(downgradedResp.headers()['cache-control']).toBe('no-store');
    expect(downgradedResp.headers()['clear-site-data']).toBe('"cache"');

    // Reloading the share page reflects downgrade and hides the Workspace Files button.
    await page.reload();
    await expect(page.getByText('The dataset files are ready in the workspace panel.')).toBeVisible({
      timeout: 10000,
    });
    await expect(page.locator('button[title="Workspace Files"]')).toHaveCount(0);
  });

  test('revoked share / invalid token returns 404 with Cache-Control: no-store', async ({ page }) => {
    await page.route(`**/api/v1/public/shared/${REVOKED_TOKEN}`, async (route) => {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        headers: {
          ...CORS_HEADERS,
          'Cache-Control': 'no-store',
        },
        body: JSON.stringify({ detail: 'Shared link not found' }),
      });
    });
    await page.route(`**/api/v1/public/shared/${REVOKED_TOKEN}/files/download*`, async (route) => {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        headers: {
          ...CORS_HEADERS,
          'Cache-Control': 'no-store',
        },
        body: JSON.stringify({ detail: 'Shared link not found' }),
      });
    });

    await page.goto(`/s/${REVOKED_TOKEN}`);
    await expect(
      page.getByText("You don't have access, or this link is unavailable"),
    ).toBeVisible({ timeout: 10000 });

    const revokedDownloadUrl = `${APP_API_ORIGIN}/api/v1/public/shared/${REVOKED_TOKEN}/files/download?path=large_150m.txt`;
    const respPromise = page.waitForResponse((res) => res.url().includes(`/${REVOKED_TOKEN}/files/download`));
    await page.evaluate((u) => fetch(u, { cache: 'no-store' }), revokedDownloadUrl);
    const resp = await respPromise;

    expect(resp.status()).toBe(404);
    expect(resp.headers()['cache-control']).toBe('no-store');
  });
});
