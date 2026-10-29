/**
 * DALTON-HOOK(handoff-report-quiescence): a group header counts a wait-cap
 * report apart — reported back with work still open, never as finished —
 * and its row keeps the ring instead of the done glyph.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { HandoffGroup } from '../HandoffGroup';
import type { HandoffView } from '../handoffModel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: { count?: number }) =>
      opts && opts.count !== undefined ? `${key}:${opts.count}` : key,
  }),
  Trans: ({ i18nKey }: { i18nKey: string }) => i18nKey,
}));

function view(over: Partial<HandoffView> = {}): HandoffView {
  return {
    id: over.id ?? 'v-1',
    phase: 'finished',
    task: 'Pull the 10-Q filings',
    workspaceName: 'Semis',
    steered: false,
    read: true,
    ...over,
  } as HandoffView;
}

describe('HandoffGroup: an unfinished report', () => {
  it('tallies it apart from the finished, never inside', () => {
    renderWithProviders(
      <HandoffGroup views={[view({ id: 'a' }), view({ id: 'b', unfinished: true })]} />,
    );
    expect(screen.getByText('chat.ptcCard.summaryFinished:1')).toBeInTheDocument();
    expect(screen.getByText('chat.ptcCard.summaryUnfinished:1')).toBeInTheDocument();
  });

  it('keeps the ring in its row, not the done glyph', () => {
    const { container } = renderWithProviders(
      <HandoffGroup views={[view({ id: 'a', unfinished: true })]} />,
    );
    const row = container.querySelector<HTMLElement>('[data-handoff-member="a"]')!;
    expect(row).not.toBeNull();
    // The glyph column is the row's first aria-hidden span.
    const column = row.querySelector(':scope > span[aria-hidden="true"]');
    expect(column).not.toBeNull();
    expect(column!.querySelector('svg')).toBeNull(); // the cut-through check is an svg
    expect(column!.querySelector('span.rounded-full')).not.toBeNull(); // the ring
  });

  it('still shows the done glyph for work that did finish', () => {
    const { container } = renderWithProviders(
      <HandoffGroup views={[view({ id: 'a' })]} />,
    );
    const column = container.querySelector<HTMLElement>(
      '[data-handoff-member="a"] > span[aria-hidden="true"]',
    )!;
    expect(column.querySelector('svg')).not.toBeNull();
    expect(column.querySelector('span.rounded-full')).toBeNull();
  });
});
