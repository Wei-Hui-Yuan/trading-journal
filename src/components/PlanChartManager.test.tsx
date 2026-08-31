/**
 * PlanChartManager: attach, replace or remove a plan's chart, for a plan
 * that already has an id.
 *
 * Extracted from the Plan modal's own edit mode, where this exact state
 * machine lived before there was a second caller (the Journal) that needed
 * it. These tests exercise the REAL component -- the API functions
 * (`uploadPlanChart`, `deletePlanChart`) are mocked and the REAL
 * `useUploadPlanChart`/`useDeletePlanChart` hooks run against a real
 * QueryClient, the same reasoning PlanModal.test.tsx documents: those hooks
 * are what turn a mocked promise into the isPending/onSuccess behaviour this
 * component actually branches on.
 *
 * `ChartDropzone` and `PlanChartView` ARE stubbed, for the same reason
 * PlanModal.test.tsx stubs them: their real implementations call
 * `URL.createObjectURL` and, on an actual file, `createImageBitmap` and a
 * canvas 2D context, none of which jsdom implements. Because
 * PlanChartManager now lives in its own file and imports both from
 * `@/components/PlanChart` as a normal cross-module import, mocking that
 * module here reaches PlanChartManager's real internals cleanly -- no
 * same-file entanglement the way there would be if all three still lived in
 * one file.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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
  uploadPlanChart: vi.fn(),
  deletePlanChart: vi.fn(),
}));

import { PlanChartManager } from '@/components/PlanChartManager';
import { deletePlanChart, uploadPlanChart } from '@/lib/api';
import type { TradePlan } from '@/types/api';

const mockUpload = vi.mocked(uploadPlanChart);
const mockDelete = vi.mocked(deletePlanChart);

/** A minimal plan, complete enough for the two mutations to resolve against. */
const plan = (over: Partial<TradePlan> = {}): TradePlan =>
  ({ id: 'plan-1', ticker: 'AAPL', has_chart: false, ...over }) as TradePlan;

let client: QueryClient;

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  chartDropzoneProps.current = null;
  mockUpload.mockResolvedValue(plan({ has_chart: true }));
  mockDelete.mockResolvedValue(plan({ has_chart: false }));
});

afterEach(() => cleanup());

function mountManager(props: Partial<React.ComponentProps<typeof PlanChartManager>> = {}) {
  const onHasChartChange = props.onHasChartChange ?? vi.fn();
  render(
    <QueryClientProvider client={client}>
      <PlanChartManager
        planId="plan-1"
        ticker="AAPL"
        hasChart={false}
        onHasChartChange={onHasChartChange}
        {...props}
      />
    </QueryClientProvider>
  );
  return onHasChartChange;
}

/** Hand the stubbed dropzone a chart, the way a real selection would. */
function pickFakeChart() {
  const onChange = (chartDropzoneProps.current as { onChange: (c: unknown) => void }).onChange;
  onChange({
    blob: new Blob(['fake'], { type: 'image/webp' }),
    mime: 'image/webp',
    width: 100,
    height: 100,
    encodedBytes: 4,
    originalBytes: 4,
    lossless: true,
  });
}

describe('with no chart yet', () => {
  it('offers the dropzone straight away, not a Replace/Remove pair', () => {
    mountManager({ hasChart: false });

    expect(screen.getByTestId('fake-dropzone')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Replace' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Remove/ })).not.toBeInTheDocument();
  });

  it('uploads under the plan id and ticker, and reports success', async () => {
    const onHasChartChange = mountManager({ hasChart: false, planId: 'plan-7', ticker: 'MSFT' });

    pickFakeChart();
    fireEvent.click(await screen.findByRole('button', { name: 'Attach chart' }));

    await waitFor(() => expect(mockUpload).toHaveBeenCalled());
    const [planId, , filename] = mockUpload.mock.calls[0];
    expect(planId).toBe('plan-7');
    expect(filename).toBe('MSFT-chart.webp');
    expect(onHasChartChange).toHaveBeenCalledWith(true);
  });

  it('reports an upload failure without losing the picked image', async () => {
    mockUpload.mockRejectedValue(new Error('Storage is unavailable.'));
    const onHasChartChange = mountManager({ hasChart: false });

    pickFakeChart();
    fireEvent.click(await screen.findByRole('button', { name: 'Attach chart' }));

    expect(await screen.findByText('Storage is unavailable.')).toBeInTheDocument();
    // The failure is named, not swallowed into a false "attached".
    expect(onHasChartChange).not.toHaveBeenCalled();
  });
});

describe('with a chart already attached', () => {
  it('shows the stored chart and a Replace/Remove pair, not the dropzone', () => {
    mountManager({ hasChart: true });

    expect(screen.getByTestId('fake-chart-view')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Replace' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Remove/ })).toBeInTheDocument();
    expect(screen.queryByTestId('fake-dropzone')).not.toBeInTheDocument();
  });

  it('opens the dropzone on Replace, labelled for a replacement rather than a first attach', async () => {
    mountManager({ hasChart: true });

    fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
    expect(screen.getByTestId('fake-dropzone')).toBeInTheDocument();

    pickFakeChart();
    expect(
      await screen.findByRole('button', { name: 'Save replacement' })
    ).toBeInTheDocument();
  });

  it('lets Replace be cancelled back to the stored chart', () => {
    mountManager({ hasChart: true });

    fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.getByTestId('fake-chart-view')).toBeInTheDocument();
    expect(screen.queryByTestId('fake-dropzone')).not.toBeInTheDocument();
  });

  it('asks for confirmation before removing, and does not delete on cancel', () => {
    mountManager({ hasChart: true });

    fireEvent.click(screen.getByRole('button', { name: /Remove/ }));
    expect(screen.getByText(/Deletes it from storage/)).toBeInTheDocument();

    // Two "Cancel"-accessible buttons once the dialog is open: the dialog's
    // own icon-only close button (aria-label="Cancel") and its text button.
    // Either one dismisses without deleting, so either is a valid target --
    // the last one keeps this in the same by-position style as "Remove".
    const cancels = screen.getAllByRole('button', { name: 'Cancel' });
    fireEvent.click(cancels[cancels.length - 1]);
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('deletes only after the confirmation is accepted', async () => {
    const onHasChartChange = mountManager({ hasChart: true, planId: 'plan-9' });

    fireEvent.click(screen.getByRole('button', { name: /Remove/ }));
    // Two buttons named "Remove" once the dialog opens: the trigger, still
    // in the DOM behind it, and the dialog's own confirm button.
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[1]);

    // TanStack Query v5 calls a mutationFn passed directly (as
    // useDeletePlanChart passes deletePlanChart) with a second, internal
    // context argument -- expect.anything() rather than a bare
    // toHaveBeenCalledWith('plan-9'), which is arity-strict and would never
    // match the real two-argument call.
    await waitFor(() =>
      expect(mockDelete).toHaveBeenCalledWith('plan-9', expect.anything())
    );
    expect(onHasChartChange).toHaveBeenCalledWith(false);
  });

  it('reports a delete failure and keeps the chart on screen', async () => {
    mockDelete.mockRejectedValue(new Error('Could not reach storage.'));
    const onHasChartChange = mountManager({ hasChart: true });

    fireEvent.click(screen.getByRole('button', { name: /Remove/ }));
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[1]);

    expect(await screen.findByText('Could not reach storage.')).toBeInTheDocument();
    expect(onHasChartChange).not.toHaveBeenCalled();
    expect(screen.getByTestId('fake-chart-view')).toBeInTheDocument();
  });
});

