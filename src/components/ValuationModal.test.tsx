/**
 * The valuation modal's filing-currency prompt.
 *
 * The DCF cannot value a holding whose statements are in a currency it does
 * not know the rate for, and there is no FX provider anywhere in this app --
 * so the only way out is to ask. These tests cover the asking, because the
 * alternative to asking is what shipped twice: assuming USD, and reading
 * TSM's TWD figures (32x wrong) and ASML's EUR figures (~9% wrong) as
 * dollars with nothing on screen saying so.
 *
 * Two distinct cases, and the difference matters to the wording. TSM's
 * statements ARRIVED and said TWD, so the prompt can name the currency.
 * ASML files a 20-F and FMP returns no statements at all, so nothing can be
 * named and the prompt has to say the currency is unknown.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/investmentsApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  setValuationOverride: vi.fn(),
  clearValuationOverride: vi.fn(),
}));

import { ValuationModal } from '@/components/ValuationModal';

afterEach(cleanup);

/** A holding with an auto row carrying these two statement fields. */
const holding = (
  statement_currency: string | null,
  statement_exchange_rate: number | null
) =>
  ({
    ticker: 'TEST',
    name: 'Test Co',
    is_valuable: true,
    quantity: 1,
    valuation: null,
    inputs: {
      auto: {
        variant: 'auto',
        base_flow: 1000,
        metric: 'free_cash_flow',
        shares_outstanding: 100,
        total_debt: 0,
        cash_and_st: 0,
        beta: 1,
        growth_1_5: 0.1,
        discount_rate: null,
        region: 'US',
        statement_currency,
        statement_exchange_rate,
        source: 'fmp',
        updated_at: '2026-08-29T00:00:00Z',
      },
      override: null,
    },
  }) as never;

function renderModal(h: never) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ValuationModal holding={h} onClose={() => {}} />
    </QueryClientProvider>
  );
}

describe('the filing-currency prompt', () => {
  it('names the currency when the statements reported one', () => {
    // TSM: the statements arrived and said TWD.
    renderModal(holding('TWD', null));

    expect(screen.getByText(/Files in TWD/)).toBeInTheDocument();
  });

  it('says the currency is unknown when nothing was reported', () => {
    // ASML: FMP returns no statements for a 20-F filer, so there is no
    // reportedCurrency to name. Before this the profile's USD stood in and
    // the rate was pinned at 1.0 with no warning at all.
    renderModal(holding(null, null));

    expect(screen.getByText(/Filing currency unknown/)).toBeInTheDocument();
  });

  it('stays quiet for a USD filer, which needs no rate', () => {
    // The common case. A prompt on every domestic holding would train the
    // eye to skip the two that mean it.
    renderModal(holding('USD', 1));

    expect(screen.queryByText(/Files in/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Filing currency unknown/)).not.toBeInTheDocument();
  });
});

/**
 * The terminal-value row.
 *
 * Two branches, and the refusing one carries most of the weight. Gordon
 * growth divides by (discount rate - perpetual growth), so as that spread
 * approaches zero the value has no upper bound -- and this book's own
 * numbers put HALF its holdings on a spread under 2% against the stage-3
 * constant, which is a 53x-76x terminal multiple. The row has to decline
 * loudly rather than print a very large number.
 */
describe('the terminal-value row', () => {
  const valued = (over: Record<string, unknown>) =>
    ({
      ...(holding('USD', 1) as unknown as Record<string, unknown>),
      valuation: {
        available: true,
        discount_rate: 0.0627,
        base: {
          scenario: 'base',
          intrinsic_value: 369.71,
          growth_1_5: 0.1829,
          growth_6_10: 0.15,
          growth_11_20: 0.04,
          perpetual_growth: 0.025,
          intrinsic_value_with_terminal: 871.11,
          terminal_share_pct: 57.9,
          ...over,
        },
        conservative: {
          scenario: 'conservative',
          intrinsic_value: 296.64,
          growth_1_5: 0.1829,
          growth_6_10: 0.09145,
          growth_11_20: 0.025,
          perpetual_growth: 0.02,
          intrinsic_value_with_terminal: 590.0,
          terminal_share_pct: 50.1,
        },
        average_intrinsic_value: 333.17,
        average_intrinsic_value_with_terminal: 730.56,
        premium_pct: 54.1,
        overridden_fields: [],
      },
    }) as never;

  it('shows the with-terminal figures beside the twenty-year ones', () => {
    renderModal(valued({}));

    // Both models on screen at once -- the gap between them is the output.
    expect(screen.getByText('369.71')).toBeInTheDocument();
    expect(screen.getByText('871.11')).toBeInTheDocument();
    expect(screen.getByText('730.56')).toBeInTheDocument();
  });

  it('says how much of the value is the perpetuity', () => {
    renderModal(valued({}));

    expect(
      screen.getByText(/58% of the value is the perpetuity/)
    ).toBeInTheDocument();
    expect(screen.getByText(/perpetuity at 2.5%/)).toBeInTheDocument();
  });

  it('warns when the perpetuity dominates the answer', () => {
    renderModal(valued({ terminal_share_pct: 82.0 }));

    expect(
      screen.getByText(/statement about the discount rate rather than about the business/)
    ).toBeInTheDocument();
  });

  it('stays quiet about dominance at an ordinary share', () => {
    renderModal(valued({}));

    expect(
      screen.queryByText(/statement about the discount rate rather than/)
    ).not.toBeInTheDocument();
  });

  it('explains itself instead of printing a number when it declines', () => {
    // The real case: a hand-set 2% discount rate sits BELOW the 2.5%
    // perpetual growth, where the formula has no finite answer at all.
    renderModal(
      valued({ intrinsic_value_with_terminal: null, terminal_share_pct: null })
    );

    expect(screen.getByText(/No terminal value/)).toBeInTheDocument();
    expect(
      screen.getByText(/no finite answer, so nothing is reported/)
    ).toBeInTheDocument();
    expect(screen.queryByText('871.11')).not.toBeInTheDocument();
  });
});
