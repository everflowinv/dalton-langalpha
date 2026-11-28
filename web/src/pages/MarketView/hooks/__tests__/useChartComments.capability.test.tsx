/**
 * DALTON-HOOK(artifact-publishing-off)/(external-share-guard)/(team-sharing):
 * chart comment controls require the deployment file-comments switch, a confirmed
 * non-external caller identity, and an owned (non-colleague) workspace.
 */
import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChartCommentsHost } from '@/pages/ChatAgent/components/viewers/comments/chart/ChartComments';
import { useChartComments } from '../useChartComments';

const state = vi.hoisted(() => ({
  artifactCommentsEnabled: true,
  canActAsOwner: true,
  isConfirming: false,
  isExternal: false,
  isColleague: false,
}));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  return {
    ...actual,
    useCapabilities: () => ({
      ...actual.useCapabilities(),
      artifactCommentsEnabled: state.artifactCommentsEnabled,
    }),
  };
});

vi.mock('@/hooks/useCallerIdentity', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCallerIdentity')>();
  return {
    ...actual,
    useCallerIdentity: () => ({
      ...actual.useCallerIdentity(),
      tier: state.isExternal ? 'external' : 'internal',
      status: state.isConfirming ? 'loading' : 'ready',
      isExternal: state.isExternal,
      canActAsOwner: state.canActAsOwner,
      isConfirming: state.isConfirming,
    }),
  };
});

vi.mock('@/pages/ChatAgent/components/dalton/TeamSharing', () => ({
  useTeamReadOnly: () => ({
    access: state.isColleague ? 'rw' : 'owner',
    isColleague: state.isColleague,
    isReadOnly: false,
    canWriteFiles: true,
    colleagueDispatchEnabled: false,
    callerTier: state.isExternal ? 'external' : 'internal',
    isThreadOnly: false,
    ownerName: null,
    ownerEmail: null,
  }),
}));

vi.mock('@/pages/ChatAgent/components/viewers/html/comments/CommentTools', () => ({
  CommentTools: () => null,
}));

const setMode = vi.fn();
const host = {
  ui: { modeTab: 'chart-tab', setMode },
  tabId: 'chart-tab',
  active: true,
} as unknown as ChartCommentsHost;

const where = { workspaceId: 'ws-1', chartId: 'NVDA:1day' };
const drawArg = {
  chart: null,
  seriesRef: { current: null },
  content: null,
  drawing: { render: vi.fn() },
  interval: '1day',
} as never;

describe('useChartComments under file-comments switch, caller identity, and workspace access', () => {
  beforeEach(() => {
    state.artifactCommentsEnabled = true;
    state.canActAsOwner = true;
    state.isConfirming = false;
    state.isExternal = false;
    state.isColleague = false;
    setMode.mockClear();
  });

  it('offers the comment tool and mode for an internal owner while the switch is on', () => {
    const { result } = renderHook(() => useChartComments(host, true, where));
    expect(result.current.tools).toBeTruthy();
    expect(result.current.mode).toBe(true);
  });

  it('offers no comment tool, mode or layer while the switch is off', () => {
    state.artifactCommentsEnabled = false;
    const { result } = renderHook(() => useChartComments(host, true, where));
    expect(result.current.tools).toBeFalsy();
    expect(result.current.mode).toBe(false);
    expect(result.current.layer(drawArg)).toBeFalsy();
    expect(setMode).toHaveBeenCalledWith('chart-tab', false);
  });

  it('hides chart comment entry and exits comment mode for external members', () => {
    state.isExternal = true;
    state.canActAsOwner = false;
    const { result } = renderHook(() => useChartComments(host, true, where));
    expect(result.current.tools).toBeFalsy();
    expect(result.current.mode).toBe(false);
    expect(result.current.layer(drawArg)).toBeFalsy();
    expect(setMode).toHaveBeenCalledWith('chart-tab', false);
  });

  it('hides chart comment entry while caller identity is still confirming', () => {
    state.isConfirming = true;
    const { result } = renderHook(() => useChartComments(host, true, where));
    expect(result.current.tools).toBeFalsy();
    expect(result.current.mode).toBe(false);
    expect(result.current.layer(drawArg)).toBeFalsy();
    expect(setMode).toHaveBeenCalledWith('chart-tab', false);
  });

  it('hides chart comment entry on colleague workspaces even for internal members', () => {
    state.isColleague = true;
    const { result } = renderHook(() => useChartComments(host, true, where));
    expect(result.current.tools).toBeFalsy();
    expect(result.current.mode).toBe(false);
    expect(result.current.layer(drawArg)).toBeFalsy();
    expect(setMode).toHaveBeenCalledWith('chart-tab', false);
  });
});
