import { describe, it, expect, vi } from 'vitest';
import React from 'react';
import { render, screen } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { createTestQueryClient } from '@/test/utils';
import { WorkspaceTreeRow } from '../../NavigationRows';
import type { NavWorkspace } from '../../../hooks/useNavigationData';
import {
  deriveCoverageWorkspaceRoles,
  splitCoverageUnits,
} from '@/lib/dalton/coverage';
import {
  buildUnitsResponse,
  COMPANY_WS,
  DELEG_SECTOR_UNIT_ID,
  INERT_SECTOR_UNIT_ID,
  SECTOR_UNIT_ID,
  SECTOR_WS,
  STANDALONE_BOUND_WS,
} from './coverageFixtures';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function makeWs(wsId: string, name: string): NavWorkspace {
  return { workspace_id: wsId, name, is_home: false };
}

const RENAME_STUB = {
  active: false,
  value: '',
  inputRef: React.createRef<HTMLInputElement>(),
  onChange: vi.fn(),
  onCommit: vi.fn(),
  onCancel: vi.fn(),
  onStart: vi.fn(),
  enabled: false,
};

const ACTIONS_STUB = {
  openDuplicate: vi.fn(),
  openMove: vi.fn(),
  openDelete: vi.fn(),
};

// NavigationPanel 的循环按 wsId 把角色解析成原始字符串再传行；这里用
// 同一规则构造 prop（角色 → 行业 chip 文案 / 行业名拼接 / undefined）。
function chipOf(roles: ReturnType<typeof deriveCoverageWorkspaceRoles>, wsId: string) {
  const role = roles.get(wsId);
  if (role?.kind === 'sector') return 'dalton.coverage.sectorChip';
  if (role?.kind === 'member' && role.sectorNames.length > 0) return role.sectorNames.join('、');
  return undefined;
}

function renderRow(wsId: string, coverageChip?: string) {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter>
        <WorkspaceTreeRow
          ws={makeWs(wsId, `Row ${wsId.slice(0, 8)}`)}
          isExpanded={false}
          isCurrent={false}
          isMobile={false}
          dragDisabled
          threadsData={{ threads: [] }}
          currentThreadId={null}
          expandedThreadIds={new Set()}
          onSelectAgent={vi.fn()}
          rename={RENAME_STUB}
          onToggleWorkspace={vi.fn()}
          onToggleThread={vi.fn()}
          onNavigateThread={vi.fn()}
          wsActions={ACTIONS_STUB}
          coverageChip={coverageChip}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function fixtureUnits() {
  return (buildUnitsResponse() as Extract<
    ReturnType<typeof buildUnitsResponse>,
    { enabled: true }
  >).units;
}

describe('deriveCoverageWorkspaceRoles / splitCoverageUnits', () => {
  it('classifies sector, member and standalone workspaces from the listing', () => {
    const units = fixtureUnits();
    const { sectors, standaloneCompanies } = splitCoverageUnits(units);
    expect(sectors.map((u) => u.unit_id)).toEqual([
      SECTOR_UNIT_ID,
      DELEG_SECTOR_UNIT_ID,
      INERT_SECTOR_UNIT_ID,
    ]);
    // 被（任一可见）行业列为成员的公司不是独立公司；未绑定工作区的单元不进角色映射
    expect(standaloneCompanies.map((u) => u.slug).sort()).toEqual(['mega-tech', 'solo-co']);

    const roles = deriveCoverageWorkspaceRoles(units);
    expect(roles.get(SECTOR_WS)?.kind).toBe('sector');
    const member = roles.get(COMPANY_WS);
    expect(member?.kind).toBe('member');
    if (member?.kind === 'member') {
      expect(member.sectorNames).toEqual(['AI Infrastructure']);
      expect(member.sectors[0].workspaceId).toBe(SECTOR_WS);
    }
    expect(roles.get(STANDALONE_BOUND_WS)?.kind).toBe('standalone');
    expect(roles.has('aaaaaaa1-1111-1111-1111-111111111111')).toBe(false);
  });

  it('returns one shared constant for empty results (memo-stable reference)', () => {
    expect(deriveCoverageWorkspaceRoles([])).toBe(deriveCoverageWorkspaceRoles([]));
  });
});

describe('WorkspaceTreeRow coverage chip', () => {
  it('marks a sector workspace with the sector chip', () => {
    renderRow(SECTOR_WS, chipOf(deriveCoverageWorkspaceRoles(fixtureUnits()), SECTOR_WS));
    expect(screen.getByTestId('coverage-role-chip').textContent).toBe(
      'dalton.coverage.sectorChip',
    );
  });

  it('marks a member company workspace with its sector name', () => {
    renderRow(COMPANY_WS, chipOf(deriveCoverageWorkspaceRoles(fixtureUnits()), COMPANY_WS));
    expect(screen.getByTestId('coverage-role-chip').textContent).toBe('AI Infrastructure');
  });

  it('renders no chip when the prop is undefined (plain row / flag off)', () => {
    renderRow('aaaaaaa1-1111-1111-1111-111111111111', undefined);
    expect(screen.queryByTestId('coverage-role-chip')).toBeNull();
  });

  it('renders no chip for a standalone company workspace', () => {
    renderRow(
      STANDALONE_BOUND_WS,
      chipOf(deriveCoverageWorkspaceRoles(fixtureUnits()), STANDALONE_BOUND_WS),
    );
    expect(screen.queryByTestId('coverage-role-chip')).toBeNull();
  });
});
