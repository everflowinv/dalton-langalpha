/**
 * What a price alert's transcript row says: the alert in words, from the
 * notice or from its target, and a generic label when neither names it. The
 * model-facing query text is never the source.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MarketAlertRow } from '../MarketAlertRow';
import type { MarketTarget } from '../../../utils/api';

// Neutral placeholder tickers and made-up prices only.
const FIRED: MarketTarget = {
  target_id: 'tg-1',
  symbol: 'NVDA',
  kind: 'above',
  value: 205,
  pct_from: null,
  armed_price: null,
  status: 'fired',
  expires_at: '2030-01-02T00:00:00Z',
  fired_at: '2030-01-01T15:05:00Z',
  fired_price: 205.12,
  created_at: '2030-01-01T00:00:00Z',
};

describe('MarketAlertRow', () => {
  it('names the wake from the target the notice points at', () => {
    render(<MarketAlertRow alert={{ source: 'wake', notices: [{ target_id: 'tg-1' }] }} targets={[FIRED]} />);
    expect(screen.getByText(/^Price alert woke the agent: NVDA crossed above 205\.00 · 205\.12 at /)).toBeInTheDocument();
  });

  it('falls back to the generic wake label when nothing names the target', () => {
    render(<MarketAlertRow alert={{ source: 'wake', notices: [{ target_id: 'gone' }] }} targets={[FIRED]} />);
    expect(screen.getByText('Price alert woke the agent')).toBeInTheDocument();
  });

  it('says a delivered move from its own fields, with no targets loaded', () => {
    render(
      <MarketAlertRow
        alert={{
          source: 'delivered',
          notices: [{ target_id: 'x', notice: 'hit', symbol: 'TSLA', kind: 'pct_down', value: 3, pct_from: 'previous_close' }],
        }}
      />,
    );
    expect(screen.getByText('TSLA moved -3% from previous close')).toBeInTheDocument();
  });

  it('says a target is about to expire', () => {
    render(
      <MarketAlertRow
        alert={{
          source: 'delivered',
          notices: [{ target_id: 'x', notice: 'expiring', symbol: 'NVDA', kind: 'below', value: 180 }],
        }}
      />,
    );
    expect(screen.getByText('NVDA ≤ 180 target expires soon')).toBeInTheDocument();
  });
});
