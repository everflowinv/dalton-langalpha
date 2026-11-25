import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { CommentComposer } from '../CommentComposer';
import { SelectionQuote } from '../../../../selectionQuote/SelectionQuote';

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: undefined, isLoading: false }),
}));

const originalMatchMedia = window.matchMedia;

function mockCoarse(coarse: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query === '(pointer: coarse)' ? coarse : false,
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

describe('Coarse-pointer Enter inserts newline in CommentComposer and SelectionQuote', () => {
  afterEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      configurable: true,
      value: originalMatchMedia,
    });
  });

  it('CommentComposer does not submit on plain Enter under pointer: coarse, but submits under pointer: fine', () => {
    mockCoarse(true);
    const onSubmitCoarse = vi.fn().mockResolvedValue(undefined);
    const { unmount } = renderWithProviders(
      <CommentComposer
        value="Check margin"
        onChange={vi.fn()}
        placeholder="Add a comment"
        canQueue
        canSend
        onSubmit={onSubmitCoarse}
        mode={{ kind: 'reply' }}
      />,
    );

    const coarseField = screen.getByPlaceholderText('Add a comment');
    fireEvent.keyDown(coarseField, { key: 'Enter', shiftKey: false });
    expect(onSubmitCoarse).not.toHaveBeenCalled();
    unmount();

    mockCoarse(false);
    const onSubmitFine = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(
      <CommentComposer
        value="Check margin"
        onChange={vi.fn()}
        placeholder="Add a comment"
        canQueue
        canSend
        onSubmit={onSubmitFine}
        mode={{ kind: 'reply' }}
      />,
    );
    const fineField = screen.getByPlaceholderText('Add a comment');
    fireEvent.keyDown(fineField, { key: 'Enter', shiftKey: false });
    expect(onSubmitFine).toHaveBeenCalledTimes(1);
  });

  it('SelectionQuote reply does not submit on plain Enter under pointer: coarse, but submits under pointer: fine', () => {
    const boundsRef = { current: document.body };
    const anchor = {
      kind: 'rect' as const,
      id: 1,
      text: 'Selected quote text',
      rect: { left: 40, top: 80, bottom: 100 },
    };

    mockCoarse(true);
    const onAddCoarse = vi.fn();
    const { unmount } = renderWithProviders(
      <SelectionQuote
        anchor={anchor}
        composing
        boundsRef={boundsRef}
        label="Reply"
        placeholder="Ask about selection"
        addLabel="Add"
        onReply={vi.fn()}
        onAdd={onAddCoarse}
        onDismiss={vi.fn()}
      />,
    );
    const coarseBox = screen.getByPlaceholderText('Ask about selection');
    fireEvent.change(coarseBox, { target: { value: 'Follow up' } });
    fireEvent.keyDown(coarseBox, { key: 'Enter', shiftKey: false });
    expect(onAddCoarse).not.toHaveBeenCalled();
    unmount();

    mockCoarse(false);
    const onAddFine = vi.fn();
    renderWithProviders(
      <SelectionQuote
        anchor={anchor}
        composing
        boundsRef={boundsRef}
        label="Reply"
        placeholder="Ask about selection"
        addLabel="Add"
        onReply={vi.fn()}
        onAdd={onAddFine}
        onDismiss={vi.fn()}
      />,
    );
    const fineBox = screen.getByPlaceholderText('Ask about selection');
    fireEvent.change(fineBox, { target: { value: 'Follow up' } });
    fireEvent.keyDown(fineBox, { key: 'Enter', shiftKey: false });
    expect(onAddFine).toHaveBeenCalledWith('Follow up');
  });
});
