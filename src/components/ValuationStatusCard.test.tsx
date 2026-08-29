/**
 * The two ways to ask for a refresh, and why there are two.
 *
 * "Run now" honours REFRESH_MAX_AGE, which is what keeps the monthly cron
 * idempotent. That guard also means a row fetched recently but holding a
 * WRONG value cannot be corrected by pressing it -- and that is not
 * hypothetical. When the filing currency began being read off the statements
 * rather than the price quote, every row already held a plausible currency
 * and a rate, so nothing was due, and TSM went on reporting an intrinsic
 * value 32x too high with no way to ask for a re-fetch.
 *
 * `refreshValuations(force)` has always supported the flag; the card simply
 * never sent `true`. These tests pin which button sends which, because
 * getting it backwards would either break the cron's idempotence or restore
 * the dead end.
 */

import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ValuationStatusCard } from '@/components/InvestmentTable';

afterEach(cleanup);

/** Only the fields the card reads. */
const holding = (over: Record<string, unknown> = {}) =>
  ({
    ticker: 'MSFT',
    is_valuable: true,
    valuation: { available: true },
    inputs: { auto: { updated_at: '2026-08-29T00:00:00Z' } },
    ...over,
  }) as never;

function renderCard(onRunNow = vi.fn(), running = false) {
  render(
    <ValuationStatusCard
      holdings={[holding()]}
      onRunNow={onRunNow}
      running={running}
    />
  );
  return onRunNow;
}

describe('asking for a refresh', () => {
  it('Run now respects the freshness guard', () => {
    // force=false is what makes the monthly schedule idempotent: a re-run
    // the same day must be a no-op rather than a second full spend of the
    // provider budget.
    const onRunNow = renderCard();

    fireEvent.click(screen.getByRole('button', { name: 'Run now' }));

    expect(onRunNow).toHaveBeenCalledWith(false);
  });

  it('Force ignores it', () => {
    // The only way to correct a row that is recent and wrong.
    const onRunNow = renderCard();

    fireEvent.click(screen.getByRole('button', { name: 'Force' }));

    expect(onRunNow).toHaveBeenCalledWith(true);
  });

  it('hides Force while a run is in flight', () => {
    // Two overlapping full fetches would race each other through the same
    // throttled provider budget.
    renderCard(vi.fn(), true);

    expect(screen.queryByRole('button', { name: 'Force' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Refreshing/ })).toBeDisabled();
  });
});
