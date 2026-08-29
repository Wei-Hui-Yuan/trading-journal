/**
 * SizingScratchpad: the fast "what would this cost me" surface.
 *
 * This component had no test file at all until now. It reached production
 * calling `computeSizing` with the account size hard-coded to null on saved
 * rows — so a note could report the risk of a quantity already typed but
 * could never suggest one — and carrying a dead price formatter from an R
 * ladder that was scoped and never built. Both survived because nothing here
 * was ever asserted on.
 *
 * The API functions are mocked; the REAL hooks run against a real
 * QueryClient, for the same reason PlanModal's suite does it that way — those
 * hooks are what turn a mocked promise into the loading and success states
 * the component actually branches on.
 *
 * `PositionSizingPanel` is NOT stubbed. It has its own suite, but the thing
 * most worth pinning here is that this page hands it real inputs, and a
 * stand-in would assert only that a component was rendered.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) =>
    React.createElement('a', { href }, children),
}));

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getSettings: vi.fn(),
  getSizingScratchpad: vi.fn(),
  createSizingEntry: vi.fn(),
  updateSizingEntry: vi.fn(),
  deleteSizingEntry: vi.fn(),
  promoteSizingEntry: vi.fn(),
}));

import { SizingScratchpad } from '@/components/SizingScratchpad';
import {
  getSettings,
  getSizingScratchpad,
  updateSizingEntry,
} from '@/lib/api';
import type { SizingScratchpadEntry } from '@/types/sizing';

const mockGetSettings = vi.mocked(getSettings);
const mockList = vi.mocked(getSizingScratchpad);
const mockUpdate = vi.mocked(updateSizingEntry);

/** A saved note, complete enough to be sized. */
const note = (
  overrides: Partial<SizingScratchpadEntry> = {}
): SizingScratchpadEntry => ({
  id: 'note-1',
  ticker: 'NVDA',
  direction: 'BUY',
  entry: 100,
  stop_loss: 95,
  take_profit: null,
  quantity: null,
  created_at: new Date().toISOString(),
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
  mockGetSettings.mockResolvedValue({
    account_size: 10_000,
    risk_percent: 1,
    updated_at: null,
  });
  mockList.mockResolvedValue([]);
  mockUpdate.mockImplementation((id, payload) =>
    Promise.resolve({ ...note({ id }), ...payload })
  );
});

afterEach(cleanup);

function renderPad() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SizingScratchpad />
    </QueryClientProvider>
  );
}

/** The Quick add price fields, by their visible label. */
const field = (label: string) =>
  screen.getByLabelText(new RegExp(`^${label}$`, 'i'));

describe('the risk % control', () => {
  it('defaults to the saved account risk once settings arrive', async () => {
    renderPad();

    await waitFor(() =>
      expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1)
    );
  });

  it('re-prices the suggestion when a preset is clicked', async () => {
    renderPad();
    await waitFor(() => expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1));

    fireEvent.change(field('Entry'), { target: { value: '100' } });
    fireEvent.change(field('Stop'), { target: { value: '95' } });

    // $10,000 at 1% is $100 of budget against $5 of risk per share.
    expect(
      await screen.findByRole('button', { name: 'Use 20 shares' })
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '2%' }));

    // Doubling the risk doubles the position, which is the whole point of
    // the control being here rather than buried in settings.
    expect(
      await screen.findByRole('button', { name: 'Use 40 shares' })
    ).toBeInTheDocument();
  });

  it('lets a typed value win over the account default', async () => {
    renderPad();
    await waitFor(() => expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1));

    fireEvent.change(field('Entry'), { target: { value: '100' } });
    fireEvent.change(field('Stop'), { target: { value: '95' } });
    fireEvent.change(screen.getByLabelText(/Risk percent/i), {
      target: { value: '0.5' },
    });

    expect(
      await screen.findByRole('button', { name: 'Use 10 shares' })
    ).toBeInTheDocument();
  });

  it('survives being cleared, rather than snapping back to the default', async () => {
    // The override is held as a string precisely so an empty field stays
    // empty. Seeding into state with an effect would refill it.
    renderPad();
    await waitFor(() => expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1));

    fireEvent.change(screen.getByLabelText(/Risk percent/i), {
      target: { value: '' },
    });

    expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(null);
  });
});

describe('the live preview', () => {
  it('stays hidden until there is an entry and a stop to size', async () => {
    renderPad();
    await waitFor(() => expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1));

    expect(screen.queryByText('Take Profit Targets')).not.toBeInTheDocument();

    fireEvent.change(field('Entry'), { target: { value: '100' } });
    expect(screen.queryByText('Take Profit Targets')).not.toBeInTheDocument();

    fireEvent.change(field('Stop'), { target: { value: '95' } });
    expect(await screen.findByText('Take Profit Targets')).toBeInTheDocument();
  });

  it('shows the whole R ladder, which the old strip discarded entirely', async () => {
    renderPad();
    await waitFor(() => expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1));

    fireEvent.change(field('Entry'), { target: { value: '100' } });
    fireEvent.change(field('Stop'), { target: { value: '95' } });

    await screen.findByText('Take Profit Targets');
    expect(
      screen.getAllByRole('button', { name: /^Set take profit to/ })
    ).toHaveLength(4);
  });

  it('writes a picked target into the Target field', async () => {
    renderPad();
    await waitFor(() => expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1));

    fireEvent.change(field('Entry'), { target: { value: '100' } });
    fireEvent.change(field('Stop'), { target: { value: '95' } });

    fireEvent.click(await screen.findByRole('button', { name: /\(3R\)$/ }));

    expect(field('Target')).toHaveValue(115);
  });

  it('says what the entered quantity risks, not only the budget', async () => {
    // Reproduces a real screenshot: $2,500 at 1% shows a $25 risk budget, but
    // 2 shares at $10 of risk each put $20 on the line. The page showed the
    // budget and left the trader to notice the floor had cost them $5 of it.
    mockGetSettings.mockResolvedValue({
      account_size: 2_500,
      risk_percent: 1,
      updated_at: null,
    });
    renderPad();
    await waitFor(() => expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1));

    fireEvent.change(field('Entry'), { target: { value: '150' } });
    fireEvent.change(field('Stop'), { target: { value: '140' } });
    fireEvent.change(field('Shares'), { target: { value: '2' } });

    expect(await screen.findByText('$25.00')).toBeInTheDocument();
    expect(
      screen.getByText(/Planning 2 shares — risking/)
    ).toBeInTheDocument();
    expect(screen.getByText('$20.00')).toBeInTheDocument();
    expect(screen.getByText(/0\.80% of account/)).toBeInTheDocument();
  });

  it('does not claim a scratchpad note is saved anywhere', async () => {
    // The Plan modal's copy of this line ends "Saved with the plan." Nothing
    // here stores a risk figure, so the sentence must not say it does.
    renderPad();
    await waitFor(() => expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1));

    fireEvent.change(field('Entry'), { target: { value: '100' } });
    fireEvent.change(field('Stop'), { target: { value: '95' } });
    fireEvent.change(field('Shares'), { target: { value: '10' } });

    expect(await screen.findByText(/Planning 10 shares/)).toBeInTheDocument();
    expect(screen.queryByText(/Saved with the plan/)).not.toBeInTheDocument();
  });

  it('names an inverted stop instead of silently showing nothing', async () => {
    renderPad();
    await waitFor(() => expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1));

    fireEvent.change(field('Entry'), { target: { value: '100' } });
    fireEvent.change(field('Stop'), { target: { value: '105' } });

    expect(
      await screen.findByText(/stop must sit below the entry/)
    ).toBeInTheDocument();
  });
});

describe('a saved note', () => {
  it('suggests a size, which it could not do while account size was hard-coded null', async () => {
    // The bug this PR fixes. `useRowSizing` passed accountSize: null, so a
    // saved row could report the risk of a quantity already typed but never
    // propose one -- the reason a real NVDA/ADBE/PLTR note showed nothing but
    // "risk $24.00".
    mockList.mockResolvedValue([note()]);
    renderPad();

    expect(await screen.findByText('20 sh')).toBeInTheDocument();
  });

  it('adopts that suggestion when it is clicked', async () => {
    mockList.mockResolvedValue([note()]);
    renderPad();

    fireEvent.click(await screen.findByRole('button', { name: /suggested 20 sh/i }));

    await waitFor(() =>
      expect(mockUpdate).toHaveBeenCalledWith('note-1', { quantity: 20 })
    );
  });

  it('stops offering a suggestion the note has already taken', async () => {
    // Telling someone to adopt the quantity they are already holding is
    // noise in a row that has very little space to spend.
    mockList.mockResolvedValue([note({ quantity: 20 })]);
    renderPad();

    // The row itself has rendered...
    expect(await screen.findByDisplayValue('NVDA')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /suggested/i })
    ).not.toBeInTheDocument();
  });

  it('re-prices with the risk control, not with the saved default', async () => {
    mockList.mockResolvedValue([note()]);
    renderPad();

    expect(await screen.findByText('20 sh')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '2%' }));

    expect(await screen.findByText('40 sh')).toBeInTheDocument();
  });

  it('still reports the risk of a quantity already entered', async () => {
    // The one thing the row could always do. Pinned so the new suggestion
    // path does not displace it.
    mockList.mockResolvedValue([note({ quantity: 10 })]);
    renderPad();

    expect(await screen.findByText('$50.00')).toBeInTheDocument();
  });
});

describe('the scaled exit planner', () => {
  /** Get the quick-add form into a state where sizing exists. */
  async function sized(qty = '100') {
    renderPad();
    await waitFor(() => expect(screen.getByLabelText(/Risk percent/i)).toHaveValue(1));
    fireEvent.change(field('Entry'), { target: { value: '100' } });
    fireEvent.change(field('Stop'), { target: { value: '95' } });
    fireEvent.change(field('Shares'), { target: { value: qty } });
    await screen.findByText('Scaled exit');
  }

  it('appears once there is something to scale out of', async () => {
    await sized();

    expect(screen.getByText('Scaled exit')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Add a slice/ })).toBeInTheDocument();
  });

  it('blends two slices into one average, in R and dollars', async () => {
    // 50% out at 105 is 1R, 50% at 115 is 3R. Blended 2R, and on 100 shares
    // that is 50x$5 + 50x$15 = $1,000.
    await sized();

    fireEvent.click(screen.getByRole('button', { name: /Add a slice/ }));
    fireEvent.change(screen.getByLabelText('Percent out, slice 1'), {
      target: { value: '50' },
    });
    fireEvent.change(screen.getByLabelText('Exit price, slice 1'), {
      target: { value: '105' },
    });

    fireEvent.click(screen.getByRole('button', { name: /Add a slice/ }));
    fireEvent.change(screen.getByLabelText('Percent out, slice 2'), {
      target: { value: '50' },
    });
    fireEvent.change(screen.getByLabelText('Exit price, slice 2'), {
      target: { value: '115' },
    });

    expect(await screen.findByText('2.00R')).toBeInTheDocument();
    expect(screen.getByText(/\$1,000\.00/)).toBeInTheDocument();
    expect(screen.getByText(/across 100% of the position/)).toBeInTheDocument();
  });

  it('says how much is still running when the plan does not cover it all', async () => {
    // Without this the blended figure reads as the whole position's outcome.
    await sized();

    fireEvent.click(screen.getByRole('button', { name: /Add a slice/ }));
    fireEvent.change(screen.getByLabelText('Percent out, slice 1'), {
      target: { value: '60' },
    });
    fireEvent.change(screen.getByLabelText('Exit price, slice 1'), {
      target: { value: '110' },
    });

    expect(await screen.findByText(/40% still running/)).toBeInTheDocument();
  });

  it('refuses to let a plan sell more than the position holds', async () => {
    await sized();

    fireEvent.click(screen.getByRole('button', { name: /Add a slice/ }));
    fireEvent.change(screen.getByLabelText('Percent out, slice 1'), {
      target: { value: '130' },
    });
    fireEvent.change(screen.getByLabelText('Exit price, slice 1'), {
      target: { value: '110' },
    });

    expect(
      await screen.findByText(/130% of a position you only hold 100% of/)
    ).toBeInTheDocument();
  });

  it('warns when a slice is too small to sell a whole share', async () => {
    // The case a small account actually hits: 10% of 2 shares is 0.2, which
    // sells nothing, yet still counts towards the blend.
    await sized('2');

    fireEvent.click(screen.getByRole('button', { name: /Add a slice/ }));
    fireEvent.change(screen.getByLabelText('Percent out, slice 1'), {
      target: { value: '10' },
    });
    fireEvent.change(screen.getByLabelText('Exit price, slice 1'), {
      target: { value: '110' },
    });

    expect(
      await screen.findByText(/sells no whole shares at 2 shares/)
    ).toBeInTheDocument();
  });

  it('removes a slice', async () => {
    await sized();

    fireEvent.click(screen.getByRole('button', { name: /Add a slice/ }));
    expect(screen.getByLabelText('Percent out, slice 1')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Remove slice 1' }));

    expect(screen.queryByLabelText('Percent out, slice 1')).not.toBeInTheDocument();
  });
});
