/**
 * Mobile P0 (390x844 touch portrait) regression suite:
 * 1. Standalone auth page (/invite) vertical scrolling under shrunk visual viewport.
 * 2. Invalid invite back-to-login button navigates to APP_ENTRY_PATH (/) rather than /login.
 * 3. CreateWorkspaceModal keyboard avoidance on mobile so submit button stays visible.
 * 4. Full-screen mobile FilePanel ignores content-area horizontal drag-to-close,
 *    renders touch per-row menu button, and places FileContextMenu above the panel.
 * 5. InlineWidget overlay action bar is always visible on touch and opens an unscaled
 *    fullscreen iframe with effective CSS width >= 360px.
 */
import { configureSSE, resetMockServer, mockAPI, test, expect } from './fixtures.js';
import { sseEvents } from './helpers/mockResponses.js';
import { chatViewOverrides, replayNothing, openThread, WS, TH } from './helpers/chatScenario.js';

test.use({
  viewport: { width: 390, height: 844 },
  isMobile: true,
  hasTouch: true,
});

const WIDE_CSV = [
  'col_1,col_2,col_3,col_4,col_5,col_6,col_7,col_8,col_9,col_10',
  'alpha,beta,gamma,delta,epsilon,zeta,eta,theta,iota,kappa',
  '100,200,300,400,500,600,700,800,900,1000',
].join('\n');

/**
 * Drive the real trackVisualViewport() listener through window.visualViewport
 * getters + resize event so subsequent focusin events (from .fill() / .click())
 * keep the simulated keyboard geometry instead of resetting CSS variables back
 * to the 844px headless viewport height.
 */
async function simulateVisualViewport(page, { height, offsetTop = 0 }) {
  await page.evaluate(({ h, top }) => {
    const vv = window.visualViewport;
    if (!vv) throw new Error('window.visualViewport is unavailable');
    Object.defineProperty(vv, 'height', { configurable: true, get: () => h });
    Object.defineProperty(vv, 'offsetTop', { configurable: true, get: () => top });
    vv.dispatchEvent(new Event('resize'));
  }, { h: height, top: offsetTop });
}

test.describe('mobile P0 regressions (390x844)', () => {
  test.beforeEach(async () => {
    await resetMockServer();
  });

  test('invite auth page scrolls vertically under software keyboard height and submits', async ({ page }) => {
    let redeemPayload = null;
    await mockAPI(page, {
      'POST /dalton/invites/check': { valid: true, reason: null },
      'POST /dalton/invites/redeem': async (route) => {
        redeemPayload = route.request().postDataJSON();
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ email: 'mobile_user@example.com' }),
        });
      },
    });

    await page.goto('/invite#tok-valid');
    const usernameInput = page.locator('#invite-username');
    const passwordInput = page.locator('#invite-password');
    const confirmInput = page.locator('#invite-confirm-password');
    const submitBtn = page.locator('.login-page__submit');
    await expect(confirmInput).toBeVisible();
    await expect(submitBtn).toBeVisible();

    // Simulate software keyboard shrinking visual viewport to 420px via real visualViewport resize.
    await simulateVisualViewport(page, { height: 420, offsetTop: 0 });

    const scrollMetrics = await page.locator('.login-page').evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        overflowY: cs.overflowY,
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
      };
    });
    expect(scrollMetrics.overflowY).toBe('auto');
    expect(scrollMetrics.scrollHeight).toBeGreaterThan(scrollMetrics.clientHeight);

    await usernameInput.fill('mobile_user');
    await passwordInput.fill('StrongPass123!');

    await confirmInput.scrollIntoViewIfNeeded();
    const confirmBox = await confirmInput.boundingBox();
    expect(confirmBox).not.toBeNull();
    expect(confirmBox.y).toBeGreaterThanOrEqual(0);
    expect(confirmBox.y + confirmBox.height).toBeLessThanOrEqual(422);
    await confirmInput.fill('StrongPass123!');

    await submitBtn.scrollIntoViewIfNeeded();
    const submitBox = await submitBtn.boundingBox();
    expect(submitBox).not.toBeNull();
    expect(submitBox.y).toBeGreaterThanOrEqual(0);
    expect(submitBox.y + submitBox.height).toBeLessThanOrEqual(422);

    await submitBtn.click();
    await expect.poll(() => redeemPayload).toEqual({
      token: 'tok-valid',
      username: 'mobile_user',
      password: 'StrongPass123!',
    });
  });

  test('invalid invite back button redirects to exact APP_ENTRY_PATH target rather than /login', async ({ page }) => {
    await mockAPI(page, {
      'POST /dalton/invites/check': { valid: false, reason: 'expired' },
    });

    await page.goto('/invite#tok-expired');
    await expect(page.getByRole('alert')).toBeVisible();
    const backBtn = page.locator('.login-page__back');
    await expect(backBtn).toBeVisible();
    await backBtn.click();

    // In OSS E2E mode APP_ENTRY_PATH is '/', whose authenticated RootRedirect lands on '/dashboard'.
    await expect.poll(() => new URL(page.url()).pathname).toBe('/dashboard');
  });

  test('CreateWorkspaceModal lifts above software keyboard on mobile with sticky close button and working submit', async ({ page }) => {
    let createPayload = null;
    await mockAPI(page, {
      'GET /computers': { computers: [] },
      'GET /dalton/coverage-units': { enabled: false, units: [] },
      'POST /workspaces': async (route) => {
        createPayload = route.request().postDataJSON();
        await route.fulfill({
          status: 201,
          contentType: 'application/json',
          body: JSON.stringify({
            workspace_id: 'ws-mobile-created',
            name: createPayload?.name ?? 'Mobile Research',
            description: createPayload?.description ?? '',
            status: 'running',
            is_home: false,
            config: {},
          }),
        });
      },
    });

    await page.goto('/chat');
    // Authenticated Dalton pages default to zh-CN when users.locale is unset; match both locales.
    const newWsBtn = page.getByRole('button', { name: /^(New workspace|新建工作区)$/ }).first();
    await expect(newWsBtn).toBeVisible();
    await newWsBtn.click();

    const modal = page.locator('.cwm-modal');
    await expect(modal).toBeVisible();
    await expect(modal).toHaveAttribute('data-sheet', '');
    await expect(modal).toHaveAttribute('role', 'dialog');
    await expect(modal).toHaveAttribute('aria-modal', 'true');
    const labelledBy = await modal.getAttribute('aria-labelledby');
    expect(labelledBy).toBeTruthy();

    // Mobile should move focus into a non-input control inside the dialog instead of auto-focusing the name input.
    const focusInfo = await page.evaluate(() => {
      const active = document.activeElement;
      return {
        tag: active?.tagName ?? '',
        inModal: Boolean(active?.closest('.cwm-modal')),
      };
    });
    expect(focusInfo.inModal).toBe(true);
    expect(focusInfo.tag).not.toBe('INPUT');

    // Simulate software keyboard with panned visual viewport (height 460px, offsetTop 110px -> visible [110, 570], bottomOffset 274px) via real visualViewport resize.
    await simulateVisualViewport(page, { height: 460, offsetTop: 110 });

    const modalBox = await modal.boundingBox();
    expect(modalBox).not.toBeNull();
    expect(modalBox.y).toBeGreaterThanOrEqual(109);
    expect(modalBox.y + modalBox.height).toBeLessThanOrEqual(572);

    const nameInput = modal.locator('input[type="text"]').first();
    await nameInput.fill('Mobile Research');

    const createBtn = page.locator('.cwm-btn-create');
    await createBtn.scrollIntoViewIfNeeded();
    const btnBox = await createBtn.boundingBox();
    expect(btnBox).not.toBeNull();
    expect(btnBox.y).toBeGreaterThanOrEqual(109);
    expect(btnBox.y + btnBox.height).toBeLessThanOrEqual(572);

    // Sticky header keeps the close button inside the visual viewport even after scrolling down to submit.
    const closeBtn = modal.locator('.cwm-close-btn');
    const closeBox = await closeBtn.boundingBox();
    expect(closeBox).not.toBeNull();
    expect(closeBox.y).toBeGreaterThanOrEqual(109);
    expect(closeBox.y + closeBox.height).toBeLessThanOrEqual(572);

    await createBtn.click();
    await expect.poll(() => createPayload).toEqual(expect.objectContaining({
      name: 'Mobile Research',
    }));
  });

  test('mobile FilePanel ignores rightward swipe in content area and opens FileContextMenu above overlay via row menu button', async ({ page }) => {
    await mockAPI(page, {
      ...chatViewOverrides(),
      [`GET /workspaces/${WS}/files`]: { files: ['wide.csv', 'reports/notes.md'] },
      [`GET /workspaces/${WS}/files/read`]: (route) => {
        const p = new URL(route.request().url()).searchParams.get('path');
        const isCsv = p === 'wide.csv';
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            workspace_id: WS,
            path: p,
            content: isCsv ? WIDE_CSV : '# Notes\n\nSome content.',
            mime: isCsv ? 'text/csv' : 'text/markdown',
            truncated: false,
          }),
        });
      },
    });
    await replayNothing();

    await page.goto(`/chat/t/${TH}`);
    await page.waitForSelector('textarea', { timeout: 10000 });
    await page.locator('button[data-panel-toggle="header"]').click();

    const overlay = page.locator('.mobile-panel-overlay');
    await expect(overlay).toBeVisible();

    const initialWideRow = page.locator('[data-row-kind="file"][data-row-path="wide.csv"]');
    await expect(initialWideRow).toBeVisible();
    await initialWideRow.getByText('wide.csv').click();

    const content = page.locator('.file-panel-content');
    await expect(content).toContainText('alpha', { timeout: 10000 });

    // Simulate a 210px rightward horizontal touch + pointer drag across the content area.
    await content.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      const y = rect.top + Math.min(200, rect.height / 2);
      const makeTouch = (x) => new Touch({
        identifier: 1,
        target: el,
        clientX: x,
        clientY: y,
        pageX: x,
        pageY: y,
      });
      const pointerOpts = (x) => ({
        bubbles: true,
        cancelable: true,
        pointerId: 1,
        pointerType: 'touch',
        isPrimary: true,
        clientX: x,
        clientY: y,
      });
      const start = makeTouch(60);
      el.dispatchEvent(new PointerEvent('pointerdown', pointerOpts(60)));
      el.dispatchEvent(new TouchEvent('touchstart', { bubbles: true, cancelable: true, touches: [start], targetTouches: [start], changedTouches: [start] }));
      for (const x of [110, 170, 230, 270]) {
        const move = makeTouch(x);
        el.dispatchEvent(new PointerEvent('pointermove', pointerOpts(x)));
        el.dispatchEvent(new TouchEvent('touchmove', { bubbles: true, cancelable: true, touches: [move], targetTouches: [move], changedTouches: [move] }));
      }
      const end = makeTouch(270);
      el.dispatchEvent(new PointerEvent('pointerup', pointerOpts(270)));
      el.dispatchEvent(new TouchEvent('touchend', { bubbles: true, cancelable: true, touches: [], targetTouches: [], changedTouches: [end] }));
    });
    await page.waitForTimeout(200);
    await expect(overlay).toBeVisible();

    // Open the tree overlay and tap the accessible sibling ⋯ menu button on both file and directory rows.
    const stripRight = page.locator('.file-panel-strip-right');
    await stripRight.getByRole('button', { name: /^(Toggle file tree|显示\/隐藏文件树)$/ }).click();
    const rowMenuBtn = page.getByRole('button', { name: /^(More actions|更多操作): wide\.csv$/ });
    await expect(rowMenuBtn).toBeVisible();
    await expect(rowMenuBtn).toHaveAttribute('aria-haspopup', 'menu');
    const nestedInTreeItem = await rowMenuBtn.evaluate((el) => Boolean(el.closest('[role="treeitem"]')));
    expect(nestedInTreeItem).toBe(false);
    await rowMenuBtn.click();

    const contextMenu = page.locator('.file-panel-context-menu');
    await expect(contextMenu).toBeVisible();
    await expect(contextMenu).toHaveAttribute('data-mobile-overlay-active', '');
    const menuId = await contextMenu.getAttribute('id');
    expect(menuId).toBeTruthy();
    await expect(rowMenuBtn).toHaveAttribute('aria-controls', menuId);

    // Verify the menu paints on top of .mobile-panel-overlay (elementFromPoint hits menu item).
    const firstItem = contextMenu.getByRole('menuitem').first();
    const itemBox = await firstItem.boundingBox();
    expect(itemBox).not.toBeNull();
    const hitInMenu = await page.evaluate(({ x, y }) => {
      const hit = document.elementFromPoint(x, y);
      return Boolean(hit?.closest('.file-panel-context-menu'));
    }, { x: itemBox.x + itemBox.width / 2, y: itemBox.y + itemBox.height / 2 });
    expect(hitInMenu).toBe(true);

    // Dismiss the file menu via Escape and verify focus returns to the opening ⋯ button.
    await page.keyboard.press('Escape');
    await expect(contextMenu).toHaveCount(0);
    await expect(rowMenuBtn).toBeFocused();

    const dirMenuBtn = page.getByRole('button', { name: /^(More actions|更多操作): reports\/?$/ });
    await expect(dirMenuBtn).toBeVisible();
    await expect(dirMenuBtn).toHaveAttribute('aria-haspopup', 'menu');
    const dirNestedInTreeItem = await dirMenuBtn.evaluate((el) => Boolean(el.closest('[role="treeitem"]')));
    expect(dirNestedInTreeItem).toBe(false);
    await dirMenuBtn.click();
    await expect(contextMenu).toBeVisible();
    await expect(contextMenu.getByRole('menuitem', { name: /^(Collapse|Expand|收起|展开)$/ })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dirMenuBtn).toBeFocused();

    // Close the FilePanel via the top close/back button.
    await stripRight.getByRole('button', { name: /^(Close|关闭)$/ }).click();
    await expect(overlay).toHaveCount(0);
  });

  test('InlineWidget action bar is always visible on touch and fullscreen iframe is unscaled with width >= 360px', async ({ page }) => {
    await openThread(page);
    await configureSSE({
      method: 'POST',
      path: `/api/v1/threads/${TH}/messages`,
      events: [
        sseEvents.toolCalls([{ name: 'show_widget', args: { title: 'Revenue Chart' }, id: 'toolu_widget' }]),
        sseEvents.finishToolCalls(),
        {
          event: 'artifact',
          data: {
            thread_id: TH,
            artifact_type: 'html_widget',
            artifact_id: 'mobile-chart-widget',
            payload: {
              title: 'Revenue Chart',
              html: '<div style="height:220px;width:100%">Revenue chart content</div>',
            },
          },
        },
        sseEvents.toolCallResult('toolu_widget', 'Displayed widget'),
        sseEvents.messageChunk('Here is the chart.'),
        sseEvents.finishStop(),
        sseEvents.creditUsage(),
      ],
      delay: 20,
    });

    await page.goto(`/chat/t/${TH}`);
    const composer = page.locator('textarea').first();
    await expect(composer).toBeVisible({ timeout: 10000 });
    await composer.fill('Show revenue chart');
    await page.getByRole('button', { name: /^(Send message|发送消息)$/ }).click();

    const widget = page.locator('.inline-widget-container');
    await expect(widget).toBeVisible({ timeout: 10000 });
    await widget.scrollIntoViewIfNeeded();

    const actionBar = widget.locator('.html-action-bar-overlay');
    await expect.poll(async () => actionBar.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { opacity: parseFloat(cs.opacity), pointerEvents: cs.pointerEvents };
    })).toEqual(expect.objectContaining({
      opacity: expect.any(Number),
      pointerEvents: 'auto',
    }));
    const barOpacity = await actionBar.evaluate((el) => parseFloat(getComputedStyle(el).opacity));
    expect(barOpacity).toBeGreaterThan(0);

    await widget.getByRole('button', { name: /^(Fullscreen|全屏)$/ }).click();

    const fullscreenFrame = page.locator('iframe.html-fullscreen-frame');
    await expect(fullscreenFrame).toBeVisible();
    await expect.poll(async () => fullscreenFrame.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      const rawZoom = getComputedStyle(el).zoom;
      const zoom = Number(rawZoom) || 1;
      return rect.width / zoom;
    })).toBeGreaterThanOrEqual(360);
    const frameMetrics = await fullscreenFrame.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      const rawZoom = getComputedStyle(el).zoom;
      const zoom = Number(rawZoom) || 1;
      return { width: rect.width, zoom, effectiveWidth: rect.width / zoom };
    });
    expect(frameMetrics.zoom).toBe(1);
    expect(frameMetrics.effectiveWidth).toBeGreaterThanOrEqual(360);
  });
});
