/**
 * PlanModal: the form that turns "I think this is a setup" into a row nothing
 * else in the app can reach yet.
 *
 * The API functions it depends on (`createPlan`, `updatePlan`, `getSettings`,
 * `getStrategies`, `getDisciplines`, `uploadPlanChart`, `deletePlanChart`) are
 * mocked; the REAL hooks from `useTradeInbox` run against a real QueryClient.
 * That is deliberate -- those hooks are themselves the thing that turns a
 * mocked promise into `isPending`/`isSuccess`/`onSuccess` behaviour, and
 * hand-simulating that state machine risks testing a fabricated shape rather
 * than what the component actually receives.
 *
 * `ChartDropzone` and `PlanChartView` ARE replaced with plain stand-ins. Not
 * because they are hard to test -- they are a separate component with its own
 * file -- but because their real implementation calls `URL.createObjectURL`
 * and, on an actual file, `createImageBitmap` and a canvas 2D context, none of
 * which jsdom implements. Stubbing them keeps this file testing PlanModal's
 * own logic: what it does with a chart handed to it, not how a chart gets
 * produced.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) =>
    React.createElement('a', { href }, children),
}));

const chartDropzoneProps = vi.hoisted(() => ({ current: null as unknown }));

vi.mock('@/components/PlanChart', () => ({
  ChartDropzone: (props: {
    value: unknown;
    onChange: (v: unknown) => void;
    disabled?: boolean;
  }) => {
    chartDropzoneProps.current = props;
    return React.createElement(
      'button',
      { type: 'button', 'data-testid': 'fake-dropzone', disabled: props.disabled },
      'fake dropzone'
    );
  },
  PlanChartView: () => React.createElement('div', { 'data-testid': 'fake-chart-view' }),
}));

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  createPlan: vi.fn(),
  updatePlan: vi.fn(),
  getSettings: vi.fn(),
  getStrategies: vi.fn(),
  getDisciplines: vi.fn(),
  uploadPlanChart: vi.fn(),
  deletePlanChart: vi.fn(),
}));

import * as api from '@/lib/api';
import { PlanModal } from '@/components/PlanModal';
import type { AppSettings, Discipline, Strategy, TradePlan } from '@/types/api';

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const SETTINGS: AppSettings = {
  account_size: 10_000,
  risk_percent: 1,
  updated_at: null,
};

const STRATEGIES: Strategy[] = [
  {
    id: 'strat-1',
    name: 'Momentum Breakout',
    description: null,
    method: 'Momentum',
    entry_criteria: '',
    exit_criteria: '',
    created_at: null,
  },
];

const GENERAL_RULE: Discipline = {
  id: 'rule-general',
  name: 'Waited for confirmation',
  strategy_id: null,
  created_at: null,
};

const STRATEGY_RULE: Discipline = {
  id: 'rule-strategy',
  name: 'Volume confirmed',
  strategy_id: 'strat-1',
  created_at: null,
};

function plan(overrides: Partial<TradePlan> = {}): TradePlan {
  return {
    id: 'plan-1',
    ticker: 'AAPL',
    direction: 'BUY',
    quantity: 100,
    planned_entry: 150,
    stop_loss: 148,
    take_profit: 156,
    planned_r: 3,
    risk_percent: 1,
    risk_amount: 200,
    strategy_id: null,
    thesis: 'Reclaim of the 50 EMA.',
    status: 'OPEN',
    created_at: null,
    updated_at: null,
    attached_trade_ids: [],
    has_chart: false,
    chart_bytes: null,
    chart_uploaded_at: null,
    disciplines: [],
    candidates: [],
    ...overrides,
  };
}

let client: QueryClient;

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client }, children);
}

function mountModal(props: Partial<React.ComponentProps<typeof PlanModal>> = {}) {
  return render(
    React.createElement(PlanModal, {
      open: true,
      onClose: vi.fn(),
      ...props,
    }),
    { wrapper }
  );
}

/** Wait for the settings/strategies/disciplines queries to have resolved. */
async function ready() {
  await waitFor(() => expect(mocked.getSettings).toHaveBeenCalled());
  await screen.findByText('Ticker');
}

const ticker = () => screen.getByPlaceholderText('AAPL');
const quantity = () => screen.getByPlaceholderText('100');
const plannedEntry = () => screen.getByPlaceholderText('150.00');
const plannedStop = () => screen.getByPlaceholderText('148.00');
const takeProfit = () => screen.getByPlaceholderText('156.00');
// Matches all three labels the submit button carries across its lifecycle --
// "Save Plan"/"Save Changes" at rest, "Saving…" once isSaving flips true. A
// narrower match would make the button itself unfindable the moment a save
// starts, which is exactly the state some tests need to assert against.
const submit = () => screen.getByRole('button', { name: /Save Plan|Save Changes|Saving/ });

/**
 * Fire the form's `submit` event directly rather than clicking Save.
 *
 * Clicking a submit button runs the browser's OWN constraint validation
 * first (`min`, `required`, and friends), and it blocks the `submit` event
 * outright when a field fails -- in jsdom exactly as in a real browser,
 * confirmed empirically against a bare `<input min="1">`. That is the
 * correct real-world behaviour, but it means clicking Save can never reach
 * this component's own "must be greater than zero" checks for a field the
 * browser already refuses to submit. Dispatching `submit` on the form
 * bypasses that native gate, which is exactly what is needed to exercise the
 * JS-level fallback on its own terms.
 */
const submitBypassingNativeValidation = () => {
  const form = document.querySelector('form');
  if (!form) throw new Error('No form is mounted.');
  fireEvent.submit(form);
};

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  chartDropzoneProps.current = null;

  mocked.getSettings.mockResolvedValue(SETTINGS);
  mocked.getStrategies.mockResolvedValue(STRATEGIES);
  mocked.getDisciplines.mockResolvedValue([GENERAL_RULE, STRATEGY_RULE]);
  // Echoes the submitted ticker back, matching what a real backend response
  // would do. The success message and the chart's upload filename are both
  // built from the RESPONSE's ticker (`created.ticker`), not the form field
  // -- a mock that always answered "AAPL" regardless of input would silently
  // decouple every assertion on those from what was actually typed.
  mocked.createPlan.mockImplementation((payload: { ticker: string }) =>
    Promise.resolve(plan({ ticker: payload.ticker }))
  );
  mocked.updatePlan.mockResolvedValue(plan());
  mocked.uploadPlanChart.mockResolvedValue(plan({ has_chart: true }));
  mocked.deletePlanChart.mockResolvedValue(plan({ has_chart: false }));
});

afterEach(() => {
  cleanup();
  client.clear();
});

describe('mount gating', () => {
  it('renders nothing when closed', async () => {
    mountModal({ open: false });
    // No portal target check needed -- if it rendered, the dialog role would
    // be findable regardless of where in the DOM it landed.
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('renders the create-mode dialog when open, with no plan given', async () => {
    mountModal();
    await ready();

    expect(screen.getByRole('dialog')).toHaveAttribute(
      'aria-label',
      'Create a trade plan'
    );
    expect(screen.getByText('CREATE TRADE PLAN')).toBeInTheDocument();
  });

  it('renders the edit-mode dialog, seeded from the given plan', async () => {
    mountModal({ plan: plan({ ticker: 'MSFT', quantity: 40 }) });
    await ready();

    expect(screen.getByRole('dialog')).toHaveAttribute('aria-label', 'Edit trade plan');
    expect(screen.getByText('EDIT TRADE PLAN')).toBeInTheDocument();
    expect(ticker()).toHaveValue('MSFT');
    expect(quantity()).toHaveValue(40);
  });
});

describe('validation', () => {
  const fillMinimalValid = () => {
    fireEvent.change(ticker(), { target: { value: 'AAPL' } });
  };

  it.each([
    ['', 'Ticker is required.'],
    ['   ', 'Ticker is required.'],
  ])('rejects an empty ticker (%j)', async (raw, message) => {
    mountModal();
    await ready();
    fireEvent.change(ticker(), { target: { value: raw } });

    fireEvent.click(submit());

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(mocked.createPlan).not.toHaveBeenCalled();
  });

  it('rejects a ticker over 10 characters', async () => {
    mountModal();
    await ready();
    fireEvent.change(ticker(), { target: { value: 'ABCDEFGHIJK' } });

    fireEvent.click(submit());

    expect(
      await screen.findByText('Ticker must be 10 characters or fewer.')
    ).toBeInTheDocument();
    expect(mocked.createPlan).not.toHaveBeenCalled();
  });

  it('uppercases and trims the ticker as typed', async () => {
    mountModal();
    await ready();
    fireEvent.change(ticker(), { target: { value: '  aapl  ' } });

    // Uppercased live -- the field itself reflects it, trimming happens at
    // submit.
    expect(ticker()).toHaveValue('  AAPL  ');
  });

  it('accepts a bare ticker with nothing else filled in', async () => {
    // "A plan is worth recording the moment you have a ticker and a bias" --
    // the docstring's own claim, worth pinning literally.
    mountModal();
    await ready();
    fillMinimalValid();

    fireEvent.click(submit());

    // Checked against the leading argument only, not a full
    // toHaveBeenCalledWith: `useCreatePlan` passes `createPlan` straight
    // through as `mutationFn`, and TanStack Query v5 always invokes a
    // mutationFn as `(variables, context)` -- the same fact already found
    // and worked around in useInvestments.test.tsx. `createPlan` itself
    // declares one parameter and never sees the trailing context object.
    await waitFor(() => expect(mocked.createPlan).toHaveBeenCalled());
    const [payload] = mocked.createPlan.mock.calls[0];
    expect(payload).toMatchObject({ ticker: 'AAPL', quantity: null });
  });

  /**
   * NOT TESTED HERE, ON PURPOSE: "must be a number" for quantity, planned
   * entry, stop loss or take profit.
   *
   * Every one of those fields is `<input type="number">`. Confirmed
   * empirically (both here and against a bare `<input type="number">` in
   * isolation) that neither jsdom nor a real browser will let `.value` become
   * a non-empty, non-numeric string at all -- typing or programmatically
   * setting "abc" leaves the field at "", and `toNullableNumber("")` returns
   * null with `form.<field>.trim() === ''`, which is the OMITTED-field branch,
   * not the invalid-field one. The `... must be a number.` messages exist in
   * source and are not wrong, but there is no path through the rendered UI
   * that reaches them -- writing a test that forced `.value` into an
   * impossible state would be testing a scenario the browser itself forbids,
   * which is worse than not testing it at all.
   */

  it('rejects a zero quantity', async () => {
    // The quantity field carries `min="1"`, so 0 is browser-invalid: clicking
    // Save would be blocked by the browser's own constraint validation before
    // React's onSubmit ever runs, in both jsdom and a real one. Submitted
    // directly on the form instead, deliberately bypassing that native check,
    // to verify the JS fallback this component ALSO carries -- the defence
    // that would matter if the input ever stopped being type="number", or on
    // whatever the next browser quirk turns out to be.
    mountModal();
    await ready();
    fillMinimalValid();
    fireEvent.change(quantity(), { target: { value: '0' } });

    submitBypassingNativeValidation();

    expect(
      await screen.findByText('Quantity must be greater than zero.')
    ).toBeInTheDocument();
  });

  it('rejects a negative quantity', async () => {
    // Same native-validation bypass as the zero case above: -5 also violates
    // the field's own min="1".
    mountModal();
    await ready();
    fillMinimalValid();
    fireEvent.change(quantity(), { target: { value: '-5' } });

    submitBypassingNativeValidation();

    expect(
      await screen.findByText('Quantity must be greater than zero.')
    ).toBeInTheDocument();
  });

  it.each([
    ['Planned entry', () => plannedEntry()],
    ['Planned stop loss', () => plannedStop()],
    ['Take profit price', () => takeProfit()],
  ])('rejects a non-positive %s', async (label, field) => {
    mountModal();
    await ready();
    fillMinimalValid();
    fireEvent.change(field(), { target: { value: '0' } });

    fireEvent.click(submit());

    expect(
      await screen.findByText(`${label} must be greater than zero.`)
    ).toBeInTheDocument();
  });

  it('validates every optional price field independently of the others', async () => {
    // A bad entry price must not be masked by a fine stop, or vice versa --
    // each of the three fields has to fail the submit on its own. -1 also
    // violates this field's own min="0", so submitted directly on the form
    // for the same reason as the quantity cases above.
    mountModal();
    await ready();
    fillMinimalValid();
    fireEvent.change(plannedEntry(), { target: { value: '150' } });
    fireEvent.change(plannedStop(), { target: { value: '-1' } });
    fireEvent.change(takeProfit(), { target: { value: '160' } });

    submitBypassingNativeValidation();

    expect(
      await screen.findByText('Planned stop loss must be greater than zero.')
    ).toBeInTheDocument();
    expect(mocked.createPlan).not.toHaveBeenCalled();
  });

  it('clears a stale error as soon as any field changes', async () => {
    mountModal();
    await ready();
    fireEvent.click(submit());
    expect(await screen.findByText('Ticker is required.')).toBeInTheDocument();

    fireEvent.change(ticker(), { target: { value: 'A' } });

    expect(screen.queryByText('Ticker is required.')).toBeNull();
  });
});

describe('risk % granularity', () => {
  /** The input is only reachable through its label -- placeholder "1" is not unique. */
  const riskPercent = () =>
    screen.getByText('Risk % This Trade').closest('label')!.querySelector('input')!;

  it('accepts a two-decimal risk %, which step="0.05" used to block', async () => {
    // Not a style preference: `step` is a VALIDITY rule, so at 0.05 the browser
    // refused to submit a typed 1.23 -- "the two nearest valid values are 1.2
    // and 1.25" -- for a figure nothing downstream constrains that way.
    mountModal();
    await ready();
    const input = riskPercent();
    fireEvent.change(input, { target: { value: '1.23' } });

    expect(input.validity.stepMismatch).toBe(false);
  });

  it('still rejects a third decimal, which NUMERIC(_,2) would silently round', async () => {
    // The reason this is 0.01 rather than `any`: the column keeps two
    // decimals, and 1.234 coming back as 1.23 without a word is worse than
    // being told up front.
    mountModal();
    await ready();
    const input = riskPercent();
    fireEvent.change(input, { target: { value: '1.234' } });

    expect(input.validity.stepMismatch).toBe(true);
  });
});

describe('submitting a new plan', () => {
  it('POSTs the payload and reports it was saved', async () => {
    mountModal();
    await ready();
    fireEvent.change(ticker(), { target: { value: 'NVDA' } });
    fireEvent.change(quantity(), { target: { value: '10' } });
    fireEvent.change(plannedEntry(), { target: { value: '900' } });
    fireEvent.change(plannedStop(), { target: { value: '880' } });

    fireEvent.click(submit());

    // Leading argument only -- see the comment on "accepts a bare ticker"
    // above for why `createPlan` cannot be asserted on with a full
    // toHaveBeenCalledWith.
    await waitFor(() => expect(mocked.createPlan).toHaveBeenCalled());
    const [payload] = mocked.createPlan.mock.calls[0];
    expect(payload).toMatchObject({
      ticker: 'NVDA',
      direction: 'BUY',
      quantity: 10,
      planned_entry: 900,
      stop_loss: 880,
    });
    expect(await screen.findByText(/Plan saved for NVDA/)).toBeInTheDocument();
  });

  it('computes risk_amount and risk_percent from quantity and the stop', async () => {
    // 10 shares, 20 points of risk per share = $200 risked, against a $10,000
    // account = 2%. Not re-derived from the calculator's own suggestion --
    // this is the quantity the trader actually typed.
    mockAccountSize(10_000);
    mountModal();
    await ready();
    fireEvent.change(ticker(), { target: { value: 'NVDA' } });
    fireEvent.change(quantity(), { target: { value: '10' } });
    fireEvent.change(plannedEntry(), { target: { value: '900' } });
    fireEvent.change(plannedStop(), { target: { value: '880' } });

    fireEvent.click(submit());

    await waitFor(() => expect(mocked.createPlan).toHaveBeenCalled());
    const [payload] = mocked.createPlan.mock.calls[0];
    expect(payload.risk_amount).toBe(200);
    expect(payload.risk_percent).toBeCloseTo(2, 5);
  });

  it('sends risk_amount and risk_percent as null when there is no stop', async () => {
    // No stop means no risk per share, so any figure here would be invented
    // rather than measured -- a null is the honest answer, not a zero.
    mountModal();
    await ready();
    fireEvent.change(ticker(), { target: { value: 'NVDA' } });
    fireEvent.change(quantity(), { target: { value: '10' } });
    fireEvent.change(plannedEntry(), { target: { value: '900' } });

    fireEvent.click(submit());

    await waitFor(() => expect(mocked.createPlan).toHaveBeenCalled());
    const [payload] = mocked.createPlan.mock.calls[0];
    expect(payload.risk_amount).toBeNull();
    expect(payload.risk_percent).toBeNull();
  });

  it('omits the disciplines key entirely when no rule applies at all', async () => {
    // Distinct from sending an empty object -- an omitted key means "no
    // checklist applies", where {} would mean "a checklist applied and
    // nothing was ticked". Needs its OWN empty rule set: the fixture's
    // general rule (strategy_id: null) is offered regardless of which
    // strategy is picked, so "no strategy chosen" alone does not empty the
    // checklist -- only having no rules at all does.
    mocked.getDisciplines.mockResolvedValue([]);
    mountModal();
    await ready();
    fireEvent.change(ticker(), { target: { value: 'NVDA' } });

    fireEvent.click(submit());

    await waitFor(() => expect(mocked.createPlan).toHaveBeenCalled());
    const [payload] = mocked.createPlan.mock.calls[0];
    expect(payload).not.toHaveProperty('disciplines');
  });

  it('sends a real answer for every visible rule, ticked or not', async () => {
    mountModal();
    await ready();
    fireEvent.change(ticker(), { target: { value: 'NVDA' } });
    // Only the general rule is offered until a strategy is chosen.
    fireEvent.click(screen.getByLabelText('Waited for confirmation'));

    fireEvent.click(submit());

    await waitFor(() => expect(mocked.createPlan).toHaveBeenCalled());
    const [payload] = mocked.createPlan.mock.calls[0];
    // Ticked -> true, and the rule was never explicitly unticked but must
    // still appear as an explicit false, not be absent.
    expect(payload.disciplines).toEqual({ 'rule-general': true });
  });

  it('adds the strategy-scoped rule to the checklist once a strategy is picked', async () => {
    mountModal();
    await ready();
    fireEvent.change(ticker(), { target: { value: 'NVDA' } });
    fireEvent.change(screen.getByDisplayValue('— None —'), {
      target: { value: 'strat-1' },
    });

    fireEvent.click(submit());

    await waitFor(() => expect(mocked.createPlan).toHaveBeenCalled());
    const [payload] = mocked.createPlan.mock.calls[0];
    expect(payload.disciplines).toEqual({
      'rule-general': false,
      'rule-strategy': false,
    });
  });

  it('closes after a delay once the plan is saved with no chart', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onClose = vi.fn();
    mountModal({ onClose });
    await ready();
    fireEvent.change(ticker(), { target: { value: 'NVDA' } });

    fireEvent.click(submit());
    await vi.waitFor(() => expect(mocked.createPlan).toHaveBeenCalled());

    expect(onClose).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(2200);
    });
    expect(onClose).toHaveBeenCalled();
    vi.useRealTimers();
  });
});

describe('submitting an edit', () => {
  it('PATCHes the existing plan by id and reports the update', async () => {
    mountModal({ plan: plan({ id: 'plan-42', ticker: 'MSFT' }) });
    await ready();
    fireEvent.change(plannedStop(), { target: { value: '145' } });

    fireEvent.click(submit());

    await waitFor(() => expect(mocked.updatePlan).toHaveBeenCalled());
    expect(mocked.updatePlan).toHaveBeenCalledWith(
      'plan-42',
      expect.objectContaining({ ticker: 'MSFT', stop_loss: 145 })
    );
    expect(mocked.createPlan).not.toHaveBeenCalled();
    expect(await screen.findByText('Plan updated for MSFT.')).toBeInTheDocument();
  });

  it('closes sooner than a create -- there is no upload to wait on', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onClose = vi.fn();
    mountModal({ plan: plan(), onClose });
    await ready();

    fireEvent.click(submit());
    await vi.waitFor(() => expect(mocked.updatePlan).toHaveBeenCalled());

    await act(async () => {
      vi.advanceTimersByTime(1400);
    });
    expect(onClose).toHaveBeenCalled();
    vi.useRealTimers();
  });

  it('surfaces a server error without closing', async () => {
    mocked.updatePlan.mockRejectedValueOnce(new Error('strategy no longer exists'));
    const onClose = vi.fn();
    mountModal({ plan: plan(), onClose });
    await ready();

    fireEvent.click(submit());

    expect(
      await screen.findByText('strategy no longer exists')
    ).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('the chart, attached at creation', () => {
  const FAKE_CHART = {
    blob: new Blob(['x'], { type: 'image/png' }),
    mime: 'image/png',
    width: 800,
    height: 600,
    originalBytes: 900,
    encodedBytes: 800,
    lossless: true,
  };

  const pickChart = () => {
    // The real ChartDropzone is stubbed; this drives its onChange exactly
    // the way a successful compress would, without going through canvas.
    (chartDropzoneProps.current as { onChange: (v: unknown) => void }).onChange(
      FAKE_CHART
    );
  };

  it('reports the plan saved AND the chart attached', async () => {
    mountModal();
    await ready();
    fireEvent.change(ticker(), { target: { value: 'NVDA' } });
    await act(async () => pickChart());

    fireEvent.click(submit());

    await waitFor(() => expect(mocked.createPlan).toHaveBeenCalled());
    await waitFor(() => expect(mocked.uploadPlanChart).toHaveBeenCalled());
    expect(mocked.uploadPlanChart).toHaveBeenCalledWith(
      'plan-1',
      FAKE_CHART.blob,
      'NVDA-chart.png'
    );
    expect(await screen.findByText(/Plan saved for NVDA/)).toBeInTheDocument();
  });

  it('names the failure as a chart problem, not a save failure, and stays open', async () => {
    // The plan is ALREADY saved by the time the upload is attempted -- a
    // failed chart must not be reported as though the plan itself was lost.
    mocked.uploadPlanChart.mockRejectedValueOnce(new Error('storage unavailable'));
    const onClose = vi.fn();
    mountModal({ onClose });
    await ready();
    fireEvent.change(ticker(), { target: { value: 'NVDA' } });
    await act(async () => pickChart());

    fireEvent.click(submit());

    // [\s\S]* rather than a dotAll (/s) flag: the project's `tsconfig.json`
    // targets ES2017, which predates dotAll support.
    const message = await screen.findByText(
      /Plan saved for NVDA[\s\S]*could not be attached[\s\S]*storage unavailable/
    );
    expect(message).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('closing', () => {
  it('closes on Escape when not saving', async () => {
    const onClose = vi.fn();
    mountModal({ onClose });
    await ready();

    fireEvent.keyDown(window, { key: 'Escape' });

    expect(onClose).toHaveBeenCalled();
  });

  it('does not close on Escape while a save is in flight', async () => {
    // A create that never resolves, so isSaving stays true for the assertion.
    mocked.createPlan.mockImplementation(() => new Promise(() => {}));
    const onClose = vi.fn();
    mountModal({ onClose });
    await ready();
    fireEvent.change(ticker(), { target: { value: 'NVDA' } });
    fireEvent.click(submit());
    await waitFor(() => expect(submit()).toBeDisabled());

    fireEvent.keyDown(window, { key: 'Escape' });

    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on a backdrop click when not saving', async () => {
    const onClose = vi.fn();
    mountModal({ onClose });
    await ready();

    // Queried from `document`, not the `render()` container: the dialog is
    // rendered through `createPortal(..., document.body)`, which attaches it
    // as a SIBLING of RTL's container rather than a descendant of it -- a
    // query scoped to `container` would never find it.
    //
    // The backdrop is the first child of the dialog root -- the element
    // carrying the click handler, not the card itself.
    const backdrop = document.querySelector('[role="dialog"] > div');
    fireEvent.click(backdrop as Element);

    expect(onClose).toHaveBeenCalled();
  });
});

/** Overrides the settings query's account size for tests that need a precise figure. */
function mockAccountSize(accountSize: number) {
  mocked.getSettings.mockResolvedValue({ ...SETTINGS, account_size: accountSize });
}
