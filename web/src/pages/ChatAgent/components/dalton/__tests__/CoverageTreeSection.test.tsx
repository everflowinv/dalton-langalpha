import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { createTestQueryClient } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import { CoverageTreeSection } from '../CoverageTreeSection';
import {
  buildUnitsResponse,
  COMPANY_WS,
  DELEG_SECTOR_UNIT_ID,
  INERT_SECTOR_UNIT_ID,
  renderWithRouterProbe,
  SECTOR_UNIT_ID,
  SECTOR_WS,
  STANDALONE_BOUND_UNIT_ID,
  STANDALONE_BOUND_WS,
  STANDALONE_UNIT_ID,
} from './coverageFixtures';

// 用例都走缓存种子（setQueryData）；无数据/刷新失败用例由它兜底拒绝，不触网。
vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(() => Promise.reject(new Error('no network in tests'))),
  },
}));

function seed(data: ReturnType<typeof buildUnitsResponse> | { enabled: false }) {
  const queryClient = createTestQueryClient();
  queryClient.setQueryData(queryKeys.dalton.coverageUnits(), data);
  return queryClient;
}

beforeEach(() => {
  localStorage.clear();
});

describe('CoverageTreeSection', () => {
  it('renders nothing when the sector layer flag is off', () => {
    const queryClient = seed({ enabled: false });
    renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );
    expect(screen.queryByTestId('coverage-tree-section')).toBeNull();
  });

  it('renders nothing when the listing has no units', () => {
    const queryClient = seed(buildUnitsResponse({ units: [], unclassified: [] }));
    renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );
    expect(screen.queryByTestId('coverage-tree-section')).toBeNull();
  });

  it('renders nothing while the query has no data', () => {
    const queryClient = createTestQueryClient(); // nothing seeded → query idle
    renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );
    expect(screen.queryByTestId('coverage-tree-section')).toBeNull();
  });

  it('drops the whole section when a refresh fails after a success', async () => {
    const queryClient = seed(buildUnitsResponse());
    renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );
    expect(screen.getByTestId('coverage-tree-section')).toBeInTheDocument();

    // 刷新失败（api mock 拒绝）→ isError → 不显示旧数据
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.dalton.coverageUnits() });
    });
    await waitFor(() => {
      expect(screen.queryByTestId('coverage-tree-section')).toBeNull();
    });
  });

  it('lists only readable units: a delegatable-only colleague sector never appears', () => {
    const queryClient = seed(buildUnitsResponse());
    renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );

    expect(screen.getByTestId(`coverage-tree-sector-${SECTOR_UNIT_ID}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`coverage-tree-sector-${DELEG_SECTOR_UNIT_ID}`)).toBeNull();
    expect(screen.queryByText('Colleague Energy')).toBeNull();
  });

  it('expands a sector into its member companies', () => {
    const queryClient = seed(buildUnitsResponse());
    renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );

    // 行业行在列，成员默认收起（当前不在该行业里 → 不自动展开）
    expect(screen.queryByText('Example Corp')).toBeNull();

    const toggle = screen.getByTestId(`coverage-tree-sector-toggle-${SECTOR_UNIT_ID}`);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Example Corp')).toBeInTheDocument();
    expect(screen.getByText('Uncovered Packaging Co')).toBeInTheDocument();
    expect(screen.getByText('EXAMPLE')).toBeInTheDocument();
  });

  it('greys members without a workspace and separates "not yet" from "no access"', () => {
    const queryClient = seed(buildUnitsResponse());
    const { currentUrl } = renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );

    fireEvent.click(screen.getByTestId(`coverage-tree-sector-toggle-${SECTOR_UNIT_ID}`));

    // 有工作区的成员：行点击进入该工作区（nav-panel-row 的 pointer 行）
    fireEvent.click(screen.getByTestId('coverage-tree-member-M1'));
    expect(currentUrl()).toBe(`/chat/${COMPANY_WS}`);

    // 从未建工作区（workspace_created false）→ tooltip"未建工作区"，
    // 行尾不再有文字标注
    const uncovered = screen.getByTestId('coverage-tree-member-M2');
    expect(uncovered).toHaveAttribute('title', 'No workspace');
    expect(uncovered).toHaveClass('coverage-row-inert');
    expect(uncovered).not.toHaveAttribute('role', 'button');
    expect(uncovered.textContent).not.toContain('No workspace');
    fireEvent.click(uncovered);
    expect(currentUrl()).toBe(`/chat/${COMPANY_WS}`);

    // 已建工作区但查看者不可读（workspace_created true、workspace_id 被
    // 后端过滤）→ tooltip"无法访问"
    const hidden = screen.getByTestId('coverage-tree-member-M3');
    expect(hidden).toHaveAttribute('title', 'No access');
    expect(hidden).toHaveClass('coverage-row-inert');
    expect(hidden).not.toHaveAttribute('role', 'button');
  });

  it('renders a sector with neither workspace nor members as an inert row', () => {
    const queryClient = seed(buildUnitsResponse());
    renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );

    const inert = screen.getByTestId(`coverage-tree-sector-${INERT_SECTOR_UNIT_ID}`);
    expect(inert).toHaveAttribute('title', 'No workspace');
    // 不复用 nav-panel-row：无 hover 底色、无 pointer
    // workspace-row metrics kept, interaction removed
    expect(inert).toHaveClass('nav-panel-row', 'coverage-row-inert');
    expect(inert).not.toHaveAttribute('role', 'button');
    expect(inert.querySelector('span')?.style.color).toBe('var(--color-text-quaternary)');
    expect(inert.textContent).toContain('0');
  });

  it('enters a sector workspace by row click and a bound standalone company row', () => {
    const queryClient = seed(buildUnitsResponse());
    const { currentUrl } = renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );

    // 行业绑定了可读工作区：点行进入（点箭头只展开，不导航）
    fireEvent.click(screen.getByTestId(`coverage-tree-sector-${SECTOR_UNIT_ID}`));
    expect(currentUrl()).toBe(`/chat/${SECTOR_WS}`);

    // 已绑定工作区的独立公司行可进入；未绑定的置灰
    fireEvent.click(screen.getByTestId(`coverage-tree-standalone-${STANDALONE_BOUND_UNIT_ID}`));
    // keyboard parity with the workspace rows' handle: focusable button, Enter activates
    const boundRow = screen.getByTestId(`coverage-tree-standalone-${STANDALONE_BOUND_UNIT_ID}`);
    expect(boundRow).toHaveAttribute('role', 'button');
    expect(boundRow).toHaveAttribute('tabIndex', '0');
    fireEvent.keyDown(boundRow, { key: 'Enter' });
    expect(currentUrl()).toBe(`/chat/${STANDALONE_BOUND_WS}`);
    // Enter on the sector's own collapse chevron is the chevron's: the row must
    // not claim it and navigate into the sector workspace.
    const chevron = screen.getByTestId(`coverage-tree-sector-toggle-${SECTOR_UNIT_ID}`);
    fireEvent.keyDown(chevron, { key: 'Enter' });
    expect(currentUrl()).toBe(`/chat/${STANDALONE_BOUND_WS}`);

    const unbound = screen.getByTestId(`coverage-tree-standalone-${STANDALONE_UNIT_ID}`);
    expect(unbound).toHaveAttribute('title', 'No workspace');
    expect(unbound).toHaveClass('coverage-row-inert');
    expect(unbound).not.toHaveAttribute('role', 'button');
  });

  it('auto-expands the sector holding the current workspace and highlights the row', () => {
    const queryClient = seed(buildUnitsResponse());
    renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={COMPANY_WS} />,
      queryClient,
    );

    // 不点任何东西：当前工作区所在的行业已自动展开，成员行标记 data-active，
    // 当前项与工作区行同款（无底色块，只有 data-active 标记）
    const member = screen.getByTestId('coverage-tree-member-M1');
    expect(member).toHaveAttribute('data-active', 'true');
    expect(member.className).not.toContain('bg-foreground/10');
    expect(screen.getByTestId(`coverage-tree-sector-${SECTOR_UNIT_ID}`)).not.toHaveAttribute('data-active');
  });

  it('persists the collapsed state across mounts', () => {
    const queryClient = seed(buildUnitsResponse());
    const first = renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );

    const toggle = screen.getByTestId('coverage-tree-toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(localStorage.getItem('sidebar.coverageTree.collapsed')).toBe('true');
    first.unmount();

    // 重新挂载读到持久化的折叠状态
    renderWithRouterProbe(<CoverageTreeSection currentWorkspaceId={null} />, queryClient);
    expect(screen.getByTestId('coverage-tree-toggle')).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('coverage-tree-body')).toBeNull();
  });

  it('links the unclassified row to the settings coverage card, unclassified tab', () => {
    const queryClient = seed(buildUnitsResponse());
    const { currentUrl } = renderWithRouterProbe(
      <CoverageTreeSection currentWorkspaceId={null} />,
      queryClient,
    );

    const row = screen.getByTestId('coverage-tree-unclassified');
    expect(row.textContent).toContain('1');
    fireEvent.click(row);
    expect(currentUrl()).toBe('/settings?tab=model#dalton-coverage-unclassified');
  });
});
