import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes, useLocation } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import type { MonitorDetail, MonitorSummary } from '../api';

const bridgeCallbacks = vi.hoisted(() => ({
  onNavigate: null as ((path: string) => void) | null,
  lastPosted: [] as unknown[],
}));

vi.mock('@/components/ui/use-toast', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/ui/use-toast')>();
  return { ...actual, toast: vi.fn<typeof actual.toast>() };
});

vi.mock('@/pages/ChatAgent/components/viewers/html/BridgedFrame', () => ({
  BridgedFrame: ({
    source,
    title,
    onBridge,
  }: {
    source: { key: string; src: string; sandbox: string } | null;
    title: string;
    onBridge?: (bridge: {
      ready: boolean;
      session: number;
      path: string | null;
      post: (msg: unknown) => void;
      subscribe: (fn: (msg: { type: string; path?: string }) => void) => () => void;
    }) => void;
  }) => {
    React.useEffect(() => {
      if (!onBridge) return;
      const listeners = new Set<(msg: { type: string; path?: string }) => void>();
      bridgeCallbacks.onNavigate = (path: string) => {
        for (const fn of listeners) fn({ type: 'page:navigate-monitor', path });
      };
      onBridge({
        ready: true,
        session: 1,
        path: 'index.html',
        post: (msg) => {
          bridgeCallbacks.lastPosted.push(msg);
        },
        subscribe: (fn) => {
          listeners.add(fn);
          return () => listeners.delete(fn);
        },
      });
    }, [onBridge]);
    return (
      <div
        data-testid="bridged-frame"
        data-src={source?.src ?? ''}
        data-sandbox={source?.sandbox ?? ''}
        title={title}
      />
    );
  },
}));

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    listMonitors: vi.fn<typeof actual.listMonitors>(),
    getMonitorDetail: vi.fn<typeof actual.getMonitorDetail>(),
    getPublicMonitorDetail: vi.fn<typeof actual.getPublicMonitorDetail>(),
    mintMonitorGrant: vi.fn<typeof actual.mintMonitorGrant>(),
  };
});

import { getMonitorDetail, getPublicMonitorDetail, listMonitors, mintMonitorGrant } from '../api';
import {
  CORE_DEFAULT_CAPABILITY_SET,
  EVERFLOW_DEFAULT_CAPABILITY_SET,
  type UseCapabilitiesResult,
} from '@/hooks/useCapabilities';
import Monitors from '../Monitors';
import PublicMonitorPage from '../PublicMonitorPage';

const capabilitiesMock = vi.hoisted(() => ({
  enabled: true,
}));

vi.mock('@/hooks/useCapabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useCapabilities')>();
  return {
    ...actual,
    useCapabilities: (): UseCapabilitiesResult => {
      const caps = capabilitiesMock.enabled
        ? actual.EVERFLOW_DEFAULT_CAPABILITY_SET
        : actual.CORE_DEFAULT_CAPABILITY_SET;
      return {
        profile: capabilitiesMock.enabled ? 'everflow' : 'core',
        capabilities: caps,
        hasCapability: (req) => actual.hasRequiredCapability(caps, req),
        brandName: capabilitiesMock.enabled ? 'Everflow' : 'Dalton',
        fingerprint: null,
        tickerLogosEnabled: capabilitiesMock.enabled,
        isLoading: false,
        isLoaded: true,
        isCoreBuild: !capabilitiesMock.enabled,
        isEverflowBuild: capabilitiesMock.enabled,
      };
    },
  };
});

const mockListMonitors = vi.mocked(listMonitors);
const mockGetMonitorDetail = vi.mocked(getMonitorDetail);
const mockGetPublicMonitorDetail = vi.mocked(getPublicMonitorDetail);
const mockMintMonitorGrant = vi.mocked(mintMonitorGrant);
let mockClipboardWriteText = vi.fn<typeof navigator.clipboard.writeText>();

function makeSummary(overrides: Partial<MonitorSummary> = {}): MonitorSummary {
  return {
    monitor_id: '60000000-0000-4000-8000-000000000001',
    slug: 'portfolio-beta',
    artifact_id: '61000000-0000-4000-8000-000000000001',
    artifact_code: 'code12345678',
    workspace_id: 'ws-1',
    entry_path: 'report.html',
    owner_user_id: 'u-1',
    owner_name: 'Alice',
    owner_email: 'alice@company.com',
    title: 'Portfolio Beta Monitor',
    category: 'portfolio',
    sort_order: 0,
    visibility: 'company',
    member_user_ids: [],
    published_seq: 3,
    keep_updated: true,
    source_kind: 'workspace',
    source_ref: 'ws-1:report.html',
    acl_revision: 1,
    last_publish_error: null,
    last_publish_failed_at: null,
    data_as_of: null,
    created_at: '2026-11-16T10:00:00Z',
    updated_at: '2026-11-16T11:00:00Z',
    can_manage: true,
    ...overrides,
  };
}

function makeDetail(overrides: Partial<MonitorDetail> = {}): MonitorDetail {
  const monitor = makeSummary(overrides.monitor);
  return {
    monitor,
    versions: [
      {
        seq: 3,
        entry_path: 'report.html',
        origin: 'owner',
        title: monitor.title,
        label: 'Latest',
        byte_len: 1024,
        published_at: '2026-11-16T11:00:00Z',
      },
      {
        seq: 2,
        entry_path: 'report.html',
        origin: 'owner',
        title: monitor.title,
        label: 'Prev',
        byte_len: 900,
        published_at: '2026-11-16T10:30:00Z',
      },
    ],
    monitor_grant: 'm1.secret-grant-token',
    frame_base: '/api/v1/dalton/monitors/g/m1.secret-grant-token/',
    data_base: '/api/v1/dalton/monitors/g/m1.secret-grant-token/data',
    expires_in: 43200,
    ...overrides,
  };
}

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location-probe">{loc.pathname}</div>;
}

describe('Monitors page (Checklist 8)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capabilitiesMock.enabled = true;
    bridgeCallbacks.onNavigate = null;
    bridgeCallbacks.lastPosted = [];
    mockClipboardWriteText = vi.fn<typeof navigator.clipboard.writeText>(async () => {});
    Object.assign(navigator, {
      clipboard: {
        writeText: mockClipboardWriteText,
      },
    });
  });

  it('groups monitors by category and shows publisher, visibility, page time, and data time', async () => {
    mockListMonitors.mockResolvedValue([
      makeSummary({
        monitor_id: 'm-1',
        slug: 'us-macro',
        title: 'US Macro Sentiment',
        category: 'us',
        visibility: 'company',
        data_as_of: '2026-11-16T11:55:00Z',
      }),
      makeSummary({
        monitor_id: 'm-2',
        slug: 'portfolio-beta',
        title: 'Portfolio Beta Monitor',
        category: 'portfolio',
        visibility: 'members',
        data_as_of: null,
      }),
    ]);

    renderWithProviders(
      <Routes>
        <Route path="/monitors" element={<Monitors />} />
      </Routes>,
      { route: '/monitors' },
    );

    const usSection = await screen.findByRole('region', { name: /US/i });
    expect(within(usSection).getByText('US Macro Sentiment')).toBeInTheDocument();
    expect(within(usSection).getByText(/Alice/)).toBeInTheDocument();
    expect(within(usSection).getByText(/Data as of:/i)).toBeInTheDocument();

    const portfolioSection = screen.getByRole('region', { name: /Portfolio/i });
    expect(within(portfolioSection).getByText('Portfolio Beta Monitor')).toBeInTheDocument();
    expect(within(portfolioSection).queryByText(/Data as of:/i)).not.toBeInTheDocument();
  });

  it('viewer copies fixed permalink without grant, uses opaque sandbox, and navigates on bridge message', async () => {
    const detail1 = makeDetail({
      monitor: makeSummary({
        slug: 'portfolio-beta',
        title: 'Portfolio Beta Monitor',
        last_publish_error: 'Timeout',
        last_publish_failed_at: '2026-11-16T11:30:00Z',
      }),
    });
    const detail2 = makeDetail({
      monitor: makeSummary({
        monitor_id: 'm-other',
        slug: 'hk-momentum',
        title: 'HK Momentum',
        category: 'hk',
      }),
    });

    mockGetMonitorDetail.mockImplementation(async (slug: string) =>
      slug === 'hk-momentum' ? detail2 : detail1,
    );
    mockMintMonitorGrant.mockResolvedValue({
      monitor_grant: 'm1.renewed',
      frame_base: '/api/v1/dalton/monitors/g/m1.renewed/',
      data_base: '/api/v1/dalton/monitors/g/m1.renewed/data',
      expires_in: 43200,
      acl_revision: 1,
    });

    renderWithProviders(
      <>
        <LocationProbe />
        <Routes>
          <Route path="/monitors/:slug" element={<Monitors />} />
        </Routes>
      </>,
      { route: '/monitors/portfolio-beta' },
    );

    const frame = await screen.findByTestId('bridged-frame');
    // Sandbox must NEVER include allow-same-origin
    expect(frame.getAttribute('data-sandbox')).not.toContain('allow-same-origin');
    expect(frame.getAttribute('data-sandbox')).toContain('allow-scripts');

    // Failure indicator shown
    expect(screen.getByText(/Last update failed:/i)).toBeInTheDocument();

    // Copy link copies fixed permalink /monitors/portfolio-beta (never exposes monitor_grant)
    await userEvent.click(screen.getByRole('button', { name: /Copy link/i }));
    expect(mockClipboardWriteText).toHaveBeenCalledWith(
      `${window.location.origin}/monitors/portfolio-beta`,
    );
    expect(mockClipboardWriteText.mock.calls[0][0]).not.toContain('secret-grant-token');

    // Cross-monitor navigation from sandboxed bridge routes cleanly via React Router
    await waitFor(() => expect(bridgeCallbacks.onNavigate).not.toBeNull());
    act(() => {
      bridgeCallbacks.onNavigate?.('/monitors/hk-momentum');
    });
    await waitFor(() => {
      expect(screen.getByTestId('location-probe')).toHaveTextContent('/monitors/hk-momentum');
    });
    expect(mockGetMonitorDetail).toHaveBeenCalledWith('hk-momentum');
  });

  it('public monitor copies /m/{slug} anonymous link and PublicMonitorPage loads without auth', async () => {
    const trimmedSummary = makeSummary({
      slug: 'public-market-pulse',
      title: 'Public Market Pulse',
      visibility: 'public',
      published_seq: 4,
    });
    delete trimmedSummary.workspace_id;
    delete trimmedSummary.artifact_id;
    delete trimmedSummary.owner_user_id;
    delete trimmedSummary.entry_path;

    const publicDetail = makeDetail({
      monitor: trimmedSummary,
      frame_base: '/api/v1/dalton/monitors/public/public-market-pulse/',
      data_base: '/api/v1/dalton/monitors/public/public-market-pulse/data',
      monitor_grant: '',
      expires_in: 0,
      versions: [],
    });
    mockGetPublicMonitorDetail.mockResolvedValue(publicDetail);

    renderWithProviders(
      <Routes>
        <Route path="/m/:slug" element={<PublicMonitorPage />} />
      </Routes>,
      { route: '/m/public-market-pulse' },
    );

    const frame = await screen.findByTestId('bridged-frame');
    expect(frame.getAttribute('data-src')).toContain(
      '/api/v1/dalton/monitors/public/public-market-pulse/v/4/',
    );
    expect(frame.getAttribute('data-sandbox')).not.toContain('allow-same-origin');
    expect(mockGetPublicMonitorDetail).toHaveBeenCalledWith('public-market-pulse');
    expect(mockGetMonitorDetail).not.toHaveBeenCalled();

    // Public monitor Copy link copies /m/{slug} anonymous permalink
    await userEvent.click(screen.getByRole('button', { name: /Copy link/i }));
    expect(mockClipboardWriteText).toHaveBeenCalledWith(
      `${window.location.origin}/m/public-market-pulse`,
    );
  });

  it('uses each version entry_path when switching to legacy v1 on a migrated monitor', async () => {
    const migratedDetail = makeDetail({
      monitor: makeSummary({
        slug: 'hk-momentum',
        title: 'HK Momentum',
        entry_path: 'hk-momentum/index.html',
        published_seq: 5,
      }),
      versions: [
        {
          seq: 5,
          entry_path: 'hk-momentum/index.html',
          origin: 'owner',
          title: 'HK Momentum',
          label: 'Migrated',
          byte_len: 1200,
          published_at: '2026-11-17T10:00:00Z',
        },
        {
          seq: 1,
          entry_path: 'index.html',
          origin: 'owner',
          title: 'HK Momentum',
          label: 'Legacy',
          byte_len: 800,
          published_at: '2026-11-16T10:00:00Z',
        },
      ],
    });
    const detailFetcher = vi.fn<typeof getMonitorDetail>(async () => migratedDetail);
    mockGetMonitorDetail.mockImplementation(detailFetcher);

    renderWithProviders(
      <Routes>
        <Route path="/monitors/:slug" element={<Monitors />} />
      </Routes>,
      { route: '/monitors/hk-momentum' },
    );

    const frame = await screen.findByTestId('bridged-frame');
    expect(frame.getAttribute('data-src')).toBe(
      '/api/v1/dalton/monitors/g/m1.secret-grant-token/v/5/hk-momentum/index.html',
    );
    expect(detailFetcher).toHaveBeenCalledWith('hk-momentum');

    const versionSelect = screen.getByRole('combobox', { name: /Version|切换版本/i });
    await userEvent.selectOptions(versionSelect, '1');

    expect(screen.getByTestId('bridged-frame').getAttribute('data-src')).toBe(
      '/api/v1/dalton/monitors/g/m1.secret-grant-token/v/1/index.html',
    );

    await userEvent.selectOptions(versionSelect, '5');

    expect(screen.getByTestId('bridged-frame').getAttribute('data-src')).toBe(
      '/api/v1/dalton/monitors/g/m1.secret-grant-token/v/5/hk-momentum/index.html',
    );
  });

  it('hides data_job monitors when data_jobs capability is off while keeping workspace monitors visible', async () => {
    capabilitiesMock.enabled = false;
    mockListMonitors.mockResolvedValue([
      makeSummary({
        monitor_id: 'm-ws',
        slug: 'workspace-dashboard',
        title: 'Workspace Coverage Dashboard',
        category: 'portfolio',
        source_kind: 'workspace',
      }),
      makeSummary({
        monitor_id: 'm-dj',
        slug: 'internal-data-job',
        title: 'Internal Data Job Monitor',
        category: 'us',
        source_kind: 'data_job',
      }),
    ]);

    renderWithProviders(
      <Routes>
        <Route path="/monitors" element={<Monitors />} />
      </Routes>,
      { route: '/monitors' },
    );

    expect(await screen.findByText('Workspace Coverage Dashboard')).toBeInTheDocument();
    expect(screen.queryByText('Internal Data Job Monitor')).not.toBeInTheDocument();
    expect(CORE_DEFAULT_CAPABILITY_SET.has('data_jobs')).toBe(false);
    expect(EVERFLOW_DEFAULT_CAPABILITY_SET.has('data_jobs')).toBe(true);
  });

  it('viewer visibly displays publish failure details and per-index data dates instead of only exit_code_1', async () => {
    const failureText =
      'smallcap 持有成分缺少必要价格 1 只 (如 00626) | 数据日期: smallcap=2026-04-16, nonggt=2026-04-17, hsi=2026-04-17';
    mockGetMonitorDetail.mockResolvedValue(
      makeDetail({
        monitor: makeSummary({
          slug: 'hk-momentum',
          title: 'HK Momentum',
          category: 'hk',
          source_kind: 'data_job',
          last_publish_error: failureText,
          last_publish_failed_at: '2026-04-17T10:15:00Z',
        }),
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/monitors/:slug" element={<Monitors />} />
      </Routes>,
      { route: '/monitors/hk-momentum' },
    );

    await screen.findByTestId('bridged-frame');
    const errBanner = screen.getByTestId('monitor-publish-error');
    expect(errBanner).toHaveTextContent(/00626/);
    expect(errBanner).toHaveTextContent(/smallcap=2026-04-16/);
    expect(errBanner).toHaveTextContent(/nonggt=2026-04-17/);
    expect(errBanner).not.toHaveTextContent(/exit_code_1/);
  });
});
