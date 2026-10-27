/**
 * The activity live zone must size to its rows after a fold is interrupted.
 *
 * Three tool rows complete together, fold into the accordion, and a fourth
 * call lands while the live zone is still collapsing. The zone must end up
 * as tall as the one row it now holds, not the three it held before.
 */
import { configureSSE, resetMockServer, mockAPI, test, expect } from './fixtures.js';
import { sseEvents, defaultResponses } from './helpers/mockResponses.js';
import { TH, chatViewOverrides } from './helpers/chatScenario.js';
import { MIN_LIVE_EXPOSURE_MS } from '../src/pages/ChatAgent/components/messageList/liveZoneTiming.ts';

// A completed row stays live for MIN_LIVE_EXPOSURE_MS after its call was
// created, then folds; the zone's collapse runs about 180 ms more, so a call
// landing 150 ms past the fold lands inside that collapse. The clock starts
// at the first event, and the four events before the delay each cost a gap,
// so the delay is measured from the first event, not from the last result.
// The tool must not be an inline-artifact tool (those skip the exposure
// window).
const EVENT_GAP_MS = 30;
const LAND_MID_COLLAPSE_MS = MIN_LIVE_EXPOSURE_MS + 150 - 4 * EVENT_GAP_MS;

test.describe('activity live zone', () => {
  test.beforeEach(async () => {
    await resetMockServer();
  });

  test('sizes to its rows after a call lands mid-fold', async ({ page }) => {
    await mockAPI(page, chatViewOverrides());
    await configureSSE({
      method: 'GET',
      path: `/api/v1/threads/${TH}/messages/replay`,
      events: [sseEvents.replayDone()],
      delay: 10,
    });

    const batch = ['toolu_a1', 'toolu_a2', 'toolu_a3'];
    await configureSSE({
      method: 'POST',
      path: `/api/v1/threads/${TH}/messages`,
      events: [
        sseEvents.toolCalls(batch.map((id, i) => ({ name: 'bash', args: { command: `echo ${i}` }, id }))),
        sseEvents.finishToolCalls(),
        sseEvents.toolCallResult('toolu_a1', '0'),
        sseEvents.toolCallResult('toolu_a2', '1'),
        { ...sseEvents.toolCallResult('toolu_a3', '2'), delayAfter: LAND_MID_COLLAPSE_MS },
        sseEvents.toolCalls([{ name: 'bash', args: { command: 'echo 3' }, id: 'toolu_b' }]),
        // Hold the fourth call in progress so it stays a live row while measured.
        { ...sseEvents.finishToolCalls(), delayAfter: 3000 },
        sseEvents.toolCallResult('toolu_b', '3'),
        sseEvents.messageChunk('done'),
        sseEvents.finishStop(),
        sseEvents.creditUsage(),
      ],
      delay: EVENT_GAP_MS,
    });

    await page.goto(`/chat/t/${TH}`);
    await page.waitForSelector('textarea', { timeout: 10000 });
    await page.locator('textarea').fill('run four commands');
    await page.locator('button[aria-label="Send message"]').click();

    // The precondition, read in one pass so it can actually fail: the three
    // rows must already be folded behind the accordion summary when the fourth
    // call goes live. If the fourth arrived first there is no interrupted fold
    // and the height below would be measured on a case that never regressed.
    const atFold = await page.waitForFunction(() => {
      if (!document.querySelector('[id^="activity-summary-"]')) return null;
      return { active: document.querySelectorAll('[data-testid="activity-live-zone"] .titem.running').length };
    }, null, { timeout: 15000 }).then((h) => h.jsonValue());
    expect(atFold.active, 'the fourth call went live before the first three folded').toBe(0);

    const zone = page.getByTestId('activity-live-zone');
    await expect(zone.locator('.titem.running')).toHaveCount(1, { timeout: 15000 });
    // Let the zone's own animations settle before measuring.
    await page.waitForTimeout(1000);

    const { zoneHeight, rowsHeight } = await zone.evaluate((el) => ({
      zoneHeight: el.getBoundingClientRect().height,
      rowsHeight: Array.from(el.children).reduce((sum, c) => sum + c.getBoundingClientRect().height, 0),
    }));
    expect(rowsHeight).toBeGreaterThan(0);
    expect(zoneHeight).toBeLessThanOrEqual(rowsHeight + 2);
  });

  test('keeps verbose reasoning open when the completed activity accordion is expanded', async ({ page }) => {
    const prefs = defaultResponses['GET /users/me/preferences'];
    await mockAPI(page, {
      ...chatViewOverrides(),
      'GET /users/me/preferences': { ...prefs, other_preference: { ...prefs.other_preference, turn_display: 'verbose' } },
    });
    await configureSSE({ method: 'GET', path: `/api/v1/threads/${TH}/messages/replay`, events: [sseEvents.replayDone()], delay: 10 });
    await configureSSE({ method: 'POST', path: `/api/v1/threads/${TH}/messages`, events: [
      sseEvents.toolCalls([{ name: 'bash', args: { command: 'echo ready' }, id: 'toolu_ready' }]),
      sseEvents.finishToolCalls(),
      { ...sseEvents.toolCallResult('toolu_ready', 'ready'), delayAfter: MIN_LIVE_EXPOSURE_MS + 300 },
      sseEvents.messageChunk('start', 'reasoning_signal'),
      { ...sseEvents.messageChunk('**Comparing inputs**\n\nChecking the supplied evidence.', 'reasoning'), delayAfter: 3500 },
      sseEvents.messageChunk('complete', 'reasoning_signal'),
      sseEvents.messageChunk('Comparison complete.'),
      sseEvents.finishStop(),
      sseEvents.creditUsage(),
    ], delay: 30 });
    await page.goto(`/chat/t/${TH}`);
    await page.locator('textarea').fill('Compare the inputs');
    await page.getByRole('button', { name: 'Send message', exact: true }).click();
    const thought = page.getByRole('button', { name: 'Comparing inputs', exact: true });
    await expect(thought).toHaveAttribute('aria-expanded', 'true');
    const body = page.locator('[data-activity-state="live"] .titem-reasoning-card');
    await expect(body).toBeVisible();
    await page.locator('[id^="activity-summary-"]').click();
    await expect(thought).toHaveAttribute('aria-expanded', 'true');
    await expect(body).toBeVisible();
    await expect(page.locator('[data-turn-fold]')).toHaveAttribute('data-turn-fold', 'collapsed', { timeout: 15000 });
    await page.locator('[data-turn-fold] button').click();
    await expect(thought).toHaveAttribute('aria-expanded', 'false');
  });

  test('keeps progress visible while the first paragraph is withheld', async ({ page }) => {
    const prefs = defaultResponses['GET /users/me/preferences'];
    await mockAPI(page, {
      ...chatViewOverrides(),
      'GET /users/me/preferences': { ...prefs, other_preference: { ...prefs.other_preference, response_streaming_mode: 'paragraph' } },
    });
    await configureSSE({ method: 'GET', path: `/api/v1/threads/${TH}/messages/replay`, events: [sseEvents.replayDone()], delay: 10 });
    await configureSSE({ method: 'POST', path: `/api/v1/threads/${TH}/messages`, events: [
      ...Array.from({ length: 40 }, () => sseEvents.messageChunk('Still writing. ')),
      sseEvents.finishStop(),
      sseEvents.creditUsage(),
    ], delay: 100 });
    await page.goto(`/chat/t/${TH}`);
    await page.locator('textarea').fill('Write one paragraph');
    await page.getByRole('button', { name: 'Send message', exact: true }).click();
    await expect(page.getByTestId('streaming-indicator')).toBeAttached();
    // More than the 700ms arrival window, while chunks continue every 100ms.
    await page.waitForTimeout(1000);
    await expect(page.locator('[data-message-role="assistant"] .markdown-content')).toHaveCount(0);
    await expect(page.getByTestId('streaming-indicator')).toHaveCSS('opacity', '1', { timeout: 500 });
    await expect(page.getByTestId('streaming-indicator')).toHaveCount(0, { timeout: 10000 });
    await expect(page.locator('[data-message-role="assistant"]')).toContainText('Still writing. '.repeat(40).trim());
  });

  for (const turnDisplay of ['lean', 'verbose']) {
    test(`respects ${turnDisplay} reasoning in a live subagent transcript`, async ({ page }) => {
      const prefs = defaultResponses['GET /users/me/preferences'];
      await mockAPI(page, {
        ...chatViewOverrides(),
        'GET /users/me/preferences': { ...prefs, other_preference: { ...prefs.other_preference, turn_display: turnDisplay } },
      });
      await configureSSE({ method: 'GET', path: `/api/v1/threads/${TH}/messages/replay`, events: [sseEvents.replayDone()], delay: 10 });
      const taskChunk = (content, type) => {
        const event = sseEvents.messageChunk(content, type);
        return { ...event, data: { ...event.data, agent: 'task:reasoning-check' } };
      };
      await configureSSE({ method: 'POST', path: `/api/v1/threads/${TH}/messages`, events: [
        sseEvents.toolCalls([{ name: 'Task', args: { description: 'Review the evidence', prompt: 'Review the evidence' }, id: 'toolu_task' }]),
        sseEvents.finishToolCalls(),
        { event: 'artifact', data: { thread_id: TH, artifact_type: 'task', tool_call_id: 'toolu_task', payload: { task_id: 'reasoning-check', action: 'spawned', description: 'Review the evidence', type: 'research' } } },
        taskChunk('start', 'reasoning_signal'),
        taskChunk('**Inspecting evidence**\n\nThe subagent is comparing source documents.', 'reasoning'),
        { ...sseEvents.messageChunk('Review underway.'), delayAfter: 8000 },
        taskChunk('complete', 'reasoning_signal'),
        sseEvents.messageChunk('Review complete.'),
        sseEvents.finishStop(),
      ], delay: 50 });
      await page.goto(`/chat/t/${TH}`);
      await page.locator('textarea').fill('Review the evidence');
      await page.getByRole('button', { name: 'Send message', exact: true }).click();
      await expect(page.getByText('Review underway.', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: 'Open subagent: Review the evidence', exact: true }).click();
      const thought = page.getByRole('button', { name: 'Inspecting evidence', exact: true });
      await expect(thought).toHaveAttribute('aria-expanded', String(turnDisplay === 'verbose'));
      const body = page.getByText('The subagent is comparing source documents.', { exact: true });
      if (turnDisplay === 'lean') {
        await expect(body).toBeHidden();
        await thought.click();
      }
      await expect(body).toBeVisible();
    });
  }

  test('folds a turn with a retained widget without a final downward step', async ({ page }) => {
    await mockAPI(page, chatViewOverrides());
    await configureSSE({ method: 'GET', path: `/api/v1/threads/${TH}/messages/replay`, events: [sseEvents.replayDone()], delay: 10 });
    await configureSSE({ method: 'POST', path: `/api/v1/threads/${TH}/messages`, events: [
      sseEvents.toolCalls([{ name: 'show_widget', args: { title: 'Sample table' }, id: 'toolu_widget' }]),
      sseEvents.finishToolCalls(),
      { event: 'artifact', data: { thread_id: TH, artifact_type: 'html_widget', artifact_id: 'sample-widget', payload: { title: 'Sample table', html: '<div style="height:180px">Widget result</div>' } } },
      sseEvents.toolCallResult('toolu_widget', 'Displayed widget'),
      sseEvents.messageChunk('Here is the result.'),
      sseEvents.finishStop(),
      sseEvents.creditUsage(),
    ], delay: 30 });
    await page.goto(`/chat/t/${TH}`);
    await page.locator('textarea').fill('Show a sample table');
    await page.getByRole('button', { name: 'Send message', exact: true }).click();
    const fold = page.locator('[data-turn-fold="collapsed"] button');
    await expect(fold).toBeVisible();
    const frame = page.locator('.inline-widget-container iframe');
    await expect.poll(() => frame.evaluate(el => el.getBoundingClientRect().height)).toBeGreaterThan(170);
    await fold.click();
    await page.waitForTimeout(1000);
    await page.evaluate(() => {
      const widget = document.querySelector('.inline-widget-container');
      const iframe = widget.querySelector('iframe');
      const row = document.querySelector('[data-turn-fold]');
      window.widgetFoldProbe = { iframe, samples: [], done: false };
      const start = performance.now();
      const sample = () => {
        // Relative to the toggle, so scroll anchoring cannot mask a layout step.
        window.widgetFoldProbe.samples.push(widget.getBoundingClientRect().top - row.getBoundingClientRect().top);
        if (performance.now() - start < 1100) requestAnimationFrame(sample);
        else window.widgetFoldProbe.done = true;
      };
      requestAnimationFrame(sample);
    });
    await page.locator('[data-turn-fold="expanded"] button').click();
    await page.waitForFunction(() => window.widgetFoldProbe.done);
    const result = await page.evaluate(() => {
      const { samples, iframe } = window.widgetFoldProbe;
      return {
        travel: samples[0] - samples.at(-1),
        maxDownwardStep: Math.max(...samples.slice(1).map((y, i) => y - samples[i])),
        sameIframe: iframe === document.querySelector('.inline-widget-container iframe'),
      };
    });
    expect(result.travel).toBeGreaterThan(10);
    expect(result.maxDownwardStep).toBeLessThanOrEqual(0.5);
    expect(result.sameIframe).toBe(true);
    await expect(fold).toBeVisible();
    await expect(frame).toBeVisible();
  });

  test('tracks a reasoning disclosure without a second trailing height animation', async ({ page }) => {
    await mockAPI(page, chatViewOverrides());
    await configureSSE({ method: 'GET', path: `/api/v1/threads/${TH}/messages/replay`, events: [sseEvents.replayDone()], delay: 10 });
    await configureSSE({ method: 'POST', path: `/api/v1/threads/${TH}/messages`, events: [
      sseEvents.messageChunk('start', 'reasoning_signal'),
      sseEvents.messageChunk('**Checking evidence**\n\n' + 'Compare each source and verify the result.\n\n'.repeat(12), 'reasoning'),
      sseEvents.messageChunk('complete', 'reasoning_signal'),
      sseEvents.messageChunk('The comparison is complete.'),
      sseEvents.finishStop(),
      sseEvents.creditUsage(),
    ], delay: 30 });
    await page.goto(`/chat/t/${TH}`);
    await page.locator('textarea').fill('Compare sources');
    await page.getByRole('button', { name: 'Send message', exact: true }).click();
    await page.locator('[data-turn-fold="collapsed"] button').click();
    const thought = page.getByRole('button', { name: 'Checking evidence', exact: true });
    await thought.click();
    await page.waitForTimeout(1100);
    await thought.evaluate(button => {
      const row = button.closest('[data-activity-state]').parentElement.parentElement.parentElement;
      const inner = row.firstElementChild;
      const answer = [...document.querySelectorAll('p')].find(p => p.textContent === 'The comparison is complete.');
      window.reasoningFoldProbe = { done: false, lag: [], positions: [], initialHeight: inner.getBoundingClientRect().height };
      const start = performance.now();
      const sample = () => {
        window.reasoningFoldProbe.lag.push(row.getBoundingClientRect().height - inner.getBoundingClientRect().height);
        const body = row.querySelector('.titem-reasoning-card')?.parentElement;
        window.reasoningFoldProbe.positions.push({
          bodyHeight: body?.getBoundingClientRect().height ?? 0,
          answerY: answer.getBoundingClientRect().top - button.getBoundingClientRect().top,
        });
        if (performance.now() - start < 1200) requestAnimationFrame(sample);
        else window.reasoningFoldProbe.done = true;
      };
      requestAnimationFrame(sample);
    });
    await thought.click();
    await page.waitForFunction(() => window.reasoningFoldProbe.done);
    const result = await page.evaluate(() => window.reasoningFoldProbe);
    expect(result.initialHeight).toBeGreaterThan(200);
    // The body owns its spacing: no trailing spring or gap removal after collapse.
    expect(Math.max(...result.lag)).toBeLessThan(0.5);
    const tail = result.positions.filter(sample => sample.bodyHeight < 0.1).map(sample => sample.answerY);
    expect(tail.length).toBeGreaterThan(5);
    expect(Math.max(...tail) - Math.min(...tail)).toBeLessThan(0.5);
    await expect(thought).toHaveAttribute('aria-expanded', 'false');
  });


  for (const width of [1280, 390]) {
    test(`spaces live cards, reasoning, batched fetches and the waiting indicator at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1400 });
      await mockAPI(page, chatViewOverrides());
      await configureSSE({ method: 'GET', path: `/api/v1/threads/${TH}/messages/replay`, events: [sseEvents.replayDone()], delay: 10 });
      const quote = { type: 'quote', quotes: [{ symbol: 'AMD', name: 'Advanced Micro Devices', price: 615.52 }] };
      const call = (id) => sseEvents.toolCalls([{ name: 'get_quote', args: { symbol: 'AMD' }, id }]);
      await configureSSE({ method: 'POST', path: `/api/v1/threads/${TH}/messages`, events: [
        call('quote1'), sseEvents.finishToolCalls(),
        { ...sseEvents.toolCallResult('quote1', 'Quote', quote), delayAfter: 2500 },
        call('missing1'), sseEvents.finishToolCalls(),
        sseEvents.toolCallResult('missing1', 'No card', { type: 'unsupported' }),
        sseEvents.messageChunk('start', 'reasoning_signal'),
        sseEvents.messageChunk('**Researching AMD valuation drivers**\n\nChecking the evidence.', 'reasoning'),
        sseEvents.messageChunk('complete', 'reasoning_signal'),
        call('missing2'), sseEvents.finishToolCalls(),
        sseEvents.toolCallResult('missing2', 'No card', { type: 'unsupported' }),
        call('quote2'), sseEvents.finishToolCalls(),
        sseEvents.toolCallResult('quote2', 'Quote', quote),
        sseEvents.toolCalls(['one', 'two', 'three'].map(id => ({ name: 'WebFetch', args: { url: `https://${id}.example.com` }, id }))),
        { ...sseEvents.finishToolCalls(), delayAfter: 8000 },
        ...['one', 'two', 'three'].map(id => sseEvents.toolCallResult(id, 'Fetched')),
        sseEvents.finishStop(), sseEvents.creditUsage(),
      ], delay: 50 });
      await page.goto(`/chat/t/${TH}`);
      await page.locator('textarea').fill('Check AMD');
      await page.getByRole('button', { name: 'Send message', exact: true }).click();
      const spinner = page.getByTestId('streaming-indicator');
      await expect(page.getByText('Advanced Micro Devices', { exact: true }).first()).toBeVisible();
      await expect(spinner).toHaveCSS('padding-top', '12px');
      await expect(spinner).toHaveAttribute('data-quiet', 'true');
      await page.waitForTimeout(250);
      await page.screenshot({ path: `../output/playwright/live-spinner-after-${width}.png` });
      const fetches = page.locator('.titem.running').filter({ hasText: 'Web Fetch' });
      await expect(fetches).toHaveCount(3);
      await page.waitForTimeout(900);
      const rects = await fetches.evaluateAll(es => es.map(e => ({ y: e.getBoundingClientRect().top, h: e.getBoundingClientRect().height })));
      expect(Math.abs((rects[1].y - rects[0].y) - (rects[2].y - rects[1].y))).toBeLessThan(0.5);
      expect(Math.max(...rects.map(r => r.h)) - Math.min(...rects.map(r => r.h))).toBeLessThan(0.5);
      const reasoning = page.locator('.titem').filter({ hasText: 'Researching AMD valuation drivers' });
      const gaps = await reasoning.evaluate(el => {
        let block = el;
        while (block.parentElement && !block.parentElement.classList.contains('sibling-space-y-3')) block = block.parentElement;
        return { before: block.getBoundingClientRect().top - block.previousElementSibling.getBoundingClientRect().bottom,
          after: block.nextElementSibling.getBoundingClientRect().top - block.getBoundingClientRect().bottom };
      });
      // Subpixel layout: the same tolerance every other geometry check here uses.
      expect(Math.abs(gaps.before - 12)).toBeLessThan(0.5);
      expect(Math.abs(gaps.after - 12)).toBeLessThan(0.5);
      await page.screenshot({ path: `../output/playwright/live-batch-after-${width}.png` });
    });

  }

});
