import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DaltonFeishuGroupsCard } from '../DaltonFeishuGroupsCard';
import type {
  DaltonFeishuGroupsResponse,
  DaltonNotifyTestResponse,
} from '@/pages/Settings/utils/daltonApi';

const h = vi.hoisted(() => ({
  fetchGroups: vi.fn<() => Promise<DaltonFeishuGroupsResponse>>(),
  sendTest: vi.fn<(target: string) => Promise<DaltonNotifyTestResponse>>(),
}));

vi.mock('@/pages/Settings/utils/daltonApi', () => ({
  fetchDaltonFeishuGroups: () => h.fetchGroups(),
  sendDaltonNotifyTest: (target: string) => h.sendTest(target),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DaltonFeishuGroupsCard />
    </QueryClientProvider>,
  );
}

describe('DaltonFeishuGroupsCard', () => {
  beforeEach(() => {
    h.fetchGroups.mockReset();
    h.sendTest.mockReset();
  });

  it('renders Feishu team groups and refetches on Refresh click', async () => {
    h.fetchGroups.mockResolvedValueOnce({
      ok: true,
      groups: [{ chat_id: 'oc_group_1', name: 'TMT 投研群' }],
    });

    renderCard();

    expect(await screen.findByText('TMT 投研群')).toBeInTheDocument();
    expect(screen.getByText(/oc_group_1/)).toBeInTheDocument();

    h.fetchGroups.mockResolvedValueOnce({
      ok: true,
      groups: [{ chat_id: 'oc_group_2', name: '能源跟踪群' }],
    });
    fireEvent.click(screen.getByTestId('dalton-feishu-groups-refresh'));

    await waitFor(() => expect(h.fetchGroups).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('能源跟踪群')).toBeInTheDocument();
  });

  it('sends test push for feishu:chat:<chat_id> and renders sending, success, and error states', async () => {
    h.fetchGroups.mockResolvedValueOnce({
      ok: true,
      groups: [
        { chat_id: 'oc_g1', name: 'Semi Group' },
        { chat_id: 'oc_g2', name: 'Cloud Group' },
      ],
    });
    let resolveFirst!: (res: DaltonNotifyTestResponse) => void;
    h.sendTest.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    );

    renderCard();

    const btn1 = await screen.findByTestId('dalton-feishu-group-test-oc_g1');
    fireEvent.click(btn1);
    expect(h.sendTest).toHaveBeenCalledWith('feishu:chat:oc_g1');
    expect(btn1).toBeDisabled();
    expect(btn1).toHaveTextContent('dalton.notifyTargets.testing');

    resolveFirst({ ok: true, detail: null, error_code: null });
    expect(await screen.findByTestId('dalton-feishu-group-test-ok-oc_g1')).toBeInTheDocument();
    expect(btn1).not.toBeDisabled();

    h.sendTest.mockRejectedValueOnce(new Error('Bot not in chat'));
    const btn2 = screen.getByTestId('dalton-feishu-group-test-oc_g2');
    fireEvent.click(btn2);
    expect(await screen.findByTestId('dalton-feishu-group-test-err-oc_g2')).toHaveTextContent(
      'Bot not in chat',
    );
  });

  it('renders empty state cleanly without OpenClaw jargon', async () => {
    h.fetchGroups.mockResolvedValueOnce({ ok: true, groups: [] });
    renderCard();
    expect(await screen.findByText('dalton.feishuGroups.empty')).toBeInTheDocument();
  });
});
