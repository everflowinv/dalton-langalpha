import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import StatusPanel from '../StatusPanel';
import type { MarketTarget } from '../../utils/api';

// i18n runs against the real en-US catalog (initialized in test setup), so
// assert on stable substrings — symbols, invented prices, and hint copy.
// Neutral placeholder tickers and made-up prices only.
function target(over: Partial<MarketTarget>): MarketTarget {
  return {
    target_id: 'tg-1',
    symbol: 'NVDA',
    kind: 'above',
    value: 205,
    pct_from: null,
    armed_price: null,
    status: 'armed',
    expires_at: new Date(Date.now() + 2 * 3_600_000 + 60_000).toISOString(),
    fired_at: null,
    fired_price: null,
    created_at: '2030-01-01T00:00:00Z',
    ...over,
  };
}

const TARGETS: MarketTarget[] = [
  target({ target_id: 'armed' }),
  target({
    target_id: 'fired', symbol: 'TSLA', kind: 'pct_down', value: 3, pct_from: 'previous_close',
    status: 'fired', fired_price: 388.4, fired_at: '2030-01-01T15:05:00Z',
  }),
  target({ target_id: 'expired', symbol: 'AAPL', kind: 'pct_up', value: 2, pct_from: 'now', armed_price: 230, status: 'expired' }),
  target({ target_id: 'cancelled', kind: 'below', value: 180, status: 'cancelled' }),
];

const CONTENT = ['As of 14:30:00 ET (Regular)', 'NVDA  178.34  +1.20%', 'TSLA  402.11  -0.85%'].join('\n');

describe('StatusPanel', () => {
  it('renders the watched symbols', () => {
    render(<StatusPanel marketWatch={{ symbols: ['NVDA', 'TSLA'] }} />);
    expect(screen.getByText('NVDA')).toBeInTheDocument();
    expect(screen.getByText('TSLA')).toBeInTheDocument();
  });

  it('renders the quote block — header line plus quote rows — when content is present', () => {
    render(<StatusPanel marketWatch={{ symbols: ['NVDA', 'TSLA'], content: CONTENT }} />);
    // First line is the muted "As of …" header.
    expect(screen.getByText(/As of 14:30:00 ET/)).toBeInTheDocument();
    // Remaining lines are the quote rows (invented prices).
    expect(screen.getByText(/178\.34/)).toBeInTheDocument();
    expect(screen.getByText(/402\.11/)).toBeInTheDocument();
  });

  it('shows the waiting hint (and no updated caption) when content is absent', () => {
    render(<StatusPanel marketWatch={{ symbols: ['NVDA'] }} />);
    expect(screen.getByText(/Live prices appear here/)).toBeInTheDocument();
    expect(screen.queryByText(/Updated /)).not.toBeInTheDocument();
  });

  it('shows an updated-at caption when timestamp is set', () => {
    render(<StatusPanel marketWatch={{ symbols: ['NVDA'], timestamp: 1_700_000_000 }} />);
    expect(screen.getByText(/Updated /)).toBeInTheDocument();
  });

  it('renders a defensive empty state when there are no symbols', () => {
    render(<StatusPanel marketWatch={{ symbols: [] }} />);
    expect(screen.getByText(/No tickers are being watched/)).toBeInTheDocument();
  });

  it('does not crash when marketWatch is null', () => {
    render(<StatusPanel marketWatch={null} />);
    expect(screen.getByText(/No tickers are being watched/)).toBeInTheDocument();
  });

  it('lists each target with its condition and where it stands', () => {
    render(<StatusPanel marketWatch={{ symbols: ['NVDA'], targets: TARGETS }} />);
    const rows = screen.getAllByRole('listitem');
    expect(rows.map((row) => row.getAttribute('data-target-status'))).toEqual(['armed', 'fired', 'expired', 'cancelled']);
    expect(rows[0]).toHaveTextContent('≥ 205');
    expect(rows[0]).toHaveTextContent('Armed · expires in 2h');
    expect(rows[1]).toHaveTextContent('-3% from previous close');
    expect(rows[1]).toHaveTextContent(/Hit · 388\.40 at /);
    expect(rows[2]).toHaveTextContent('+2% from 230.00');
    expect(rows[2]).toHaveTextContent('Expired');
    expect(rows[3]).toHaveTextContent('≤ 180');
    expect(rows[3]).toHaveTextContent('Cancelled');
  });

  it('offers a cancel only on an armed target, and calls it with its id', async () => {
    const onCancelTarget = vi.fn().mockResolvedValue(undefined);
    render(<StatusPanel marketWatch={{ symbols: [], targets: TARGETS }} onCancelTarget={onCancelTarget} />);
    const cancels = screen.getAllByRole('button', { name: /Cancel the/ });
    expect(cancels).toHaveLength(1);
    expect(cancels[0]).toHaveAccessibleName('Cancel the NVDA ≥ 205 target');
    fireEvent.click(cancels[0]);
    expect(onCancelTarget).toHaveBeenCalledWith('armed');
    await waitFor(() => expect(cancels[0]).not.toBeDisabled());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('says so when a cancel fails, and lets it be tried again', async () => {
    const onCancelTarget = vi.fn().mockRejectedValue(new Error('Network Error'));
    render(<StatusPanel marketWatch={{ symbols: [], targets: [target({})] }} onCancelTarget={onCancelTarget} />);
    fireEvent.click(screen.getByRole('button', { name: /Cancel the/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't cancel this target");
    expect(screen.getByRole('button', { name: /Cancel the/ })).not.toBeDisabled();
  });

  it('shows no cancel without a handler', () => {
    render(<StatusPanel marketWatch={{ symbols: [], targets: [target({})] }} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('shows targets alone without the empty watch-list state', () => {
    render(<StatusPanel marketWatch={{ symbols: [], targets: [target({})] }} />);
    expect(screen.getByText('Price targets')).toBeInTheDocument();
    expect(screen.queryByText(/No tickers are being watched/)).not.toBeInTheDocument();
  });
});
