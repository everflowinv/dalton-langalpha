import { useEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router';

import { requestShowLayers } from '@/pages/MarketView/stores/layerViewStore';
import { chartKeyFor } from '@/lib/bars/chartId';

export interface ChartDeepLinkTarget {
  symbol: string;
  timeframe: string;
  workspaceId: string;
}

/**
 * Parse a `?chart=<chartId>` query parameter (e.g. `NVDA:1day` or `NVDA`) into
 * `{ symbol, timeframe }`, or null when empty.
 */
export function parseChartDeepLinkParam(
  rawChart: string | null | undefined,
): { symbol: string; timeframe: string } | null {
  const cleaned = (rawChart ?? '').trim();
  if (!cleaned) return null;
  const colonIdx = cleaned.lastIndexOf(':');
  const symbol = (colonIdx > 0 ? cleaned.slice(0, colonIdx) : cleaned).trim();
  const timeframe = (colonIdx > 0 ? cleaned.slice(colonIdx + 1) : '1day').trim() || '1day';
  if (!symbol) return null;
  return { symbol, timeframe };
}

/**
 * Resolve `?chart=<chartId>&layer=<layerId>` and `?widget=<artifactId>` channel deep links
 * in `ChatView`:
 * - `?chart=&layer=` un-hides/focuses the requested layer via `requestShowLayers`, opens the
 *   right-hand chart panel via `onOpenChart`, and strips `chart`/`layer` from the URL.
 * - `?widget=` locates `[data-widget-id="<artifactId>"]` once rendered, scrolls it into view
 *   with a temporary accent highlight, and strips `widget` from the URL.
 */
export function useChannelDeepLinks({
  active,
  workspaceId,
  onOpenChart,
  messagesDeps,
}: {
  active: boolean;
  workspaceId: string | null | undefined;
  onOpenChart: (spec: ChartDeepLinkTarget) => void;
  messagesDeps: unknown;
}): void {
  const location = useLocation();
  const navigate = useNavigate();

  const consumedChartRef = useRef<string | null>(null);
  useEffect(() => {
    if (!active || !workspaceId || consumedChartRef.current === location.key) return;
    const params = new URLSearchParams(location.search);
    const parsed = parseChartDeepLinkParam(params.get('chart'));
    if (!parsed) return;
    const rawLayer = params.get('layer')?.trim() || '';

    consumedChartRef.current = location.key;
    if (rawLayer) {
      const key = chartKeyFor(workspaceId, parsed.symbol, parsed.timeframe);
      if (key) {
        requestShowLayers(key, [rawLayer]);
      }
    }
    onOpenChart({ symbol: parsed.symbol, timeframe: parsed.timeframe, workspaceId });
    params.delete('chart');
    params.delete('layer');
    const nextSearch = params.toString();
    navigate(
      { pathname: location.pathname, search: nextSearch ? `?${nextSearch}` : '' },
      { replace: true, state: location.state },
    );
  }, [
    active,
    workspaceId,
    location.key,
    location.search,
    location.pathname,
    location.state,
    onOpenChart,
    navigate,
  ]);

  const consumedWidgetRef = useRef<string | null>(null);
  const highlightCleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    return () => {
      highlightCleanupRef.current?.();
      highlightCleanupRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!active || consumedWidgetRef.current === location.key) return;
    const params = new URLSearchParams(location.search);
    const widgetId = params.get('widget')?.trim();
    if (!widgetId) return;
    const normalizedWidgetId = widgetId.startsWith('widget-')
      ? widgetId.slice('widget-'.length)
      : widgetId;
    const escapeAttr = (val: string): string =>
      typeof CSS !== 'undefined' && typeof CSS.escape === 'function'
        ? CSS.escape(val)
        : val.replace(/["\\]/g, '\\$&');
    const targetEl =
      document.querySelector(`[data-widget-id="${escapeAttr(normalizedWidgetId)}"]`) ??
      document.querySelector(`[data-widget-id="${escapeAttr(widgetId)}"]`);
    if (!(targetEl instanceof HTMLElement)) return;

    consumedWidgetRef.current = location.key;
    // Restore any prior highlight before applying a new one so stripping `?widget=` below
    // does not cancel the highlight timer via effect cleanup.
    highlightCleanupRef.current?.();
    targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    targetEl.setAttribute('data-deep-link-highlighted', 'true');
    const prevOutline = targetEl.style.outline;
    const prevOutlineOffset = targetEl.style.outlineOffset;
    const prevBorderRadius = targetEl.style.borderRadius;
    targetEl.style.outline = '2px solid var(--color-accent-primary)';
    targetEl.style.outlineOffset = '4px';
    targetEl.style.borderRadius = '8px';

    let restored = false;
    const restoreHighlight = () => {
      if (restored) return;
      restored = true;
      window.clearTimeout(clearTimer);
      targetEl.removeAttribute('data-deep-link-highlighted');
      targetEl.style.outline = prevOutline;
      targetEl.style.outlineOffset = prevOutlineOffset;
      targetEl.style.borderRadius = prevBorderRadius;
      if (highlightCleanupRef.current === restoreHighlight) {
        highlightCleanupRef.current = null;
      }
    };
    const clearTimer = window.setTimeout(restoreHighlight, 3000);
    highlightCleanupRef.current = restoreHighlight;

    params.delete('widget');
    const nextSearch = params.toString();
    navigate(
      { pathname: location.pathname, search: nextSearch ? `?${nextSearch}` : '' },
      { replace: true, state: location.state },
    );
  }, [
    active,
    messagesDeps,
    location.key,
    location.search,
    location.pathname,
    location.state,
    navigate,
  ]);
}
