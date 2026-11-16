import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DaltonCoverageCard } from '../DaltonCoverageCard';
import type {
  CoverageUnitDetail,
  CoverageUnitRecord,
  DaltonCoverageUnitsResponse,
} from '@/lib/dalton/coverage';

const h = vi.hoisted(() => ({
  userId: 'u-self',
  unitsResponse: { enabled: false } as DaltonCoverageUnitsResponse,
  unitsLoading: false,
  detailByUnit: {} as Record<string, CoverageUnitDetail>,
  registerUnit: vi.fn<(payload: unknown) => Promise<CoverageUnitRecord>>(),
  bindUnit: vi.fn<(unitId: string, wsId: string) => Promise<CoverageUnitRecord>>(),
  unbindUnit: vi.fn<(unitId: string) => Promise<CoverageUnitRecord>>(),
  archiveUnit: vi.fn<(unitId: string) => Promise<CoverageUnitRecord>>(),
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: { user_id: h.userId } }),
}));

function LocationSpy() {
  const loc = useLocation();
  const navigate = useNavigate();
  return (
    <div>
      <div data-testid="coverage-location-spy">{`${loc.pathname}${loc.search}${loc.hash}`}</div>
      <button
        type="button"
        data-testid="replay-unclassified-hash"
        onClick={() => navigate('/settings?tab=coverage#dalton-coverage-unclassified')}
      >
        Replay Hash
      </button>
    </div>
  );
}

vi.mock('@/components/TimezonePicker', () => ({
  default: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <input
      data-testid="tz-picker-stub"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));

vi.mock('@/lib/dalton/coverage', () => ({
  useDaltonCoverageUnits: () => ({
    data: h.unitsResponse,
    isLoading: h.unitsLoading,
    isFetching: false,
    isError: false,
    refetch: vi.fn(async () => ({ data: h.unitsResponse })),
  }),
  useDaltonCoverageUnitDetail: (unitId: string | null) => ({
    data: unitId ? h.detailByUnit[unitId] : undefined,
    isLoading: false,
    isError: false,
  }),
  splitCoverageUnits: (units: Array<{ kind: string }>) => ({
    sectors: units.filter((u) => u.kind === 'sector'),
    standaloneCompanies: units.filter((u) => u.kind === 'company'),
  }),
  coverageUnitErrorCode: () => null,
  registerDaltonCoverageUnit: (p: unknown) => h.registerUnit(p),
  bindDaltonCoverageUnit: (u: string, w: string) => h.bindUnit(u, w),
  unbindDaltonCoverageUnit: (u: string) => h.unbindUnit(u),
  archiveDaltonCoverageUnit: (u: string) => h.archiveUnit(u),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) =>
      opts && 'count' in opts && 'total' in opts
        ? `${key}:${opts.count}/${opts.total}`
        : key,
  }),
}));

function renderCard(initialEntry = '/settings?tab=coverage') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <LocationSpy />
        <Routes>
          <Route path="/settings" element={<DaltonCoverageCard />} />
          <Route path="/chat/:workspaceId" element={<div data-testid="chat-route">Chat Route</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('DaltonCoverageCard', () => {
  beforeEach(() => {
    h.userId = 'u-self';
    h.unitsLoading = false;
    h.registerUnit.mockReset().mockResolvedValue({
      unit_id: 'u-sec-new',
      kind: 'sector',
      slug: 'ai-infra',
      display_name: 'AI Infra',
      ticker: null,
      owner_user_id: 'u-self',
      workspace_id: null,
      research_tz: null,
      status: 'active',
      created_at: '2026-11-16T00:00:00Z',
      updated_at: '2026-11-16T00:00:00Z',
    });
    h.bindUnit.mockReset().mockResolvedValue({
      unit_id: 'sec-1',
      kind: 'sector',
      slug: 'semi',
      display_name: 'Semis',
      ticker: null,
      owner_user_id: 'u-self',
      workspace_id: 'ws-1',
      research_tz: null,
      status: 'active',
      created_at: '2026-11-16T00:00:00Z',
      updated_at: '2026-11-16T00:00:00Z',
    });
    h.unbindUnit.mockReset().mockResolvedValue({
      unit_id: 'sec-2',
      kind: 'sector',
      slug: 'cloud',
      display_name: 'Cloud',
      ticker: null,
      owner_user_id: 'u-self',
      workspace_id: null,
      research_tz: null,
      status: 'active',
      created_at: '2026-11-16T00:00:00Z',
      updated_at: '2026-11-16T00:00:00Z',
    });
    h.archiveUnit.mockReset().mockResolvedValue({
      unit_id: 'sec-2',
      kind: 'sector',
      slug: 'cloud',
      display_name: 'Cloud',
      ticker: null,
      owner_user_id: 'u-self',
      workspace_id: null,
      research_tz: null,
      status: 'archived',
      created_at: '2026-11-16T00:00:00Z',
      updated_at: '2026-11-16T00:00:00Z',
    });
    h.unitsResponse = {
      enabled: true,
      units: [
        {
          unit_id: 'sec-1',
          kind: 'sector',
          slug: 'semi',
          display_name: 'Semis',
          owner_user_id: 'u-self',
          workspace_id: null,
          readable: true,
          delegatable: true,
          auto_tracked: true,
        },
        {
          unit_id: 'sec-2',
          kind: 'sector',
          slug: 'cloud',
          display_name: 'Cloud',
          owner_user_id: 'u-self',
          workspace_id: 'ws-1',
          readable: true,
          delegatable: true,
          auto_tracked: true,
        },
        {
          unit_id: 'sec-other',
          kind: 'sector',
          slug: 'bio',
          display_name: 'Bio',
          owner_user_id: 'u-colleague',
          workspace_id: null,
          readable: true,
          delegatable: false,
          auto_tracked: false,
        },
      ],
      unclassified: [
        {
          workspace_id: 'ws-un',
          name: 'Scratch Research',
          description: null,
          updated_at: null,
        },
      ],
    };
    h.detailByUnit = {
      'sec-1': {
        unit_id: 'sec-1',
        kind: 'sector',
        slug: 'semi',
        display_name: 'Semis',
        owner_user_id: 'u-self',
        workspace_id: null,
        readable: true,
        delegatable: true,
        auto_tracked: true,
        sector: {
          docs: {
            sector: null,
            thesis: null,
            drivers: {
              version_no: 1,
              as_of: null,
              drivers: [
                {
                  id: 'd1',
                  name: 'Wafer Demand',
                  kind: null,
                  updated_at: null,
                  leading_indicators: [
                    {
                      id: 'i1',
                      name: 'CoWoS Capacity',
                      frequency: null,
                      good_direction: null,
                      latest: {
                        value: 45,
                        unit: 'kwpm',
                        period: '2026Q3',
                        as_of: '2026-10-01T00:00:00Z',
                      },
                    },
                  ],
                },
              ],
            },
            agenda_version_no: 1,
          },
          agenda_todos: {
            questions: [
              {
                id: 'q1',
                question: 'Confirm packaging capex',
                why: '',
                value: '',
                urgency: 'high',
                origin: null,
                waiting_pm: true,
              },
            ],
            validations: [{ id: 'v1', what: 'Validate 2nm yield', due: '2026-11-30' }],
            events: [],
          },
          members: [
            {
              member_id: 'm1',
              name: 'TSMC',
              ticker: 'TSM',
              company_unit_id: 'co-pending',
              workspace_created: false,
              workspace_id: null,
              scope: 'core',
              reason: '',
              valid_until: null,
            },
            {
              member_id: 'm2',
              name: 'NVIDIA',
              ticker: 'NVDA',
              company_unit_id: 'co-1',
              workspace_created: true,
              workspace_id: 'ws-co-1',
              scope: 'core',
              reason: '',
              valid_until: null,
            },
            {
              member_id: 'm3',
              name: 'Broadcom',
              ticker: 'AVGO',
              company_unit_id: null,
              workspace_created: false,
              workspace_id: 'ws-avgo',
              scope: 'watch',
              reason: '',
              valid_until: null,
            },
          ],
          evidence_gaps: [],
          ledger_gaps: [
            {
              request_id: 'lg1',
              kind: 'gap',
              question: 'Verify substrate lead time',
              target_workspace_id: null,
              status: 'open',
              created_at: null,
              updated_at: null,
            },
          ],
          pending_total: 5,
        },
      },
      'co-1': {
        unit_id: 'co-1',
        kind: 'company',
        slug: null,
        display_name: 'NVIDIA',
        ticker: 'NVDA',
        owner_user_id: 'u-self',
        workspace_id: 'ws-co-1',
        readable: true,
        delegatable: true,
        auto_tracked: true,
        company: {
          docs: {
            thesis: {
              version_no: 2,
              one_liner: 'Data center GPU leader',
              aggregate_status: 'active',
              stance: 'bullish',
              conviction: 'high',
              pillars: [{ id: 'p1', claim: 'CUDA moat', priority: 'p0', status: 'intact' }],
            },
          },
          sectors: [
            {
              unit_id: 'sec-1',
              slug: 'semi',
              display_name: 'Semis',
              member_id: 'm2',
              scope: 'core',
              valid_until: null,
              readable: true,
              delegatable: true,
              workspace_id: 'ws-sec-1',
            },
          ],
          re_review: [
            {
              id: 'rr1',
              question: 'Verify Blackwell rack ramp',
              value: 'Q1 FY27',
              urgency: 'medium',
              status: 'open',
              source_ref: null,
            },
          ],
        },
      },
    };
  });

  it('renders nothing before flag loads or when disabled', () => {
    h.unitsLoading = true;
    renderCard();
    expect(screen.queryByTestId('dalton-coverage-card')).not.toBeInTheDocument();

    h.unitsLoading = false;
    h.unitsResponse = { enabled: false };
    renderCard();
    expect(screen.queryByTestId('dalton-coverage-card')).not.toBeInTheDocument();
  });

  it('registers sector, binds unbound workspace from unclassified, shows badges & open link, and hides manage actions for colleague-owned units', async () => {
    renderCard();

    expect(screen.queryByTestId('dalton-coverage-archive-sec-other')).not.toBeInTheDocument();
    const row = screen.getByTestId('dalton-coverage-unit-sec-2');
    expect(row).toHaveTextContent('dalton.coverage.readable');
    expect(row).toHaveTextContent('dalton.coverage.delegatable');
    expect(row).toHaveTextContent('dalton.coverage.autoTracked');

    fireEvent.change(screen.getByTestId('dalton-coverage-slug-input'), {
      target: { value: 'ai-infra' },
    });
    fireEvent.click(screen.getByTestId('dalton-coverage-register-btn'));
    await waitFor(() =>
      expect(h.registerUnit).toHaveBeenCalledWith({
        kind: 'sector',
        slug: 'ai-infra',
        display_name: undefined,
        research_tz: undefined,
      }),
    );

    fireEvent.change(screen.getByTestId('dalton-coverage-bind-select-sec-1'), {
      target: { value: 'ws-un' },
    });
    fireEvent.click(screen.getByTestId('dalton-coverage-bind-btn-sec-1'));
    await waitFor(() => expect(h.bindUnit).toHaveBeenCalledWith('sec-1', 'ws-un'));
  });

  it('requires confirmation before unbinding and archiving', async () => {
    renderCard();

    fireEvent.click(screen.getByTestId('dalton-coverage-unbind-sec-2'));
    expect(h.unbindUnit).not.toHaveBeenCalled();
    const unbindConfirmBtns = await screen.findAllByRole('button', {
      name: 'dalton.coverage.unbind',
    });
    fireEvent.click(unbindConfirmBtns[unbindConfirmBtns.length - 1]);
    await waitFor(() => expect(h.unbindUnit).toHaveBeenCalledWith('sec-2'));

    fireEvent.click(screen.getByTestId('dalton-coverage-archive-sec-2'));
    expect(h.archiveUnit).not.toHaveBeenCalled();
    const archiveConfirmBtns = await screen.findAllByRole('button', {
      name: 'dalton.coverage.archiveUnit',
    });
    fireEvent.click(archiveConfirmBtns[archiveConfirmBtns.length - 1]);
    await waitFor(() => expect(h.archiveUnit).toHaveBeenCalledWith('sec-2'));
  });

  it('expands sector detail, expands covered company member with manage controls & detail, and navigates to workspaces', async () => {
    renderCard('/settings?tab=coverage#dalton-coverage-unclassified');
    expect(screen.getByText('Scratch Research')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-coverage-open-ws-ws-un')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('dalton-coverage-tab-sectors'));
    fireEvent.click(screen.getByTestId('dalton-coverage-expand-sec-1'));

    const detail = await screen.findByTestId('dalton-coverage-detail-sec-1');
    expect(detail).toHaveTextContent('CoWoS Capacity');
    expect(detail).toHaveTextContent('45 kwpm (2026Q3) · 2026-10-01');
    expect(detail).toHaveTextContent('Validate 2nm yield (2026-11-30)');
    expect(detail).toHaveTextContent('dalton.coverage.waitingPm');
    // Unit exists (co-pending) with workspace_created=false must still render notCovered
    expect(detail).toHaveTextContent('TSMC (dalton.coverage.notCovered)');
    expect(detail).toHaveTextContent('NVIDIA (dalton.coverage.covered)');
    // Member row openWorkspace depends strictly on workspace_id (even when workspace_created is false)
    expect(screen.getByTestId('dalton-coverage-open-ws-ws-avgo')).toBeInTheDocument();
    expect(detail).toHaveTextContent('dalton.coverage.pendingTruncated:1/5');

    // Expand covered member company -> renders CompanyDetail (including scope badge) & owned UnitManageActions
    fireEvent.click(screen.getByTestId('dalton-coverage-expand-member-co-1'));
    const memberDetail = await screen.findByTestId('dalton-coverage-member-detail-co-1');
    expect(memberDetail).toHaveTextContent('Data center GPU leader');
    expect(memberDetail).toHaveTextContent('core');
    expect(memberDetail).toHaveTextContent('Verify Blackwell rack ramp');
    expect(screen.getByTestId('dalton-coverage-unbind-co-1')).toBeInTheDocument();
    expect(screen.getByTestId('dalton-coverage-archive-co-1')).toBeInTheDocument();

    // Navigating with same #dalton-coverage-unclassified hash (new location.key) switches back to unclassified tab
    fireEvent.click(screen.getByTestId('replay-unclassified-hash'));
    expect(await screen.findByText('Scratch Research')).toBeInTheDocument();

    // Return to sectors and click bound unit Open Workspace link
    fireEvent.click(screen.getByTestId('dalton-coverage-tab-sectors'));
    fireEvent.click(screen.getByTestId('dalton-coverage-open-ws-ws-1'));
    expect(screen.getByTestId('coverage-location-spy')).toHaveTextContent('/chat/ws-1');
  });
});
