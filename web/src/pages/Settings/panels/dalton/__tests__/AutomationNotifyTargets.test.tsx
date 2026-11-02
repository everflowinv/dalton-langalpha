import React from 'react';
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { renderWithProviders } from '@/test/utils';
import { ApiError } from '@/api/client';
import { AutomationNotifyTargets, resolveNotifyRouteEntry } from '../AutomationNotifyTargets';
import type { DaltonFeishuGroupsResponse } from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  preferences: {
    model_preference: {} as Record<string, unknown>,
    other_preference: {} as Record<string, unknown>,
  },
  isLoading: false,
  isLoaded: true,
  groups: [] as Array<{ chat_id: string; name: string }>,
  groupsError: null as ApiError | null,
  fetchCount: 0,
  listeners: new Set<() => void>(),
  mutateAsync: vi.fn(async (patch: Record<string, unknown>) => {
    const mp = (patch.model_preference ?? {}) as Record<string, unknown>;
    h.preferences = {
      ...h.preferences,
      model_preference: {
        ...h.preferences.model_preference,
        ...mp,
      },
    };
    for (const listener of h.listeners) listener();
    return h.preferences;
  }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => {
    const [, forceRender] = React.useState(0);
    React.useEffect(() => {
      const cb = () => forceRender((n) => n + 1);
      h.listeners.add(cb);
      return () => {
        h.listeners.delete(cb);
      };
    }, []);
    return {
      preferences: h.preferences,
      isLoading: h.isLoading,
      isLoaded: h.isLoaded,
    };
  },
}));

vi.mock('@/hooks/useUpdatePreferences', () => ({
  PREFERENCE_MUTATION_KEY: ['user-preferences'],
  useUpdatePreferences: () => ({
    mutate: vi.fn(),
    mutateAsync: h.mutateAsync,
  }),
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  // The selector fetches the PM-only group list; the admin case answers a
  // list, the non-admin case the endpoint's own 403.
  fetchDaltonFeishuGroups: async (): Promise<DaltonFeishuGroupsResponse> => {
    h.fetchCount += 1;
    if (h.groupsError) throw h.groupsError;
    return { ok: true, groups: h.groups };
  },
}));

// Radix Popover's DismissableLayer touches pointer-capture APIs jsdom omits;
// without them the popover content never mounts (SourcesPanel.test.tsx pattern).
beforeAll(() => {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
    Element.prototype.setPointerCapture = () => {};
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
});

const AUTOMATION = {
  name: 'dalton-googl-earn-2026q1-t0',
  dalton_notify_category: 'earnings_t0',
} as const;

const PERSONAL_TARGETS = ['feishu:user:ou_me_1', 'feishu:user:ou_backup_2'];
const PERSONAL_LABELS: Record<string, string> = {
  'feishu:user:ou_me_1': '我本人',
  'feishu:user:ou_backup_2': '备用',
};
const GROUPS = [
  { chat_id: 'oc_research_group', name: '核心投研群' },
  { chat_id: 'oc_ops_group', name: '运营群' },
];

/** The upstream page-test shape: QueryClient + Router, and no AuthProvider. */
type SelectorProps = React.ComponentProps<typeof AutomationNotifyTargets>;

function renderSelector(props: SelectorProps = { automation: AUTOMATION }) {
  return renderWithProviders(<AutomationNotifyTargets {...props} />);
}

function triggerButton() {
  return screen.getByRole('button', { name: '推送目标 dalton-googl-earn-2026q1-t0' });
}

function refusal(status: number): ApiError {
  return new ApiError('Request failed with status code ' + status, 'ERR_BAD_REQUEST', {
    data: {},
    status,
    statusText: '',
    headers: {},
  });
}

function prefsWith(overrides: Record<string, unknown>) {
  return {
    model_preference: {
      dalton_notify_enabled: true,
      dalton_notify_targets: PERSONAL_TARGETS,
      dalton_notify_target_labels: PERSONAL_LABELS,
      ...overrides,
    },
    other_preference: {},
  };
}

describe('resolveNotifyRouteEntry (backend key precedence)', () => {
  const routes = {
    'category:earnings_t0': ['feishu:chat:oc_from_category'],
    'dalton-googl-earn-2026q1-t0': ['feishu:user:ou_from_task'],
  };
  it('prefers the exact task name over the category key', () => {
    expect(resolveNotifyRouteEntry({ ...AUTOMATION }, routes)).toEqual({
      list: ['feishu:user:ou_from_task'],
      source: 'task',
    });
  });
  it('falls back to the category key when no task entry exists', () => {
    const withoutTask = { 'category:earnings_t0': routes['category:earnings_t0'] };
    expect(resolveNotifyRouteEntry({ ...AUTOMATION }, withoutTask)).toEqual({
      list: ['feishu:chat:oc_from_category'],
      source: 'category',
    });
  });
  it('returns null when neither exists', () => {
    expect(resolveNotifyRouteEntry({ ...AUTOMATION }, {})).toBeNull();
  });
});

describe('AutomationNotifyTargets', () => {
  beforeEach(() => {
    h.preferences = prefsWith({});
    h.isLoading = false;
    h.isLoaded = true;
    h.groups = [];
    h.groupsError = null;
    h.fetchCount = 0;
    h.mutateAsync.mockClear();
  });

  it('renders nothing without a category, without the master switch, or without targets', () => {
    const { container, rerender } = renderSelector({
      automation: { name: 'dalton-googl-watch', dalton_notify_category: null },
    });
    expect(container).toBeEmptyDOMElement();

    h.preferences = {
      model_preference: { dalton_notify_enabled: false, dalton_notify_targets: PERSONAL_TARGETS },
      other_preference: {},
    };
    rerender(<AutomationNotifyTargets automation={AUTOMATION} />);
    expect(container).toBeEmptyDOMElement();
  });

  // No AuthProvider wraps anything in this file (renderWithProviders mounts
  // none): that is exactly the upstream Automations page test setup, and the
  // selector must render there (personal targets only) instead of throwing.
  it('renders without an AuthProvider and without group data as personal-only, not throwing', () => {
    h.groups = [];
    renderSelector();
    expect(triggerButton()).toHaveTextContent('仅个人目标');
  });

  it('stays personal-only when the group endpoint refuses with 403 (non-admin)', async () => {
    h.groupsError = refusal(403);
    renderSelector();
    await act(async () => {
      fireEvent.click(triggerButton());
    });
    expect(screen.getByText('备用')).toBeInTheDocument();
    expect(screen.queryByText('飞书群')).not.toBeInTheDocument();
  });

  it('defaults to 仅个人目标 and submits a personal subset from the grouped options', async () => {
    renderSelector();
    expect(triggerButton()).toHaveTextContent('仅个人目标');

    await act(async () => {
      fireEvent.click(triggerButton());
    });
    expect(screen.getByText('备用')).toBeInTheDocument();
    expect(screen.getByText('我本人')).toBeInTheDocument();
    expect(screen.queryByText('飞书群')).not.toBeInTheDocument();

    // With no route every personal target is checked (that is what
    // delivers); unchecking one saves the remaining explicit list.
    await act(async () => {
      fireEvent.click(screen.getByRole('checkbox', { name: '个人目标 feishu:user:ou_me_1' }));
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': ['feishu:user:ou_backup_2'] },
        },
      });
    });
    expect(triggerButton()).toHaveTextContent('备用');
  });

  it('shows an explicit empty route as 不推送 (Not pushed) and re-enables on any check', async () => {
    h.preferences = prefsWith({
      dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': [] },
    });
    renderSelector();
    expect(triggerButton()).toHaveTextContent('Not pushed');

    await act(async () => {
      fireEvent.click(triggerButton());
    });
    // All checks off; the user opts back in by checking one target.
    expect(screen.getByRole('checkbox', { name: '个人目标 feishu:user:ou_me_1' })).toHaveAttribute(
      'aria-checked',
      'false',
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('checkbox', { name: '个人目标 feishu:user:ou_me_1' }));
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': ['feishu:user:ou_me_1'] },
        },
      });
    });
  });

  it('shows a category route as inherited and saves an explicit task list on edit', async () => {
    h.preferences = prefsWith({
      dalton_notify_routes: { 'category:earnings_t0': ['feishu:user:ou_me_1'] },
    });
    renderSelector();
    // The collapsed state reflects what actually delivers: the category route.
    expect(triggerButton()).toHaveTextContent('我本人');

    await act(async () => {
      fireEvent.click(triggerButton());
    });
    expect(screen.getByTestId('dalton-notify-inherits-category')).toHaveTextContent(
      'Inherits category settings',
    );
    expect(screen.getByRole('checkbox', { name: '个人目标 feishu:user:ou_me_1' })).toHaveAttribute(
      'aria-checked',
      'true',
    );

    // An edit saves an explicit task-level list — null would fall back to the
    // category route and undo it.
    await act(async () => {
      fireEvent.click(screen.getByRole('checkbox', { name: '个人目标 feishu:user:ou_me_1' }));
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': [] },
        },
      });
    });
  });

  it('offers a 飞书群 group section once groups are discovered and submits a mixed route', async () => {
    h.groups = GROUPS;
    h.preferences = prefsWith({
      dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': ['feishu:user:ou_me_1'] },
    });
    renderSelector();
    expect(triggerButton()).toHaveTextContent('我本人');

    await act(async () => {
      fireEvent.click(triggerButton());
    });
    expect(await screen.findByText('飞书群')).toBeInTheDocument();
    expect(screen.getByText('核心投研群')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole('checkbox', { name: '飞书群 核心投研群' }));
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_routes: {
            'dalton-googl-earn-2026q1-t0': ['feishu:user:ou_me_1', 'feishu:chat:oc_research_group'],
          },
        },
      });
    });
    expect(triggerButton()).toHaveTextContent('我本人、核心投研群');
  });

  it('shows group names in the collapsed label for a saved group route', async () => {
    h.groups = GROUPS;
    h.preferences = prefsWith({
      dalton_notify_routes: {
        'dalton-googl-earn-2026q1-t0': ['feishu:chat:oc_research_group'],
      },
    });
    renderSelector();
    // selectedChats > 0 fetches the group list without opening the popover,
    // so the collapsed label shows the group's name, never a bare oc_… id.
    await waitFor(() => {
      expect(triggerButton()).toHaveTextContent('核心投研群');
    });
  });

  it('still renders for a user with no personal targets once groups are discovered', async () => {
    h.groups = GROUPS;
    h.preferences = prefsWith({
      dalton_notify_targets: [],
      dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': ['feishu:chat:oc_research_group'] },
    });
    renderSelector();
    await waitFor(() => {
      expect(triggerButton()).toHaveTextContent('核心投研群');
    });
  });

  it('stays hidden with no personal targets and no groups (the 403 shape)', async () => {
    h.groupsError = refusal(403);
    h.preferences = prefsWith({ dalton_notify_targets: [] });
    const { container } = renderSelector();
    await act(async () => {});
    expect(container).toBeEmptyDOMElement();
  });

  it('stores an explicit list, never null: emptied saves [] and a full personal set stays a list', async () => {
    h.preferences = prefsWith({
      dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': ['feishu:user:ou_me_1'] },
    });
    renderSelector();
    await act(async () => {
      fireEvent.click(triggerButton());
    });
    // Unselect the only routed target -> explicit deliver-nowhere ([]).
    await act(async () => {
      fireEvent.click(screen.getByRole('checkbox', { name: '个人目标 feishu:user:ou_me_1' }));
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': [] },
        },
      });
    });

    // Re-select it, then the other personal target: the full personal set is
    // saved as an explicit list, not null (null would re-widen or fall back).
    await act(async () => {
      fireEvent.click(screen.getByRole('checkbox', { name: '个人目标 feishu:user:ou_me_1' }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('checkbox', { name: '个人目标 feishu:user:ou_backup_2' }));
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_routes: {
            'dalton-googl-earn-2026q1-t0': [
              'feishu:user:ou_me_1',
              'feishu:user:ou_backup_2',
            ],
          },
        },
      });
    });
  });

  it('collapses more than two selected targets to N 个目标', () => {
    h.preferences = prefsWith({
      dalton_notify_routes: {
        'dalton-googl-earn-2026q1-t0': [
          'feishu:user:ou_me_1',
          'feishu:chat:oc_research_group',
          'feishu:chat:oc_ops_group',
        ],
      },
    });
    h.groups = GROUPS;
    renderSelector();
    expect(triggerButton()).toHaveTextContent('3 个目标');
  });

  it('reads a stale route naming a deleted personal target as an empty route', () => {
    h.preferences = prefsWith({
      dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': ['feishu:user:ou_gone'] },
    });
    renderSelector();
    // The stale personal entry drops out; the remaining (empty) list is an
    // explicit route, so the label says Not pushed, not the default.
    expect(triggerButton()).toHaveTextContent('Not pushed');
  });
  it('shows a saved group the bot left as removable, and drops it on uncheck', async () => {
    h.groups = GROUPS;
    h.preferences = prefsWith({
      dalton_notify_routes: {
        'dalton-googl-earn-2026q1-t0': ['feishu:chat:oc_old', 'feishu:user:ou_me_1'],
      },
    });
    renderSelector();
    await act(async () => {
      fireEvent.click(triggerButton());
    });
    // Discovered groups plus the stale one, marked as left.
    expect(screen.getByText('核心投研群')).toBeInTheDocument();
    const stale = screen.getByRole('checkbox', { name: '飞书群 oc_old' });
    expect(stale).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText(`oc_old（bot no longer in this group）`)).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(stale);
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_routes: {
            'dalton-googl-earn-2026q1-t0': ['feishu:user:ou_me_1'],
          },
        },
      });
    });
  });

  it('stays openable with a saved route, no personal targets, and no groups, to allow clearing', async () => {
    // The 403 shape: no group list at all, no personal targets — but a saved
    // route naming a group the bot left. The selector must open so the route
    // can be cleared (otherwise the task could never be edited again).
    h.groupsError = refusal(403);
    h.preferences = prefsWith({
      dalton_notify_targets: [],
      dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': ['feishu:chat:oc_old'] },
    });
    renderSelector();
    expect(triggerButton()).toHaveTextContent('oc_old');
    await act(async () => {
      fireEvent.click(triggerButton());
    });
    const stale = screen.getByRole('checkbox', { name: '飞书群 oc_old' });
    await act(async () => {
      fireEvent.click(stale);
    });
    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalledWith({
        model_preference: {
          dalton_notify_routes: { 'dalton-googl-earn-2026q1-t0': [] },
        },
      });
    });
  });

  it('fetches no group list for a task without a pushable category', async () => {
    h.preferences = prefsWith({ dalton_notify_targets: [] });
    const { container } = renderSelector({
      automation: { name: 'dalton-googl-watch', dalton_notify_category: null },
    });
    await act(async () => {});
    expect(container).toBeEmptyDOMElement();
    expect(h.fetchCount).toBe(0);
  });
});
