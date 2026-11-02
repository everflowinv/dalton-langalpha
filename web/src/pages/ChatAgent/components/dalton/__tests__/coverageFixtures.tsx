import React, { type ReactElement } from 'react';
import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router';
import type { DaltonCoverageUnitsResponse } from '@/lib/dalton/coverage';

// 与后端 /api/v1/dalton/coverage/units 的真实响应逐字段一致
// （src/dalton/autonomy/units_view.py；成员 workspace_id 已按查看者过滤，
// 有值即代表本人可读）。字段与 DaltonCoverageCard.test.tsx 的 fixture 同源。
export const SECTOR_UNIT_ID = '22222222-2222-2222-2222-222222222222';
export const MEMBER_COMPANY_UNIT_ID = '44444444-4444-4444-4444-444444444444';
export const STANDALONE_UNIT_ID = '55555555-5555-5555-5555-555555555555';
export const STANDALONE_BOUND_UNIT_ID = 'cccccccc-cccc-cccc-cccc-cccccccccccc';
export const SECTOR_WS = '11111111-1111-1111-1111-111111111111';
export const COMPANY_WS = '33333333-3333-3333-3333-333333333333';
export const STANDALONE_BOUND_WS = 'dddddddd-dddd-dddd-dddd-dddddddddddd';
export const UNCLASSIFIED_WS = '66666666-6666-6666-6666-666666666666';
// 只可委派、不可读的同事行业（d025：进入 units 但 readable:false）。
export const DELEG_SECTOR_UNIT_ID = '77777777-7777-7777-7777-777777777777';
export const DELEG_SECTOR_WS = '88888888-8888-8888-8888-888888888888';
export const DELEG_MEMBER_CO_UNIT_ID = '99999999-9999-9999-9999-999999999999';
// 未绑定工作区又没有成员的本人行业：侧栏里的"死行"。
export const INERT_SECTOR_UNIT_ID = 'aaaaaaa2-2222-2222-2222-222222222222';

export function buildUnitsResponse(
  overrides: Partial<Extract<DaltonCoverageUnitsResponse, { enabled: true }>> = {},
): DaltonCoverageUnitsResponse {
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
            name: 'Example Corp',
            ticker: 'EXAMPLE',
            company_unit_id: MEMBER_COMPANY_UNIT_ID,
            workspace_created: true,
            workspace_id: COMPANY_WS,
            scope: 'AI accelerators only',
            reason: '',
            valid_until: null,
          },
          {
            member_id: 'M2',
            name: 'Uncovered Packaging Co',
            ticker: null,
            company_unit_id: null,
            workspace_created: false,
            workspace_id: null,
            scope: '',
            reason: '',
            valid_until: null,
          },
          {
            // 已建公司工作区，但查看者不可读：后端把 workspace_id 过滤为 null
            member_id: 'M3',
            name: 'Hidden Memory Co',
            ticker: 'HIDDEN',
            company_unit_id: DELEG_MEMBER_CO_UNIT_ID,
            workspace_created: true,
            workspace_id: null,
            scope: '',
            reason: '',
            valid_until: null,
          },
        ],
      },
      {
        // 同事的行业单元：可见但只能委派（web 不可读），不进侧栏导航树
        unit_id: DELEG_SECTOR_UNIT_ID,
        kind: 'sector',
        slug: 'colleague-energy',
        display_name: 'Colleague Energy',
        owner_user_id: 'u-colleague',
        workspace_id: DELEG_SECTOR_WS,
        readable: false,
        delegatable: true,
        auto_tracked: false,
        members: [
          {
            member_id: 'MD1',
            name: 'Hidden Memory Co',
            ticker: 'HIDDEN',
            company_unit_id: DELEG_MEMBER_CO_UNIT_ID,
            workspace_created: true,
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
        display_name: 'Example',
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
        // 未绑定工作区、没有成员：树里不可点击的死行
        unit_id: INERT_SECTOR_UNIT_ID,
        kind: 'sector',
        slug: 'empty-sector',
        display_name: 'Empty Sector',
        owner_user_id: 'local-dev-user',
        workspace_id: null,
        readable: true,
        delegatable: false,
        auto_tracked: false,
        members: [],
      },
      {
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
        name: 'Random theme workspace',
        description: null,
        updated_at: '2026-10-01T00:00:00+00:00',
      },
    ],
    ...overrides,
  };
}

/** Renders inside a MemoryRouter and reports the current URL, so a component's
 *  navigate() calls can be asserted without mounting real routes. */
export function renderWithRouterProbe(
  ui: ReactElement,
  queryClient: QueryClient,
): ReturnType<typeof render> & { currentUrl: () => string } {
  const seen: string[] = [];
  function LocationProbe() {
    const location = useLocation();
    seen.push(`${location.pathname}${location.search}${location.hash}`);
    return null;
  }
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/chat']}>
        <LocationProbe />
        {ui}
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, currentUrl: () => seen[seen.length - 1] };
}
