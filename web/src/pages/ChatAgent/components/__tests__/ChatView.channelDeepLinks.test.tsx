import React from 'react';
import { render, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../viewers/InlineWidget', () => ({
  default: ({ title }: { title: string }) => <div data-testid="mock-inline-widget">{title}</div>,
}));

import {
  clearShowRequest,
  getShowLayersRequest,
} from '@/pages/MarketView/stores/layerViewStore';
import { chartKeyFor } from '@/pages/MarketView/utils/chartInstanceKey';
import {
  parseChartDeepLinkParam,
  useChannelDeepLinks,
} from '../chatView/useChannelDeepLinks';
import { MessageContentSegments } from '../messageList/MessageContentSegments';

function ChannelDeepLinksHost({
  workspaceId,
  onOpenChart,
}: {
  workspaceId: string;
  onOpenChart: (spec: { symbol: string; timeframe: string; workspaceId: string }) => void;
}) {
  const location = useLocation();
  useChannelDeepLinks({
    active: true,
    workspaceId,
    onOpenChart,
    messagesDeps: 1,
  });

  // Render real MessageContentSegments with the internal `widget-${artifactId}` segment key
  // produced by both live streaming (`mainEventHandlers.ts`) and history replay (`historyHandlers.ts`).
  return (
    <div>
      <span data-testid="loc-search">{location.search}</span>
      <MessageContentSegments
        segments={[{ type: 'html_widget', widgetId: 'widget-widget_abc123', order: 1 }]}
        reasoningProcesses={{}}
        toolCallProcesses={{}}
        subagentTasks={{}}
        htmlWidgetProcesses={{
          'widget-widget_abc123': {
            html: '<div>Interactive Widget</div>',
            title: 'Valuation Table',
          },
        }}
      />
    </div>
  );
}

describe('ChatView channel deep links (?chart=&layer= and ?widget=)', () => {
  beforeEach(() => {
    const req = getShowLayersRequest();
    if (req) clearShowRequest(req.seq);
  });

  it('parses ?chart=NVDA:1day&layer=layer-1 via useChannelDeepLinks, focuses layer, opens panel, and clears query params', async () => {
    expect(parseChartDeepLinkParam('NVDA:1day')).toEqual({ symbol: 'NVDA', timeframe: '1day' });
    expect(parseChartDeepLinkParam('AAPL')).toEqual({ symbol: 'AAPL', timeframe: '1day' });
    expect(parseChartDeepLinkParam('   ')).toBeNull();

    const onOpenChart = vi.fn();
    const { getByTestId } = render(
      <MemoryRouter initialEntries={['/chat/t/thread-1?chart=NVDA%3A1day&layer=layer-1&keep=1']}>
        <ChannelDeepLinksHost workspaceId="ws-1" onOpenChart={onOpenChart} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(onOpenChart).toHaveBeenCalledWith({
        symbol: 'NVDA',
        timeframe: '1day',
        workspaceId: 'ws-1',
      });
    });

    const req = getShowLayersRequest();
    expect(req).not.toBeNull();
    expect(req?.key).toBe(chartKeyFor('ws-1', 'NVDA', '1day'));
    expect(req?.layerIds).toEqual(['layer-1']);
    await waitFor(() => {
      expect(getByTestId('loc-search').textContent).toBe('?keep=1');
    });
  });

  it('locates [data-widget-id] for ?widget=widget_abc123, scrolls into view, highlights card, clears query param, and removes highlight after 3s', async () => {
    const scrollIntoView = vi.fn();
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    const onOpenChart = vi.fn();
    const setTimeoutSpy = vi.spyOn(window, 'setTimeout');
    const clearTimeoutSpy = vi.spyOn(window, 'clearTimeout');

    try {
      const { container, getByTestId } = render(
        <MemoryRouter initialEntries={['/chat/t/thread-1?widget=widget_abc123']}>
          <ChannelDeepLinksHost workspaceId="ws-1" onOpenChart={onOpenChart} />
        </MemoryRouter>,
      );

      await waitFor(() => {
        expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center' });
      });

      const el = container.querySelector('[data-widget-id="widget_abc123"]') as HTMLElement | null;
      expect(el).not.toBeNull();
      expect(el?.getAttribute('data-deep-link-highlighted')).toBe('true');
      expect(el?.style.outline).toContain('2px solid');
      expect(onOpenChart).not.toHaveBeenCalled();

      await waitFor(() => {
        expect(getByTestId('loc-search').textContent).toBe('');
      });

      // The 3000ms highlight restore timer MUST NOT have been cancelled by the URL cleanup navigation
      const highlightCallIndex = setTimeoutSpy.mock.calls.findIndex((c) => c[1] === 3000);
      expect(highlightCallIndex).toBeGreaterThanOrEqual(0);
      const timerId = setTimeoutSpy.mock.results[highlightCallIndex]?.value;
      const wasCancelledOnNav = clearTimeoutSpy.mock.calls.some((c) => c[0] === timerId);
      expect(wasCancelledOnNav).toBe(false);

      // Firing the timer callback clears the highlight attribute and restores inline styles
      const restoreCb = setTimeoutSpy.mock.calls[highlightCallIndex]?.[0];
      expect(typeof restoreCb).toBe('function');
      (restoreCb as () => void)();
      expect(el?.getAttribute('data-deep-link-highlighted')).toBeNull();
      expect(el?.style.outline).toBe('');
    } finally {
      setTimeoutSpy.mockRestore();
      clearTimeoutSpy.mockRestore();
    }
  });
});
