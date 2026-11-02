import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@testing-library/jest-dom';
import { DaltonFeishuGroupsCard } from '../DaltonFeishuGroupsCard';
import { ApiError } from '@/api/client';
import type {
  DaltonFeishuBotResponse,
  DaltonFeishuGroupsResponse,
  DaltonNotifyTestResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  groups: [] as Array<{ chat_id: string; name: string }>,
  groupsError: null as ApiError | null,
  bot: {
    ok: true,
    app_name: 'Everflow 投研',
  } as DaltonFeishuBotResponse,
  sendTest: vi.fn<(target: string) => Promise<DaltonNotifyTestResponse>>(
    async () => ({ ok: true, detail: null, error_code: null }),
  ),
}));

// No AuthProvider is mounted anywhere in this file: admin visibility is read
// from the group endpoint's own refusal, never from an auth context.
vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonFeishuBotName: async (): Promise<DaltonFeishuBotResponse> => h.bot,
  fetchDaltonFeishuGroups: async (): Promise<DaltonFeishuGroupsResponse> => {
    if (h.groupsError) throw h.groupsError;
    return { ok: true, groups: h.groups };
  },
  sendDaltonNotifyTest: (target: string) => h.sendTest(target),
}));

function refusal(status: number): ApiError {
  return new ApiError('Request failed with status code ' + status, 'ERR_BAD_REQUEST', {
    data: {},
    status,
    statusText: '',
    headers: {},
  });
}

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DaltonFeishuGroupsCard />
    </QueryClientProvider>,
  );
}

const GROUPS = [
  { chat_id: 'oc_research_group', name: '核心投研群' },
  { chat_id: 'oc_ops_group', name: '' },
];

describe('DaltonFeishuGroupsCard', () => {
  beforeEach(() => {
    h.groups = GROUPS;
    h.groupsError = null;
    h.bot = { ok: true, app_name: 'Everflow 投研' };
    h.sendTest.mockClear();
    h.sendTest.mockImplementation(async () => ({ ok: true, detail: null, error_code: null }));
  });

  it('renders nothing for a non-admin (the endpoint refuses with 403)', async () => {
    h.groupsError = refusal(403);
    const { container } = renderCard();
    // Let the query settle; the card must stay absent, not error out.
    await waitFor(() => expect(screen.queryByTestId('dalton-feishu-groups-card')).not.toBeInTheDocument());
    expect(container).toBeEmptyDOMElement();
  });

  it('lists discovered groups with the bot name in the caption', async () => {
    renderCard();
    expect(
      await screen.findByText(
        /以下是 OpenClaw 机器人「Everflow 投研」已加入的群，系统可向这些群推送；在自动化列表里为每个任务选择要推送的群/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('核心投研群')).toBeInTheDocument();
    expect(screen.getByText('未命名群')).toBeInTheDocument();
    expect(screen.getByText('feishu:chat:oc_research_group')).toBeInTheDocument();
  });

  it('falls back to the generic caption when the bot name is unavailable', async () => {
    h.bot = { ok: false, app_name: null };
    renderCard();
    expect(
      await screen.findByText(/以下是 OpenClaw 机器人已加入的群，系统可向这些群推送/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/「/)).not.toBeInTheDocument();
  });

  it('sends a test push to the group chat_id and shows both outcomes', async () => {
    renderCard();
    await screen.findByText('核心投研群');

    h.sendTest.mockImplementation(async (target: string) =>
      target === 'feishu:chat:oc_research_group'
        ? { ok: true, detail: null, error_code: null }
        : {
            ok: false,
            detail: '发送失败（exit_code=1）',
            error_code: 'exit_code=1',
          },
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '发送测试 核心投研群' }));
    });
    expect(await screen.findByText('测试已发送')).toBeInTheDocument();
    expect(h.sendTest).toHaveBeenCalledWith('feishu:chat:oc_research_group');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '发送测试 oc_ops_group' }));
    });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('发送失败（exit_code=1）');
  });

  it('still shows the card with its refresh when discovery fails for another reason', async () => {
    h.groupsError = refusal(502);
    renderCard();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('群列表暂时不可用');
    expect(screen.getByRole('button', { name: '刷新群列表' })).toBeInTheDocument();
  });
});
