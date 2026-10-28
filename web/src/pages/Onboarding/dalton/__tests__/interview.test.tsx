import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { DaltonOnboardingRedirect, daltonInterviewKickoff } from '../interview';

const mockResolveChatWorkspace = vi.fn();
const mockWarmWorkspace = vi.fn();
const mockToast = vi.fn();

vi.mock('@/hooks/useHomeWorkspace', () => ({
  resolveChatWorkspace: (...args: unknown[]) => mockResolveChatWorkspace(...args),
}));

vi.mock('@/pages/ChatAgent/utils/warmWorkspace', () => ({
  warmWorkspace: (...args: unknown[]) => mockWarmWorkspace(...args),
}));

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));

function LocationSpy({ onCapture }: { onCapture: (pathname: string, state: unknown) => void }) {
  const loc = useLocation();
  onCapture(loc.pathname, loc.state);
  return null;
}

describe('daltonInterviewKickoff & DaltonOnboardingRedirect', () => {
  beforeEach(() => {
    mockResolveChatWorkspace.mockReset();
    mockWarmWorkspace.mockReset();
    mockToast.mockReset();
  });

  it('loads dalton-onboarding skill (and never upstream onboarding) in daltonInterviewKickoff', () => {
    const t = ((key: string) => key) as unknown as Parameters<typeof daltonInterviewKickoff>[0];
    const kickoff = daltonInterviewKickoff(t);

    expect(kickoff.initialMessage).toBe('onboarding.kickoff.noBrokerage');
    expect(kickoff.additionalContext).toEqual([
      {
        type: 'skills',
        name: 'dalton-onboarding',
        instruction: expect.stringContaining('Dalton onboarding interview'),
      },
    ]);
    expect(
      kickoff.additionalContext.some((ctx) => ctx.name === 'onboarding'),
    ).toBe(false);
  });

  it('redirects /welcome to /chat/t/__default__ with dalton-onboarding skill in state', async () => {
    mockResolveChatWorkspace.mockResolvedValue('ws-home-1');
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let capturedPath = '';
    let capturedState: Record<string, unknown> | null = null;

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/welcome']}>
          <Routes>
            <Route path="/welcome" element={<DaltonOnboardingRedirect />} />
            <Route
              path="*"
              element={
                <LocationSpy
                  onCapture={(pathname, state) => {
                    capturedPath = pathname;
                    capturedState = state as Record<string, unknown> | null;
                  }}
                />
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(capturedPath).toBe('/chat/t/__default__');
    });
    expect(mockWarmWorkspace).toHaveBeenCalledWith('ws-home-1', queryClient);
    expect(capturedState).toMatchObject({
      workspaceId: 'ws-home-1',
      additionalContext: [
        {
          type: 'skills',
          name: 'dalton-onboarding',
        },
      ],
    });
  });
});
