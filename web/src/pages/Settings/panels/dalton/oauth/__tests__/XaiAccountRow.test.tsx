import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import type { ReactNode } from 'react';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback || key,
  }),
}));

const mockInvalidateQueries = vi.fn();
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({
    invalidateQueries: mockInvalidateQueries,
  }),
}));

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ open, children }: { open: boolean; children: ReactNode }) =>
    open ? <div data-testid="dialog-root">{children}</div> : null,
  DialogContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
  DialogFooter: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

const h = vi.hoisted(() => ({
  initiateXaiDevice: vi.fn(),
  pollXaiDevice: vi.fn(),
  getXaiOAuthStatus: vi.fn(),
  disconnectXaiOAuth: vi.fn(),
}));

vi.mock('../../../../utils/daltonOAuthApi', () => ({
  initiateXaiDevice: (...args: unknown[]) => h.initiateXaiDevice(...args),
  pollXaiDevice: (...args: unknown[]) => h.pollXaiDevice(...args),
  getXaiOAuthStatus: (...args: unknown[]) => h.getXaiOAuthStatus(...args),
  disconnectXaiOAuth: (...args: unknown[]) => h.disconnectXaiOAuth(...args),
}));

import { XaiAccountRow } from '../XaiAccountRow';

describe('XaiAccountRow', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(window, 'open').mockImplementation(() => null);
    h.getXaiOAuthStatus.mockReset().mockResolvedValue({ connected: false });
    h.initiateXaiDevice.mockReset();
    h.pollXaiDevice.mockReset();
    h.disconnectXaiOAuth.mockReset();
    mockInvalidateQueries.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('displays error message after poll returns access_denied and stops polling', async () => {
    h.initiateXaiDevice.mockResolvedValue({
      user_code: 'ABCD-1234',
      verification_url: 'https://auth.x.ai/activate?user_code=ABCD-1234',
      interval: 5,
      expires_in: 300,
    });
    h.pollXaiDevice.mockRejectedValue({
      response: {
        data: {
          detail: 'The xAI sign-in request was denied (access_denied).',
        },
      },
    });

    render(<XaiAccountRow />);
    await act(async () => {
      await Promise.resolve();
    });

    fireEvent.click(screen.getByText('Connect'));
    fireEvent.click(screen.getByText('Open verification page'));

    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByText('ABCD-1234')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    expect(
      screen.getByText('The xAI sign-in request was denied (access_denied).'),
    ).toBeInTheDocument();
    expect(screen.queryByText('ABCD-1234')).not.toBeInTheDocument();
  });

  it('discards stale poll results from a cancelled generation after reconnecting', async () => {
    h.initiateXaiDevice
      .mockResolvedValueOnce({
        user_code: 'GEN1-CODE',
        verification_url: 'https://auth.x.ai/activate?user_code=GEN1-CODE',
        interval: 5,
        expires_in: 300,
      })
      .mockResolvedValueOnce({
        user_code: 'GEN2-CODE',
        verification_url: 'https://auth.x.ai/activate?user_code=GEN2-CODE',
        interval: 5,
        expires_in: 300,
      });

    let rejectGen1Poll!: (reason?: unknown) => void;
    const gen1PollPromise = new Promise((_, reject) => {
      rejectGen1Poll = reject;
    });
    h.pollXaiDevice.mockReturnValueOnce(gen1PollPromise);

    render(<XaiAccountRow />);
    await act(async () => {
      await Promise.resolve();
    });

    // Start generation 1
    fireEvent.click(screen.getByText('Connect'));
    fireEvent.click(screen.getByText('Open verification page'));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText('GEN1-CODE')).toBeInTheDocument();

    // Trigger gen 1 poll (now in-flight)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(h.pollXaiDevice).toHaveBeenCalledTimes(1);

    // Cancel gen 1 and start gen 2 before gen 1's poll settles
    fireEvent.click(screen.getByText('Cancel'));
    fireEvent.click(screen.getByText('Connect'));
    fireEvent.click(screen.getByText('Open verification page'));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText('GEN2-CODE')).toBeInTheDocument();

    // Reject stale gen 1 poll — must be ignored and keep gen 2 active
    await act(async () => {
      rejectGen1Poll({
        response: { data: { detail: 'Stale access_denied from gen 1' } },
      });
      await Promise.resolve();
    });

    expect(screen.getByText('GEN2-CODE')).toBeInTheDocument();
    expect(
      screen.queryByText('Stale access_denied from gen 1'),
    ).not.toBeInTheDocument();
  });
});
