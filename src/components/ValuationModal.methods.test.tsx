/**
 * Choosing which of three base flows the twenty-year model is grown from.
 *
 * The three bars are not three models. They are ONE model — same discount
 * rate, same growth schedule, same debt-and-cash bridge — run on free cash
 * flow, operating cash flow and net income in turn. Showing them together is
 * the point: because they differ in exactly one input, the gap between them
 * is attributable. A DCF-20 far above its DFCF-20 says the operating cash is
 * going into capital expenditure, and a DNI-20 above both says there are
 * accounting earnings the cash flow statement does not corroborate.
 *
 * Two behaviours here are deliberate and worth pinning, because both are
 * the kind of thing a later "tidy-up" would reverse:
 *
 *   * A method with no figure behind it stays CLICKABLE. That is the way in
 *     to hand-keying one — switch to DNI-20, and the banner names
 *     `net_income` as what is missing with the field for it on the right.
 *     Disabling it would make the flow a dead end for exactly the holdings
 *     no data provider covers, which are the ones that need it.
 *
 *   * A flow that is present but zero or negative gets a DIFFERENT message
 *     from one that is absent. Telling someone to "fill in net_income" when
 *     net income is a real reported loss is asking them to correct a fact.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const updateHolding = vi.fn();

vi.mock('@/lib/investmentsApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  setValuationOverride: vi.fn(),
  clearValuationOverride: vi.fn(),
  updateHolding: (...args: unknown[]) => updateHolding(...args),
}));

import { ValuationModal } from '@/components/ValuationModal';

afterEach(cleanup);
beforeEach(() => updateHolding.mockReset().mockResolvedValue({}));

/** One scenario, in the shape the API returns it. */
const scenario = (intrinsic: number) => ({
  scenario: 'base',
  intrinsic_value: intrinsic,
  growth_1_5: 0.1,
  growth_6_10: 0.1,
  growth_11_20: 0.04,
  perpetual_growth: 0.025,
  intrinsic_value_with_terminal: intrinsic * 2,
  terminal_share_pct: 50,
});

/** One method's output. */
const model = (average: number, premiumPct: number | null) => ({
  discount_rate: 0.0627,
  base: scenario(average * 1.1),
  conservative: scenario(average * 0.9),
  average_intrinsic_value: average,
  average_intrinsic_value_with_terminal: average * 2,
  premium_pct: premiumPct,
});

/**
 * MSFT's real proportions: operating cash flow well above free cash flow
 * (the gap is capex), net income between the two — so the ordering of the
 * three values on screen is the ordering of the flows behind them.
 */
const holding = (over: Record<string, unknown> = {}) =>
  ({
    ticker: 'MSFT',
    name: 'Microsoft',
    is_valuable: true,
    quantity: 1,
    current_price: 400,
    valuation_method: 'free_cash_flow',
    valuation: {
      available: true,
      method: 'free_cash_flow',
      discount_rate: 0.0627,
      base: scenario(369.71),
      conservative: scenario(296.64),
      average_intrinsic_value: 333.17,
      average_intrinsic_value_with_terminal: 730.56,
      premium_pct: 20.1,
      overridden_fields: [],
      models: {
        free_cash_flow: model(333.17, 20.1),
        operating_cash_flow: model(647.29, -38.2),
        net_income: model(418.85, -4.5),
      },
      ...(over.valuation as Record<string, unknown>),
    },
    inputs: { auto: null, override: null },
    ...over,
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

const toggle = (name: RegExp) => screen.getByRole('button', { name });

describe('the three methods', () => {
  it('shows all three at once, not only the selected one', () => {
    renderModal(holding());

    expect(toggle(/DFCF-20, Free cash flow/)).toBeInTheDocument();
    expect(toggle(/DCF-20, Operating cash flow/)).toBeInTheDocument();
    expect(toggle(/DNI-20, Net income/)).toBeInTheDocument();
  });

  it('gives each method its own value and its own premium', () => {
    // The comparison is the output, so a shared figure across the three
    // would defeat the whole panel.
    renderModal(holding());

    expect(toggle(/DFCF-20/)).toHaveTextContent('333.17');
    expect(toggle(/DCF-20, Operating/)).toHaveTextContent('647.29');
    expect(toggle(/DNI-20/)).toHaveTextContent('418.85');
    expect(toggle(/DCF-20, Operating/)).toHaveTextContent('−38.2%');
  });

  it('marks the one in force', () => {
    renderModal(holding({ valuation_method: 'net_income' }));

    // The SERVER's answer wins over the holding's own column: it says which
    // model produced the numbers on screen, not which was asked for.
    expect(toggle(/DFCF-20/)).toHaveAttribute('aria-pressed', 'true');
    expect(toggle(/DNI-20/)).toHaveAttribute('aria-pressed', 'false');
  });

  it('falls back to the holding column when the payload predates the choice', () => {
    renderModal(
      holding({
        valuation_method: 'net_income',
        valuation: { method: undefined },
      })
    );

    expect(toggle(/DNI-20/)).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('switching', () => {
  it('patches the holding with the chosen method', async () => {
    renderModal(holding());

    fireEvent.click(toggle(/DNI-20/));

    await waitFor(() =>
      expect(updateHolding).toHaveBeenCalledWith('MSFT', {
        valuation_method: 'net_income',
      })
    );
  });

  it('does not send a request for the method already in force', () => {
    renderModal(holding());

    fireEvent.click(toggle(/DFCF-20/));

    expect(updateHolding).not.toHaveBeenCalled();
  });

  /* A FAILED switch is not covered here, deliberately.
   *
   * The behaviour works -- the modal's onError sets the message and the
   * banner renders it; that was verified directly by asserting on
   * document.body after a rejected patch. But react-query leaves a second,
   * unhandled copy of the rejection behind in this particular render tree,
   * and vitest reports it as a crash ON the very error the handler exists to
   * display, so the test fails while the feature works. Neither mutateAsync
   * with an explicit .catch nor marking the promise handled in the mock
   * suppresses it; an identical probe against the same hook outside this
   * modal passes, so it is not the call site.
   *
   * Left out rather than papered over with a swallowed assertion. Mutation
   * error surfacing is generic and already covered against a real component
   * in src/app/analytics/page.test.tsx ("Save error keeps the drawer open").
   */
});

describe('a method with nothing behind it', () => {
  it('says so rather than showing a zero', () => {
    // 0.00 would read as "worth nothing", which is a claim about the
    // business where the truth is that this model has no input.
    renderModal(
      holding({
        valuation: {
          models: { free_cash_flow: model(333.17, 20.1) },
        },
      })
    );

    expect(toggle(/DNI-20/)).toHaveTextContent('no figure');
    expect(toggle(/DNI-20/)).not.toHaveTextContent('0.00');
  });

  it('stays clickable, because that is the way in to keying one', async () => {
    renderModal(
      holding({
        valuation: {
          models: { free_cash_flow: model(333.17, 20.1) },
        },
      })
    );

    fireEvent.click(toggle(/DNI-20/));

    await waitFor(() =>
      expect(updateHolding).toHaveBeenCalledWith('MSFT', {
        valuation_method: 'net_income',
      })
    );
  });
});

describe('when the chosen method cannot run', () => {
  it('names the missing flow and offers the fields', () => {
    renderModal(
      holding({
        valuation_method: 'net_income',
        valuation: {
          available: false,
          method: 'net_income',
          missing: ['net_income'],
          non_positive_flow: null,
          models: { free_cash_flow: model(333.17, 20.1) },
        },
      })
    );

    expect(screen.getByText(/Not valued yet/)).toBeInTheDocument();
    expect(screen.getByText('net_income')).toBeInTheDocument();
  });

  it('explains a loss instead of asking for a figure that is already there', () => {
    renderModal(
      holding({
        valuation_method: 'net_income',
        valuation: {
          available: false,
          method: 'net_income',
          missing: [],
          non_positive_flow: 'net_income',
          models: { free_cash_flow: model(333.17, 20.1) },
        },
      })
    );

    expect(screen.getByText(/zero or negative/)).toBeInTheDocument();
    // Not the "fill them in" wording: there is nothing to fill in.
    expect(screen.queryByText(/Not valued yet/)).not.toBeInTheDocument();
  });

  it('still shows the methods that did work, so a switch is one click away', () => {
    renderModal(
      holding({
        valuation_method: 'net_income',
        valuation: {
          available: false,
          method: 'net_income',
          missing: ['net_income'],
          models: { free_cash_flow: model(333.17, 20.1) },
        },
      })
    );

    expect(toggle(/DFCF-20/)).toHaveTextContent('333.17');
  });
});
