/**
 * DALTON-HOOK(artifact-publishing-off): with the deployment's file-comments
 * switch off, a chart offers no comment tool and draws no comment layer.
 */
import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ChartCommentsHost } from '@/pages/ChatAgent/components/viewers/comments/chart/ChartComments';
import { useChartComments } from '../useChartComments';

const caps = vi.hoisted(() => ({ artifactCommentsEnabled: true }));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  return {
    ...actual,
    useCapabilities: () => ({ ...actual.useCapabilities(), artifactCommentsEnabled: caps.artifactCommentsEnabled }),
  };
});

vi.mock('@/pages/ChatAgent/components/viewers/html/comments/CommentTools', () => ({
  CommentTools: () => null,
}));

const host = {
  ui: { modeTab: 'chart-tab', setMode: vi.fn() },
  tabId: 'chart-tab',
  active: true,
} as unknown as ChartCommentsHost;

const where = { workspaceId: 'ws-1', chartId: 'NVDA:1day' };

describe('useChartComments under the file-comments switch', () => {
  it('offers the comment tool and mode while the switch is on', () => {
    caps.artifactCommentsEnabled = true;
    const { result } = renderHook(() => useChartComments(host, true, where));
    expect(result.current.tools).toBeTruthy();
    expect(result.current.mode).toBe(true);
  });

  it('offers no comment tool, mode or layer while the switch is off', () => {
    caps.artifactCommentsEnabled = false;
    const { result } = renderHook(() => useChartComments(host, true, where));
    expect(result.current.tools).toBeFalsy();
    expect(result.current.mode).toBe(false);
    expect(result.current.layer({ chart: null, seriesRef: { current: null }, content: null, drawing: { render: vi.fn() }, interval: '1day' } as never)).toBeFalsy();
  });
});
