/**
 * sync3 B15: a read-only thread must not offer quoting — ChatView passes
 * `active={... && !isThreadReadOnly}` (colleague read-only included), so an
 * inactive surface takes no selection at all. This pins the component's half
 * of that gate; the call site is smoke-covered.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { createRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MessageSelectionQuote } from '../MessageSelectionQuote';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

// jsdom gives a selection no client rects, so the toolbar's placement would
// read the anchor as lost and dismiss itself. One line's worth of rect.
const RECT = { top: 10, bottom: 24, left: 10, right: 120, width: 110, height: 14 };
// jsdom does not define these on Range at all, so give them a body to spy on.
for (const name of ['getClientRects', 'getBoundingClientRect'] as const) {
  if (!(name in Range.prototype)) {
    Object.defineProperty(Range.prototype, name, { configurable: true, writable: true, value: () => undefined });
  }
}
const rectsSpy = vi
  .spyOn(Range.prototype, 'getClientRects')
  .mockReturnValue([RECT] as unknown as DOMRectList);
const boxSpy = vi
  .spyOn(Range.prototype, 'getBoundingClientRect')
  .mockReturnValue(RECT as unknown as DOMRect);

function renderQuoteSurface(active: boolean) {
  const surfaceRef = createRef<HTMLDivElement>();
  render(
    <div ref={surfaceRef}>
      <p data-message-id="m-1" data-message-role="assistant">
        Revenue grew by a lot in the first quarter
      </p>
      <MessageSelectionQuote
        surfaceRef={surfaceRef}
        active={active}
        agentName="Analyst"
        onAddContext={vi.fn()}
      />
    </div>,
  );
  return screen.getByText('Revenue grew by a lot in the first quarter');
}

/** Selects the paragraph's text the way a drag ends: a settled selectionchange. */
function selectParagraph(el: HTMLElement) {
  const range = document.createRange();
  range.selectNodeContents(el);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
  fireEvent(document, new Event('selectionchange'));
}

describe('MessageSelectionQuote on a surface that cannot take quotes', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    window.getSelection()?.removeAllRanges();
    rectsSpy.mockClear();
    boxSpy.mockClear();
  });

  it('ignores a selection while inactive (a read-only thread): no quote toolbar, nothing handed to the composer', () => {
    const el = renderQuoteSurface(false);
    selectParagraph(el);
    act(() => {
      vi.advanceTimersByTime(500);
    });

    // No toolbar appeared, and none of the text became a chip that could
    // reach the composer.
    // `hidden: true`: the toolbar mounts hidden until it is placed, so count it either way.
    expect(screen.queryByRole('toolbar', { hidden: true })).toBeNull();
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(0);
  });

  it('offers the quote while active — the control the inactive case silences', () => {
    const el = renderQuoteSurface(true);
    selectParagraph(el);
    act(() => {
      vi.advanceTimersByTime(500);
    });

    // The selection's toolbar: the quote, copy, and more controls.
    // It mounts hidden until placed (jsdom has no layout), so look past visibility.
    expect(screen.getByRole('toolbar', { hidden: true })).toBeTruthy();
  });
});
