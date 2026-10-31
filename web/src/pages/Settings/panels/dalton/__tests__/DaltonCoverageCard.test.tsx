import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { DaltonCoverageCard } from '../DaltonCoverageCard';
import {
  useDaltonCoverageUnits,
  useDaltonCoverageUnitDetail,
} from '@/lib/dalton/coverage';

vi.mock('@/lib/dalton/coverage', () => ({
  useDaltonCoverageUnits: vi.fn(),
  useDaltonCoverageUnitDetail: vi.fn(),
}));

const mockUnitsQuery = useDaltonCoverageUnits as unknown as Mock;
const mockDetailQuery = useDaltonCoverageUnitDetail as unknown as Mock;

// 与后端 /api/v1/dalton/coverage/units 的真实响应逐字段一致
// （src/dalton/autonomy/units_view.py）。
const SECTOR_UNIT_ID = '22222222-2222-2222-2222-222222222222';
const MEMBER_COMPANY_UNIT_ID = '44444444-4444-4444-4444-444444444444';
const STANDALONE_UNIT_ID = '55555555-5555-5555-5555-555555555555';
const STANDALONE_BOUND_UNIT_ID = 'cccccccc-cccc-cccc-cccc-cccccccccccc';
const STANDALONE_BOUND_WS = 'dddddddd-dddd-dddd-dddd-dddddddddddd';
const SECTOR_WS = '11111111-1111-1111-1111-111111111111';
const COMPANY_WS = '33333333-3333-3333-3333-333333333333';
const UNCLASSIFIED_WS = '66666666-6666-6666-6666-666666666666';

function unitsResponse() {
  return {
    enabled: true,
    units: [
      {
        unit_id: SECTOR_UNIT_ID,
        kind: 'sector',
        slug: 'ai-infrastructure',
        display_name: 'AI Infrastructure',
        owner_user_id: 'local-dev-user',
        workspace_id: SECTOR_WS,
        readable: true,
        delegatable: true,
        auto_tracked: false,
        members: [
          {
            member_id: 'M1',
            name: '某综合科技公司',
            ticker: 'EXAMPLE',
            company_unit_id: MEMBER_COMPANY_UNIT_ID,
            workspace_created: true,
            workspace_id: COMPANY_WS,
            scope: '只计 AI 加速卡收入',
            reason: '',
            valid_until: null,
          },
          {
            member_id: 'M2',
            name: '未覆盖的封装厂商',
            ticker: null,
            company_unit_id: null,
            workspace_created: false,
            workspace_id: null,
            scope: '',
            reason: '',
            valid_until: null,
          },
        ],
      },
      {
        unit_id: MEMBER_COMPANY_UNIT_ID,
        kind: 'company',
        slug: 'example',
        display_name: 'EXAMPLE',
        owner_user_id: 'local-dev-user',
        workspace_id: COMPANY_WS,
        readable: true,
        delegatable: true,
        auto_tracked: true,
        ticker: 'EXAMPLE',
      },
      {
        unit_id: STANDALONE_UNIT_ID,
        kind: 'company',
        slug: 'mega-tech',
        display_name: 'Mega Tech',
        owner_user_id: 'local-dev-user',
        workspace_id: null,
        readable: true,
        delegatable: false,
        auto_tracked: false,
        ticker: 'MEGA',
      },
      {
        // 已绑定工作区的独立公司（codex p4 r1 #1：单元行要有打开入口）
        unit_id: STANDALONE_BOUND_UNIT_ID,
        kind: 'company',
        slug: 'solo-co',
        display_name: 'Solo Co',
        owner_user_id: 'local-dev-user',
        workspace_id: STANDALONE_BOUND_WS,
        readable: true,
        delegatable: true,
        auto_tracked: false,
        ticker: 'SOLO',
      },
    ],
    unclassified: [
      {
        workspace_id: UNCLASSIFIED_WS,
        name: '随便的主题工作区',
        description: '尚未登记覆盖单元',
        updated_at: '2026-10-01T00:00:00+00:00',
      },
    ],
  };
}

function sectorDetailResponse() {
  return {
    unit_id: SECTOR_UNIT_ID,
    kind: 'sector',
    slug: 'ai-infrastructure',
    display_name: 'AI Infrastructure',
    owner_user_id: 'local-dev-user',
    workspace_id: SECTOR_WS,
    readable: true,
    delegatable: true,
    auto_tracked: false,
    sector: {
      docs: {
        sector: {
          version_no: 2,
          name: 'AI Infrastructure',
          name_zh: 'AI 基础设施',
          definition: '算力、网络与智算中心的供给链条',
          boundaries: '不含终端消费电子',
          one_liner: '算力供给仍紧张',
          coverage_stage: 'monitoring',
          coverage_kind: 'sector',
          wiki_slug: 'ai-infra',
          research_tz: 'America/New_York',
          regions: ['全球'],
          markets: ['US equities'],
        },
        thesis: {
          version_no: 3,
          one_liner: '算力缺口延续到 2027',
          aggregate_status: 'intact',
          stance: 'constructive',
          conviction: 'high',
          pillars: [
            { id: 'P1', claim: '训练需求仍在上行', priority: 'core', status: 'supported' },
          ],
        },
        drivers: {
          version_no: 2,
          as_of: '2026-10-20',
          drivers: [
            {
              id: 'K1',
              name: '全球 AI 服务器出货量',
              kind: 'volume',
              updated_at: '2026-10-20T00:00:00Z',
              leading_indicators: [
                {
                  id: 'K1.1',
                  name: 'CoWoS 月产能',
                  frequency: 'monthly',
                  good_direction: 'up',
                  latest: {
                    value: 35,
                    unit: 'count_mn',
                    period: '2026-09',
                    as_of: '2026-10-05T00:00:00Z',
                  },
                },
                {
                  id: 'K1.2',
                  name: '尚无读数的 HBM 出货指标',
                  frequency: 'quarterly',
                  good_direction: 'up',
                  latest: null,
                },
              ],
            },
          ],
        },
        agenda_version_no: 4,
      },
      agenda_todos: {
        questions: [
          {
            id: 'Q-001',
            question: 'HBM 供给何时宽松？',
            why: '',
            value: 'high',
            urgency: 'this_week',
            origin: 'analyst',
            waiting_pm: false,
          },
        ],
        validations: [{ id: 'V1', what: '季度 capex 口径核对', due: '2026-11-05' }],
        events: [],
      },
      members: [
        {
          member_id: 'M1',
          name: '某综合科技公司',
          ticker: 'EXAMPLE',
          company_unit_id: MEMBER_COMPANY_UNIT_ID,
          workspace_created: true,
          workspace_id: COMPANY_WS,
          scope: '',
          reason: '',
          valid_until: null,
        },
        {
          member_id: 'M2',
          name: '未覆盖的封装厂商',
          ticker: null,
          company_unit_id: null,
          workspace_created: false,
          workspace_id: null,
          scope: '',
          reason: '',
          valid_until: null,
        },
      ],
      evidence_gaps: [
        {
          request_id: '77777777-7777-7777-7777-777777777777',
          question: '需要 CoWoS 月度产能数据',
          evidence_needed: [],
          completion_condition: '拿到 10 月数据',
          target_workspace_id: null,
          status: 'open',
          created_at: '2026-10-10T00:00:00+00:00',
        },
      ],
      ledger_gaps: [
        {
          request_id: '88888888-8888-8888-8888-888888888888',
          kind: 'linkage_retry',
          question: '行业支柱 P1 改版后的联动补发',
          target_workspace_id: COMPANY_WS,
          status: 'gap',
          created_at: '2026-10-12T00:00:00+00:00',
          updated_at: '2026-10-12T00:00:00+00:00',
        },
      ],
      pending_total: 2,
    },
  };
}

function companyDetailResponse() {
  return {
    unit_id: MEMBER_COMPANY_UNIT_ID,
    kind: 'company',
    slug: 'example',
    display_name: 'EXAMPLE',
    owner_user_id: 'local-dev-user',
    workspace_id: COMPANY_WS,
    readable: true,
    delegatable: true,
    auto_tracked: true,
    ticker: 'EXAMPLE',
    company: {
      docs: {
        thesis: {
          version_no: 5,
          one_liner: 'AI 收入占比继续提升',
          aggregate_status: 'intact',
          stance: 'positive',
          conviction: 'medium',
          pillars: [],
        },
      },
      sectors: [
        {
          unit_id: SECTOR_UNIT_ID,
          slug: 'ai-infrastructure',
          display_name: 'AI Infrastructure',
          member_id: 'M1',
          scope: '只计 AI 加速卡收入',
          valid_until: null,
          readable: true,
          delegatable: true,
          workspace_id: SECTOR_WS,
        },
      ],
      re_review: [
        {
          id: 'Q-002',
          question: '待复核：行业支柱 P1（AI Infrastructure）已更新到 v3，本公司论点仍采用旧版本（assumes 引用）。',
          value: 'high',
          urgency: 'this_week',
          status: 'open',
          source_ref: {
            workspace_id: SECTOR_WS,
            doc_key: 'thesis',
            version_no: 3,
            entry_id: 'P1',
            relation: 'assumes',
          },
        },
      ],
    },
  };
}

function installQueries(data: ReturnType<typeof unitsResponse> | { enabled: false }) {
  mockUnitsQuery.mockReturnValue({
    data,
    isLoading: false,
    isFetching: false,
    error: null,
    refetch: vi.fn(),
  });
  // 详情查询按 unitId 返回对应替身（行业 / 成员公司）
  mockDetailQuery.mockImplementation((unitId: string | null) => ({
    data:
      unitId === null
        ? undefined
        : unitId === SECTOR_UNIT_ID
          ? sectorDetailResponse()
          : companyDetailResponse(),
    isLoading: false,
    isFetching: false,
    error: null,
  }));
}

describe('DaltonCoverageCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installQueries(unitsResponse());
  });

  it('renders nothing while the backend flag is off (no sector copy at all)', () => {
    installQueries({ enabled: false });
    const { container } = renderWithProviders(<DaltonCoverageCard />);
    expect(screen.queryByTestId('dalton-coverage-card')).not.toBeInTheDocument();
    expect(container.textContent).toBe('');
  });

  it('renders nothing before the flag state has loaded', () => {
    mockUnitsQuery.mockReturnValue({
      data: undefined,
      isLoading: true,
      isFetching: false,
      error: null,
      refetch: vi.fn(),
    });
    const { container } = renderWithProviders(<DaltonCoverageCard />);
    expect(screen.queryByTestId('dalton-coverage-card')).not.toBeInTheDocument();
    expect(container.textContent).toBe('');
  });

  it('shows the three entries: sectors, standalone companies, unclassified workspaces', async () => {
    renderWithProviders(<DaltonCoverageCard />);

    // 行业入口：行业单元 + 独立公司被排除在行业成员之外
    expect(await screen.findByTestId('coverage-tab-sectors')).toBeInTheDocument();
    expect(screen.getByTestId(`coverage-unit-${SECTOR_UNIT_ID}`)).toBeInTheDocument();

    // 独立公司：mega-tech 未被任何行业列为成员；成员公司 EXAMPLE 不在此列
    fireEvent.click(screen.getByTestId('coverage-tab-companies'));
    expect(screen.getByTestId(`coverage-unit-${STANDALONE_UNIT_ID}`)).toBeInTheDocument();
    expect(
      screen.queryByTestId(`coverage-unit-${MEMBER_COMPANY_UNIT_ID}`),
    ).not.toBeInTheDocument();

    // 未分类工作区
    fireEvent.click(screen.getByTestId('coverage-tab-unclassified'));
    expect(
      screen.getByTestId(`coverage-unclassified-${UNCLASSIFIED_WS}`),
    ).toBeInTheDocument();
    expect(screen.getByText('随便的主题工作区')).toBeInTheDocument();
  });

  it('marks members without company coverage and never forces creating one', async () => {
    renderWithProviders(<DaltonCoverageCard />);

    fireEvent.click(await screen.findByTestId(`coverage-unit-toggle-${SECTOR_UNIT_ID}`));
    await waitFor(() => {
      expect(screen.getByTestId(`coverage-detail-${SECTOR_UNIT_ID}`)).toBeInTheDocument();
    });
    // 已建公司覆盖 / 未建立公司覆盖 两种标识同时出现
    expect(screen.getByTestId('coverage-member-covered-M1')).toBeInTheDocument();
    expect(screen.getByTestId('coverage-member-uncovered-M2')).toBeInTheDocument();
    // 未建立覆盖的成员没有强制创建入口，也没有可展开的公司详情
    expect(screen.queryByTestId('coverage-member-toggle-M2')).not.toBeInTheDocument();
    // 详情段落：论点 / 指标 / 议程待办 / 证据缺口 / 跨层台账 gap（jsdom 下
    // 组件文案是 en-US，断言只锚定 fixture 内容与 testid，不锚定界面语言）
    expect(screen.getByText('算力缺口延续到 2027')).toBeInTheDocument();
    expect(screen.getByText('全球 AI 服务器出货量')).toBeInTheDocument();
    expect(screen.getByTestId('coverage-agenda-question-Q-001')).toHaveTextContent(
      'HBM 供给何时宽松？',
    );
    expect(
      screen.getByTestId('coverage-evidence-gap-77777777-7777-7777-7777-777777777777'),
    ).toHaveTextContent('需要 CoWoS 月度产能数据');
    expect(
      screen.getByTestId('coverage-ledger-gap-88888888-8888-8888-8888-888888888888'),
    ).toHaveTextContent('行业支柱 P1 改版后的联动补发');
  });

  it('shows related sectors and pending re-review assumptions on a member company', async () => {
    renderWithProviders(<DaltonCoverageCard />);

    // 成员公司不在"独立公司"入口（它是行业成员）
    fireEvent.click(await screen.findByTestId('coverage-tab-companies'));
    expect(
      screen.queryByTestId(`coverage-unit-${MEMBER_COMPANY_UNIT_ID}`),
    ).not.toBeInTheDocument();

    // 从行业详情的成员行展开公司详情
    fireEvent.click(screen.getByTestId('coverage-tab-sectors'));
    fireEvent.click(screen.getByTestId(`coverage-unit-toggle-${SECTOR_UNIT_ID}`));
    await waitFor(() => {
      expect(screen.getByTestId(`coverage-detail-${SECTOR_UNIT_ID}`)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('coverage-member-toggle-M1'));
    await waitFor(() => {
      expect(
        screen.getByTestId(`coverage-detail-${MEMBER_COMPANY_UNIT_ID}`),
      ).toBeInTheDocument();
    });
    // 关联行业 + 待复核假设（P2 联动生成的 agenda 待办）
    expect(
      screen.getByTestId(`coverage-related-sector-${SECTOR_UNIT_ID}`),
    ).toBeInTheDocument();
    expect(screen.getByTestId('coverage-re-review-Q-002')).toBeInTheDocument();
  });

  it('shows an open-workspace entry on bound unit rows, none on unbound ones (r1 #1)', async () => {
    renderWithProviders(<DaltonCoverageCard />);

    // 没有成员的行业：单元行自身提供打开入口
    expect(
      await screen.findByTestId(`coverage-open-workspace-${SECTOR_WS}`),
    ).toBeInTheDocument();

    // 独立公司：已绑定的有入口，未绑定的没有
    fireEvent.click(screen.getByTestId('coverage-tab-companies'));
    expect(
      screen.getByTestId(`coverage-open-workspace-${STANDALONE_BOUND_WS}`),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('coverage-open-workspace-null'),
    ).not.toBeInTheDocument();
    const megaRow = screen.getByTestId(`coverage-unit-${STANDALONE_UNIT_ID}`);
    expect(megaRow.querySelector('[data-testid^="coverage-open-workspace-"]')).toBeNull();
  });

  it('marks waiting-PM agenda questions instead of dropping them (r1 #3)', async () => {
    const detail = sectorDetailResponse();
    detail.sector!.agenda_todos!.questions.push({
      id: 'Q-003',
      question: '已转交 PM 的行业问题',
      why: '',
      value: 'high',
      urgency: 'this_week',
      origin: 'analyst',
      waiting_pm: true,
    });
    mockDetailQuery.mockImplementation((unitId: string | null) => ({
      data: unitId === SECTOR_UNIT_ID ? detail : undefined,
      isLoading: false,
      isFetching: false,
      error: null,
    }));

    renderWithProviders(<DaltonCoverageCard />);
    fireEvent.click(await screen.findByTestId(`coverage-unit-toggle-${SECTOR_UNIT_ID}`));

    await waitFor(() => {
      expect(screen.getByTestId('coverage-waiting-pm-Q-003')).toBeInTheDocument();
    });
    expect(screen.getByTestId('coverage-agenda-question-Q-003')).toHaveTextContent(
      '已转交 PM 的行业问题',
    );
    // 非等待的问题不带标识
    expect(
      screen.queryByTestId('coverage-waiting-pm-Q-001'),
    ).not.toBeInTheDocument();
  });

  it('lists leading indicators with their latest reading under each driver (r1 #4)', async () => {
    renderWithProviders(<DaltonCoverageCard />);
    fireEvent.click(await screen.findByTestId(`coverage-unit-toggle-${SECTOR_UNIT_ID}`));

    await waitFor(() => {
      expect(screen.getByTestId('coverage-driver-K1')).toBeInTheDocument();
    });
    const withReading = screen.getByTestId('coverage-indicator-K1.1');
    expect(withReading).toHaveTextContent('CoWoS 月产能');
    expect(withReading).toHaveTextContent('35');
    expect(withReading).toHaveTextContent('count_mn');
    expect(withReading).toHaveTextContent('2026-09');
    expect(withReading).toHaveTextContent('2026-10-05');
    // 无读数的指标只显示名字
    const withoutReading = screen.getByTestId('coverage-indicator-K1.2');
    expect(withoutReading).toHaveTextContent('尚无读数的 HBM 出货指标');
  });

  it('notes truncation when the pending ledger total exceeds the shown rows (r1 #2)', async () => {
    const detail = sectorDetailResponse();
    detail.sector!.pending_total = 7; // 展示 2 条（1 evidence + 1 ledger）
    mockDetailQuery.mockImplementation((unitId: string | null) => ({
      data: unitId === SECTOR_UNIT_ID ? detail : undefined,
      isLoading: false,
      isFetching: false,
      error: null,
    }));

    renderWithProviders(<DaltonCoverageCard />);
    fireEvent.click(await screen.findByTestId(`coverage-unit-toggle-${SECTOR_UNIT_ID}`));

    await waitFor(() => {
      expect(
        screen.getByTestId(`coverage-pending-truncated-${SECTOR_UNIT_ID}`),
      ).toBeInTheDocument();
    });
    // jsdom 下组件测试以 en-US 渲染（sector 摘要名同理由 i18n 提供）
    expect(
      screen.getByTestId(`coverage-pending-truncated-${SECTOR_UNIT_ID}`),
    ).toHaveTextContent('Showing first 2 of 7 pending items');
  });
});
