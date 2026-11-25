import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import ChatInput from '../chat-input';
import { ModelPicker } from '../chat-input.modelPicker';
import type { ModelGroup } from '@/lib/modelGroups';
import type { Payer } from '@/lib/modelPayer';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: undefined, isLoading: false }),
}));

vi.mock('../use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
  toast: vi.fn(),
}));

const mediaState = vi.hoisted(() => ({ mobile: false }));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => mediaState.mobile,
  getIsMobileSnapshot: () => mediaState.mobile,
}));

const originalMatchMedia = window.matchMedia;

function mockMedia({ mobile, coarse }: { mobile: boolean; coarse: boolean }) {
  mediaState.mobile = mobile;
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches:
        query === '(max-width: 767px)'
          ? mobile
          : query === '(pointer: coarse)'
            ? coarse
            : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

const SAMPLE_GROUPS: ModelGroup[] = [
  {
    id: 'anthropic',
    label: 'Anthropic',
    connections: [
      {
        id: 'anthropic:api',
        payer: 'own_key' as Payer,
        models: ['claude-sonnet-4'],
      },
    ],
  },
  {
    id: 'openai',
    label: 'OpenAI',
    connections: [
      {
        id: 'openai:api',
        payer: 'own_key' as Payer,
        models: ['gpt-5'],
      },
    ],
  },
];

const SAMPLE_META: Record<string, ModelMetadataEntry> = {
  'claude-sonnet-4': { display_name: 'Claude Sonnet 4', provider: 'anthropic' },
  'gpt-5': { display_name: 'GPT-5', provider: 'openai' },
};

describe('Mobile ChatInput Enter & ModelPicker focus behavior', () => {
  afterEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      configurable: true,
      value: originalMatchMedia,
    });
    vi.restoreAllMocks();
  });

  it('does not call onSend on Enter when pointer is coarse (touch device)', () => {
    mockMedia({ mobile: true, coarse: true });
    const onSend = vi.fn();
    renderWithProviders(<ChatInput onSend={onSend} />);

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: 'hello mobile' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    expect(onSend).not.toHaveBeenCalled();
  });

  it('calls onSend on Enter when pointer is fine (desktop)', () => {
    mockMedia({ mobile: false, coarse: false });
    const onSend = vi.fn();
    renderWithProviders(<ChatInput onSend={onSend} />);

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: 'hello desktop' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it('does not auto-focus the ModelPicker search input on mobile', () => {
    mockMedia({ mobile: true, coarse: true });
    const anchor = document.createElement('button');
    document.body.appendChild(anchor);
    const triggerRef = { current: anchor };

    renderWithProviders(
      <ModelPicker
        triggerRef={triggerRef}
        isOpen
        onOpenChange={vi.fn()}
        direction="up"
        onPick={vi.fn()}
        onManage={vi.fn()}
        selectedModel="claude-sonnet-4"
        metadata={SAMPLE_META}
        payers={{}}
        groups={SAMPLE_GROUPS}
        favorites={['claude-sonnet-4']}
        recent={[]}
        onToggleFavorite={vi.fn()}
        rowStatus={() => ({})}
      />,
    );

    const searchInput = screen.getByRole('searchbox');
    expect(document.activeElement).not.toBe(searchInput);

    // Switching provider tab on mobile must also avoid focusing the search input
    const openaiTab = screen.getByRole('radio', { name: 'OpenAI' });
    fireEvent.click(openaiTab);
    expect(document.activeElement).not.toBe(searchInput);

    anchor.remove();
  });

  it('focuses the ModelPicker search input on desktop when switching provider tabs', () => {
    mockMedia({ mobile: false, coarse: false });
    const anchor = document.createElement('button');
    document.body.appendChild(anchor);
    const triggerRef = { current: anchor };

    renderWithProviders(
      <ModelPicker
        triggerRef={triggerRef}
        isOpen
        onOpenChange={vi.fn()}
        direction="up"
        onPick={vi.fn()}
        onManage={vi.fn()}
        selectedModel="claude-sonnet-4"
        metadata={SAMPLE_META}
        payers={{}}
        groups={SAMPLE_GROUPS}
        favorites={['claude-sonnet-4']}
        recent={[]}
        onToggleFavorite={vi.fn()}
        rowStatus={() => ({})}
      />,
    );

    const searchInput = screen.getByRole('searchbox');
    const openaiTab = screen.getByRole('radio', { name: 'OpenAI' });
    fireEvent.click(openaiTab);
    expect(document.activeElement).toBe(searchInput);

    anchor.remove();
  });
});
