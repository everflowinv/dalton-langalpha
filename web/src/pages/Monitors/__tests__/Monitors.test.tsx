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

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light', setTheme: () => {} }),
}));

vi.mock('@/components/ui/use-toast', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/ui/use-toast')>();
  return { ...actual, toast: vi.fn<typeof actual.toast>() };
});

vi.mock('@/pages/ChatAgent/utils/api/artifacts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/ChatAgent/utils/api/artifacts')>();
  return { ...actual, unshareArtifact: vi.fn<typeof actual.unshareArtifact>() };
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
    listSharedDocuments: vi.fn<typeof actual.listSharedDocuments>(async () => []),
    getMonitorDetail: vi.fn<typeof actual.getMonitorDetail>(),
    getPublicMonitorDetail: vi.fn<typeof actual.getPublicMonitorDetail>(),
    mintMonitorGrant: vi.fn<typeof actual.mintMonitorGrant>(),
  };
});

import {
  getMonitorDetail,
  getPublicMonitorDetail,
  listMonitors,
  listSharedDocuments,
  mintMonitorGrant,
  type SharedDocumentSummary,
} from '../api';
import { unshareArtifact } from '@/pages/ChatAgent/utils/api/artifacts';
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
const mockListSharedDocuments = vi.mocked(listSharedDocuments);
const mockGetMonitorDetail = vi.mocked(getMonitorDetail);
const mockGetPublicMonitorDetail = vi.mocked(getPublicMonitorDetail);
const mockMintMonitorGrant = vi.mocked(mintMonitorGrant);
const mockUnshareArtifact = vi.mocked(unshareArtifact);
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
    mockListSharedDocuments.mockResolvedValue([]);
    capabilitiesMock.enabled = true;
    bridgeCallbacks.onNavigate = null;
    bridgeCallbacks.lastPosted = [];
    mockClipboardWriteText = vi.fn<typeof navigator.clipboard.writeText>(async () => {});
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      writable: true,
      value: {
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
    // Must wrap cleanly instead of truncating trailing per-index dates.
    expect(errBanner.className).toContain('whitespace-normal');
    expect(errBanner.className).toContain('break-words');
    expect(errBanner.querySelector('.truncate')).toBeNull();
  });

  it('pins public and authenticated viewers to detail.monitor.last_verified_seq and shows has_recent_publish_issue notice', async () => {
    const publicMonitor = makeSummary({
      slug: 'public-verified-pin',
      title: 'Public Verified Pin',
      visibility: 'public',
      published_seq: 5,
      last_verified_seq: 4,
      last_publish_error: null,
      last_publish_failed_at: null,
      has_recent_publish_issue: true,
    });
    delete publicMonitor.workspace_id;
    delete publicMonitor.artifact_id;
    delete publicMonitor.owner_user_id;
    delete publicMonitor.entry_path;

    mockGetPublicMonitorDetail.mockResolvedValue(
      makeDetail({
        monitor: publicMonitor,
        frame_base: '/api/v1/dalton/monitors/public/public-verified-pin/',
        data_base: '/api/v1/dalton/monitors/public/public-verified-pin/data',
        monitor_grant: '',
        expires_in: 0,
        versions: [],
      }),
    );

    const { unmount } = renderWithProviders(
      <Routes>
        <Route path="/m/:slug" element={<PublicMonitorPage />} />
      </Routes>,
      { route: '/m/public-verified-pin' },
    );

    const publicFrame = await screen.findByTestId('bridged-frame');
    expect(publicFrame.getAttribute('data-src')).toContain(
      '/api/v1/dalton/monitors/public/public-verified-pin/v/4/',
    );
    const issueBanner = screen.getByTestId('monitor-publish-issue');
    expect(issueBanner).toBeInTheDocument();
    unmount();

    // Authenticated mode also prefers last_verified_seq over published_seq until user selects another version.
    mockGetMonitorDetail.mockResolvedValue(
      makeDetail({
        monitor: makeSummary({
          slug: 'auth-verified-pin',
          title: 'Auth Verified Pin',
          entry_path: 'index.html',
          published_seq: 6,
          last_verified_seq: 5,
          has_recent_publish_issue: true,
          last_publish_error: null,
          last_publish_failed_at: null,
        }),
        versions: [
          {
            seq: 5,
            entry_path: 'index.html',
            origin: 'owner',
            title: 'Auth Verified Pin',
            label: 'Verified',
            byte_len: 1000,
            published_at: '2026-11-27T10:00:00Z',
          },
        ],
      }),
    );

    renderWithProviders(
      <Routes>
        <Route path="/monitors/:slug" element={<Monitors />} />
      </Routes>,
      { route: '/monitors/auth-verified-pin' },
    );

    const authFrame = await screen.findByTestId('bridged-frame');
    expect(authFrame.getAttribute('data-src')).toContain('/v/5/index.html');
  });

  it('excludes non-HTML entries from monitor cards, groups shared documents into Shared by me and Shared with me, revokes own doc after confirmation, and hides Stop sharing on others', async () => {
    const user = userEvent.setup();
    mockListMonitors.mockResolvedValue([
      makeSummary({
        monitor_id: 'm-html',
        slug: 'html-dashboard',
        title: 'HTML Dashboard',
        entry_path: 'dashboard.html',
        category: 'overview',
      }),
      makeSummary({
        monitor_id: 'm-legacy-md',
        slug: 'legacy-notes',
        title: 'Legacy Markdown Entry',
        entry_path: 'notes/memo.md',
        category: 'other',
      }),
    ]);

    const myDoc: SharedDocumentSummary = {
      doc_id: 'd-mine',
      artifact_id: 'a-md-mine',
      artifact_code: 'mycode123456',
      workspace_id: 'ws-1',
      entry_path: 'notes/my-outlook.md',
      file_name: 'my-outlook.md',
      title: 'My Outlook',
      owner_user_id: 'local-user',
      owner_name: 'Alice',
      owner_email: 'alice@company.com',
      visibility: 'company',
      member_user_ids: [],
      published_seq: 1,
      keep_updated: false,
      acl_revision: 1,
      url: '/a/mycode123456',
      created_at: '2026-11-26T10:00:00Z',
      updated_at: '2026-11-27T09:00:00Z',
      can_manage: true,
      is_owner: true,
    };
    const otherMembersDoc: SharedDocumentSummary = {
      doc_id: 'd-1',
      artifact_id: 'a-md-1',
      artifact_code: 'doccode12345',
      workspace_id: 'ws-2',
      entry_path: 'notes/q4-memo.md',
      file_name: 'q4-memo.md',
      title: 'Q4 Memo',
      owner_user_id: 'u-2',
      owner_name: 'Bob',
      owner_email: 'bob@company.com',
      visibility: 'members',
      member_user_ids: ['local-user'],
      published_seq: 2,
      keep_updated: false,
      acl_revision: 1,
      url: '/a/doccode12345',
      created_at: '2026-11-26T10:00:00Z',
      updated_at: '2026-11-27T08:30:00Z',
      can_manage: false,
      is_owner: false,
    };
    const otherPublicDoc: SharedDocumentSummary = {
      ...otherMembersDoc,
      doc_id: 'd-2',
      artifact_id: 'a-pdf-2',
      artifact_code: 'pubcode12345',
      entry_path: 'briefs/public.pdf',
      file_name: 'public.pdf',
      title: 'Public Brief',
      owner_user_id: 'u-3',
      owner_name: 'Carol',
      visibility: 'public',
      url: '/a/pubcode12345',
    };

    mockListSharedDocuments.mockResolvedValue([myDoc, otherMembersDoc, otherPublicDoc]);
    mockUnshareArtifact.mockResolvedValue({
      artifact_id: myDoc.artifact_id,
      code: myDoc.artifact_code!,
      workspace_id: 'ws-1',
      path: myDoc.entry_path,
      name: myDoc.file_name,
      url: myDoc.url,
      shared: false,
      keep_updated: false,
      latest_seq: 1,
      shared_seq: 0,
      created_at: myDoc.created_at,
    } as never);

    const { unmount } = renderWithProviders(
      <Routes>
        <Route path="/monitors" element={<Monitors />} />
      </Routes>,
      { route: '/monitors' },
    );

    expect(await screen.findByText('HTML Dashboard')).toBeInTheDocument();
    expect(screen.queryByText('Legacy Markdown Entry')).not.toBeInTheDocument();

    const byMeGroup = await screen.findByTestId('shared-documents-by-me');
    expect(within(byMeGroup).getByText('my-outlook.md')).toBeInTheDocument();
    expect(within(byMeGroup).getByText('Team')).toBeInTheDocument();
    expect(within(byMeGroup).getByText(/Alice/)).toBeInTheDocument();
    expect(within(byMeGroup).getByText(/Updated:/i)).toBeInTheDocument();
    expect(within(byMeGroup).getByRole('link', { name: /Open/i })).toHaveAttribute('href', '/a/mycode123456');
    const stopBtn = within(byMeGroup).getByRole('button', { name: /Stop sharing/i });

    const withMeGroup = await screen.findByTestId('shared-documents-with-me');
    expect(within(withMeGroup).getByText('q4-memo.md')).toBeInTheDocument();
    expect(within(withMeGroup).getByText('Selected colleagues')).toBeInTheDocument();
    expect(within(withMeGroup).getByText(/Bob/)).toBeInTheDocument();
    expect(within(withMeGroup).getByText('public.pdf')).toBeInTheDocument();
    expect(within(withMeGroup).getByText('Public link')).toBeInTheDocument();
    expect(within(withMeGroup).getByText(/Carol/)).toBeInTheDocument();
    expect(within(withMeGroup).queryByRole('button', { name: /Stop sharing/i })).toBeNull();

    // Clicking Stop sharing opens confirmation dialog; confirming calls unshareArtifact and removes the item.
    mockListSharedDocuments.mockResolvedValue([otherMembersDoc, otherPublicDoc]);
    await user.click(stopBtn);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/my-outlook\.md/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: /Stop sharing/i }));

    await waitFor(() => expect(mockUnshareArtifact).toHaveBeenCalledWith('a-md-mine'));
    await waitFor(() => expect(screen.queryByTestId('shared-documents-by-me')).toBeNull());
    expect(screen.getByTestId('shared-documents-with-me')).toBeInTheDocument();
    unmount();

    // When only Shared by me is present, Shared with me is hidden.
    mockListSharedDocuments.mockResolvedValue([myDoc]);
    const onlyMine = renderWithProviders(
      <Routes>
        <Route path="/monitors" element={<Monitors />} />
      </Routes>,
      { route: '/monitors' },
    );
    expect(await screen.findByTestId('shared-documents-by-me')).toBeInTheDocument();
    expect(screen.queryByTestId('shared-documents-with-me')).toBeNull();
    onlyMine.unmount();

    // When both groups are empty, the entire shared-documents section stays hidden.
    mockListSharedDocuments.mockResolvedValue([]);
    renderWithProviders(
      <Routes>
        <Route path="/monitors" element={<Monitors />} />
      </Routes>,
      { route: '/monitors' },
    );
    expect(await screen.findByText('HTML Dashboard')).toBeInTheDocument();
    expect(screen.queryByTestId('shared-documents-section')).toBeNull();
  });

  it('renders a legacy Markdown monitor using Markdown viewer with raw toggle instead of raw iframe', async () => {
    const user = userEvent.setup();
    const mdBody = [
      '# Earnings Recap',
      '',
      'Summary with **strong growth**.',
      '',
      '| Metric | Value |',
      '| --- | --- |',
      '| Revenue | $10B |',
      '',
      '<script>window.__mdMonitorXss = true;</script>',
    ].join('\n');

    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/markdown; charset=utf-8' }),
      text: async () => mdBody,
      blob: async () => new Blob([mdBody], { type: 'text/markdown' }),
    }));
    vi.stubGlobal('fetch', fetchMock);

    try {
      mockGetMonitorDetail.mockResolvedValue(
        makeDetail({
          monitor: makeSummary({
            slug: 'legacy-recap',
            title: 'Earnings Recap',
            entry_path: 'notes/recap.md',
            published_seq: 1,
          }),
          versions: [
            {
              seq: 1,
              entry_path: 'notes/recap.md',
              origin: 'owner',
              title: 'Earnings Recap',
              label: null,
              byte_len: mdBody.length,
              published_at: '2026-11-27T08:00:00Z',
            },
          ],
        }),
      );

      renderWithProviders(
        <Routes>
          <Route path="/monitors/:slug" element={<Monitors />} />
        </Routes>,
        { route: '/monitors/legacy-recap' },
      );

      const bold = await screen.findByText('strong growth', {}, { timeout: 5000 });
      expect(bold.tagName).toBe('STRONG');
      expect(screen.queryByTestId('bridged-frame')).toBeNull();
      const host = document.querySelector('.monitors-frame-host') as HTMLElement;
      expect(within(host).getByRole('heading', { level: 1, name: 'Earnings Recap' })).toBeInTheDocument();
      expect(within(host).getByRole('table')).toBeInTheDocument();
      expect(document.querySelector('script')).toBeNull();
      expect((window as unknown as Record<string, unknown>).__mdMonitorXss).toBeUndefined();

      // Switch to raw Markdown view and back.
      await user.click(screen.getByRole('button', { name: /View raw/i }));
      expect(await screen.findByText(/\*\*strong growth\*\*/)).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: /View rendered/i }));
      expect((await screen.findByText('strong growth')).tagName).toBe('STRONG');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('renders an anonymous public /m/ legacy Markdown page from real trimmed public payload with raw toggle and download', async () => {
    const user = userEvent.setup();
    const mdBody = [
      '# Public Weekly Note',
      '',
      'Highlights: **margin expansion** this quarter.',
      '',
      '<script>window.__publicMdXss = true;</script>',
    ].join('\n');

    const createObjectURLMock = vi.fn<typeof URL.createObjectURL>(() => 'blob:public-md-download');
    const revokeObjectURLMock = vi.fn<typeof URL.revokeObjectURL>();
    URL.createObjectURL = createObjectURLMock;
    URL.revokeObjectURL = revokeObjectURLMock;

    const publicDetailPayload: MonitorDetail = {
      monitor: {
        monitor_id: 'm-pub-md',
        slug: 'public-weekly-note',
        owner_name: 'Alice',
        owner_email: null,
        title: 'Public Weekly Note',
        category: 'other',
        sort_order: 0,
        visibility: 'public',
        member_user_ids: [],
        published_seq: 2,
        keep_updated: false,
        source_kind: 'workspace',
        source_ref: null,
        acl_revision: 1,
        last_publish_error: null,
        last_publish_failed_at: null,
        data_as_of: null,
        created_at: '2026-11-27T08:00:00Z',
        updated_at: '2026-11-27T08:00:00Z',
        can_manage: false,
        entry_file_type: 'markdown',
      },
      versions: [],
      monitor_grant: '',
      frame_base: '/api/v1/dalton/monitors/public/public-weekly-note/',
      data_base: '/api/v1/dalton/monitors/public/public-weekly-note/data',
      expires_in: 0,
    };

    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url === '/api/v1/dalton/monitors/public/public-weekly-note/v/2/') {
        return {
          ok: true,
          status: 200,
          headers: new Headers({ 'content-type': 'text/markdown; charset=utf-8' }),
          text: async () => mdBody,
          blob: async () => new Blob([mdBody], { type: 'text/markdown' }),
        } as Response;
      }
      return { ok: false, status: 404, headers: new Headers() } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);
    mockGetPublicMonitorDetail.mockResolvedValue(publicDetailPayload);

    try {
      renderWithProviders(
        <Routes>
          <Route path="/m/:slug" element={<PublicMonitorPage />} />
        </Routes>,
        { route: '/m/public-weekly-note' },
      );

      const bold = await screen.findByText('margin expansion', {}, { timeout: 5000 });
      expect(bold.tagName).toBe('STRONG');
      expect(screen.queryByTestId('bridged-frame')).toBeNull();
      expect(document.querySelector('script')).toBeNull();
      expect((window as unknown as Record<string, unknown>).__publicMdXss).toBeUndefined();

      // View raw / View rendered toggle works in anonymous mode
      await user.click(screen.getByRole('button', { name: /View raw/i }));
      expect(await screen.findByText(/\*\*margin expansion\*\*/)).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: /View rendered/i }));
      expect((await screen.findByText('margin expansion')).tagName).toBe('STRONG');

      // Download button fetches the anonymous entry endpoint
      await user.click(screen.getByRole('button', { name: /Download/i }));
      await waitFor(() => expect(createObjectURLMock).toHaveBeenCalled());
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/dalton/monitors/public/public-weekly-note/v/2/',
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('resolves ../ relative image and link on anonymous /m/ entry in a subdirectory without root fallback', async () => {
    const user = userEvent.setup();
    const entryDir = ['reports', 'q4'];
    const versionFiles: Record<string, { body: string; contentType: string }> = {
      'reports/q4/memo.md': {
        body: [
          '# Q4 Subdir Memo',
          '',
          '![Shared Logo](../shared/logo.png)',
          '![Root Decoy](shared/logo.png)',
          '',
          'See [Shared Overview](../shared/overview.md), [Root Decoy Doc](shared/overview.md), and [Outside](../../../etc/passwd.md).',
        ].join('\n'),
        contentType: 'text/markdown; charset=utf-8',
      },
      'reports/shared/logo.png': { body: 'parent-shared-logo', contentType: 'image/png' },
      'reports/shared/overview.md': {
        body: '# Shared Overview\n\nOpened via **parent relative link**.',
        contentType: 'text/markdown; charset=utf-8',
      },
      // Workspace-root decoys with the same suffix must never be returned for entry-relative paths.
      'shared/logo.png': { body: 'wrong-root-logo', contentType: 'image/png' },
      'shared/overview.md': { body: '# Wrong Root Overview', contentType: 'text/markdown; charset=utf-8' },
    };

    const resolveEntryRel = (rel: string): string | null => {
      const stack = [...entryDir];
      for (const seg of rel.split('/')) {
        if (!seg || seg === '.') continue;
        if (seg === '..') {
          if (stack.length === 0) return null;
          stack.pop();
          continue;
        }
        stack.push(seg);
      }
      return stack.length > 0 ? stack.join('/') : null;
    };

    const createObjectURLMock = vi.fn<typeof URL.createObjectURL>(() => 'blob:parent-shared-logo');
    const revokeObjectURLMock = vi.fn<typeof URL.revokeObjectURL>();
    URL.createObjectURL = createObjectURLMock;
    URL.revokeObjectURL = revokeObjectURLMock;

    const publicDetailPayload: MonitorDetail = {
      monitor: {
        monitor_id: 'm-pub-subdir-md',
        slug: 'public-q4-memo',
        owner_name: 'Alice',
        owner_email: null,
        title: 'Q4 Subdir Memo',
        category: 'other',
        sort_order: 0,
        visibility: 'public',
        member_user_ids: [],
        published_seq: 2,
        keep_updated: false,
        source_kind: 'workspace',
        source_ref: null,
        acl_revision: 1,
        last_publish_error: null,
        last_publish_failed_at: null,
        data_as_of: null,
        created_at: '2026-11-27T08:00:00Z',
        updated_at: '2026-11-27T08:00:00Z',
        can_manage: false,
        entry_file_type: 'markdown',
      },
      versions: [],
      monitor_grant: '',
      frame_base: '/api/v1/dalton/monitors/public/public-q4-memo/',
      data_base: '/api/v1/dalton/monitors/public/public-q4-memo/data',
      expires_in: 0,
    };

    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const urlStr = String(input);
      const parsed = new URL(urlStr, 'http://localhost');
      if (parsed.pathname === '/api/v1/dalton/monitors/public/public-q4-memo/v/2/') {
        const rel = parsed.searchParams.get('rel');
        const target = rel === null ? 'reports/q4/memo.md' : resolveEntryRel(rel);
        const hit = target ? versionFiles[target] : undefined;
        if (hit) {
          return {
            ok: true,
            status: 200,
            headers: new Headers({ 'content-type': hit.contentType }),
            text: async () => hit.body,
            blob: async () => new Blob([hit.body], { type: hit.contentType }),
          } as Response;
        }
      }
      return { ok: false, status: 404, headers: new Headers() } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);
    mockGetPublicMonitorDetail.mockResolvedValue(publicDetailPayload);

    try {
      renderWithProviders(
        <Routes>
          <Route path="/m/:slug" element={<PublicMonitorPage />} />
        </Routes>,
        { route: '/m/public-q4-memo' },
      );

      expect(await screen.findByRole('heading', { level: 1, name: 'Q4 Subdir Memo' })).toBeInTheDocument();

      // ../shared/logo.png loads via ?rel=..%2Fshared%2Flogo.png (resolved to reports/shared/logo.png)
      const logoImg = await screen.findByAltText('Shared Logo');
      await waitFor(() => expect(logoImg).toHaveAttribute('src', 'blob:parent-shared-logo'));

      // Root decoy image (shared/logo.png -> reports/q4/shared/logo.png) does NOT resolve to workspace root
      expect(await screen.findByText(/logo\.png/i)).toBeInTheDocument();

      // ../shared/overview.md probes OK and opens inside the viewer
      const overviewLink = await screen.findByRole('link', { name: 'Shared Overview' });
      expect(overviewLink).toHaveAttribute(
        'href',
        '/api/v1/dalton/monitors/public/public-q4-memo/v/2/?rel=..%2Fshared%2Foverview.md',
      );

      // Root decoy link and escaping ../../../etc/passwd.md stay placeholders
      const placeholders = await screen.findAllByTestId('shared-link-placeholder');
      const placeholderText = placeholders.map((el) => el.textContent).join(' ');
      expect(placeholderText).toContain('Root Decoy Doc');
      expect(placeholderText).toContain('Outside');
      expect(screen.queryByRole('link', { name: 'Root Decoy Doc' })).toBeNull();
      expect(screen.queryByRole('link', { name: 'Outside' })).toBeNull();

      await user.click(overviewLink);
      expect(await screen.findByRole('heading', { level: 1, name: 'Shared Overview' })).toBeInTheDocument();
      expect(screen.getByText('parent relative link').tagName).toBe('STRONG');
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
