/**
 * sync3 B11 r4 review fix: the mobile dashboard composer reports its send's
 * outcome — a refused handoff keeps the composer open (and the stored draft),
 * a successful one collapses the sheet, exactly as the desktop behaves.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import ChatInputCard from '../components/ChatInputCard';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: 'en-US', changeLanguage: vi.fn() } }),
}));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => true,
  getIsMobileSnapshot: () => true,
}));

// Exits finish at once, so a collapse unmounts the composer in the same act.
vi.mock('@/lib/framer', async (importActual) => ({
  ...(await importActual<Record<string, unknown>>()),
  AnimatePresence: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
}));

// The real FAB opens on framer's tap gesture, which jsdom clicks never fire.
vi.mock('@/components/ui/langalpha-fab', () => ({
  default: ({ onClick }: { onClick: () => void }) => (
    <button type="button" aria-label="Open chat" onClick={onClick} />
  ),
}));

const handleSend = vi.fn<(m: string) => Promise<boolean>>();

vi.mock('../hooks/useChatInput', () => ({
  useChatInput: () => ({
    scope: 'all',
    setScope: vi.fn(),
    isLoading: false,
    handleSend: (...args: unknown[]) => handleSend(...(args as [string])),
    workspaces: { workspaces: [] },
    selectedWorkspaceId: null,
    setSelectedWorkspaceId: vi.fn(),
  }),
}));

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ data: undefined, isLoading: false }),
}));

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
  toast: vi.fn(),
}));

function renderCard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ChatInputCard />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

async function expandAndType(text: string) {
  renderCard();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Open chat' }));
  });
  const box = screen.getByRole('textbox') as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: text } });
  return box;
}

describe('ChatInputCard: the mobile composer honors its send outcome', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('keeps the sheet open when the handoff is refused', async () => {
    handleSend.mockResolvedValue(false);
    const box = await expandAndType('look at NVDA after hours');

    await act(async () => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });

    expect(handleSend).toHaveBeenCalledTimes(1);
    // Refused: the composer stays up for the kept draft.
    expect(screen.queryByRole('textbox')).not.toBeNull();
  });

  it('collapses the sheet when the handoff succeeds', async () => {
    handleSend.mockResolvedValue(true);
    const box = await expandAndType('today in markets');

    await act(async () => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });

    expect(handleSend).toHaveBeenCalledTimes(1);
    // Success: the sheet folds back to the FAB.
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByRole('textbox')).toBeNull();
  });
});
