import { describe, it, expect, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { createTestQueryClient } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import { CoverageAffiliationBar } from '../CoverageAffiliationBar';
import {
  buildUnitsResponse,
  COMPANY_WS,
  renderWithRouterProbe,
  SECTOR_WS,
  STANDALONE_BOUND_WS,
  UNCLASSIFIED_WS,
} from './coverageFixtures';

// 用例都走缓存种子（setQueryData）；兜底拒绝，不触网。
// 门控与 DaltonCoverageCard 一致：只由接口数据驱动，无登录模式门控。
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

describe('CoverageAffiliationBar', () => {
  it('shows the sector line with member chips on a sector workspace', () => {
    const { currentUrl } = renderWithRouterProbe(
      <CoverageAffiliationBar workspaceId={SECTOR_WS} />,
      seed(buildUnitsResponse()),
    );

    const bar = screen.getByTestId('coverage-affiliation-bar');
    expect(bar.textContent).toContain('AI Infrastructure');
    expect(bar.textContent).toContain('3 members');

    // 有工作区的成员 chip 可点击进入：hover 态与"共享设置"按钮同款
    const chips = screen.getAllByTestId('coverage-affiliation-chip');
    expect(chips[0].tagName).toBe('BUTTON');
    expect(chips[0]).toHaveClass('hover:bg-foreground/5');
    fireEvent.click(chips[0]);
    expect(currentUrl()).toBe(`/chat/${COMPANY_WS}`);

    // 未建工作区（workspace_created false）：quaternary 文字、无 hover、
    // tooltip"未建工作区"
    const notCreated = screen.getByTitle('No workspace');
    expect(notCreated.tagName).not.toBe('BUTTON');
    expect(notCreated.style.color).toBe('var(--color-text-quaternary)');
    expect(notCreated.className).not.toContain('hover:bg-foreground/5');

    // 已建但不可读（workspace_id 被后端过滤）：tooltip"无法访问"
    const noAccess = screen.getByTitle('No access');
    expect(noAccess.tagName).not.toBe('BUTTON');
    expect(noAccess.style.color).toBe('var(--color-text-quaternary)');
  });

  it('links a member company workspace back to its readable sector', () => {
    const { currentUrl } = renderWithRouterProbe(
      <CoverageAffiliationBar workspaceId={COMPANY_WS} />,
      seed(buildUnitsResponse()),
    );

    const bar = screen.getByTestId('coverage-affiliation-bar');
    expect(bar.textContent).toContain('Sector');
    fireEvent.click(screen.getByTestId('coverage-affiliation-chip'));
    expect(currentUrl()).toBe(`/chat/${SECTOR_WS}`);
  });

  it('keeps a sector chip plain when its workspace is not readable', () => {
    const base = buildUnitsResponse();
    const enabled = base as Extract<typeof base, { enabled: true }>;
    // 同事的行业单元：可见但 web 只读为 false（delegatable-only）
    enabled.units[0] = {
      ...enabled.units[0],
      readable: false,
      delegatable: true,
    };
    const { currentUrl } = renderWithRouterProbe(
      <CoverageAffiliationBar workspaceId={COMPANY_WS} />,
      seed(base),
    );

    const chip = screen.getByTestId('coverage-affiliation-chip');
    expect(chip.tagName).not.toBe('BUTTON');
    expect(chip.className).not.toContain('hover:bg-foreground/5');
    // 有工作区但不可读 → "无法访问"；未绑定工作区 → "未建工作区"
    expect(chip).toHaveAttribute('title', 'No access');
    expect(currentUrl()).toBe('/chat');
  });

  it('labels a standalone company workspace', () => {
    renderWithRouterProbe(
      <CoverageAffiliationBar workspaceId={STANDALONE_BOUND_WS} />,
      seed(buildUnitsResponse()),
    );
    expect(
      screen.getByTestId('coverage-affiliation-bar').textContent,
    ).toContain('Standalone company');
  });

  it('renders nothing for an unclassified workspace and when the flag is off', () => {
    const queryClient = seed(buildUnitsResponse());
    renderWithRouterProbe(
      <CoverageAffiliationBar workspaceId={UNCLASSIFIED_WS} />,
      queryClient,
    );
    expect(screen.queryByTestId('coverage-affiliation-bar')).toBeNull();

    const offClient = seed({ enabled: false });
    renderWithRouterProbe(
      <CoverageAffiliationBar workspaceId={SECTOR_WS} />,
      offClient,
    );
    expect(screen.queryAllByTestId('coverage-affiliation-bar')).toHaveLength(0);
  });
});
