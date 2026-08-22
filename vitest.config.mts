import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

/**
 * Vitest rather than Jest: esbuild reads the project's TypeScript and JSX as-is,
 * so there is no babel config, no transform map and no `next/jest` wrapper to
 * keep in step with the Next version. The three dev dependencies this needs are
 * vitest, jsdom and @testing-library/react.
 */
export default defineConfig({
  test: {
    // jsdom everywhere rather than per-file `@vitest-environment` pragmas. Most
    // of what is worth testing here is pure, but a pragma that has to be
    // remembered is one that will be forgotten, and the failure it produces
    // ("document is not defined") reads as a broken test rather than a missing
    // annotation.
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    // Sets IS_REACT_ACT_ENVIRONMENT. See the file -- without it React never
    // flushes an effect in a test, silently.
    setupFiles: ['./vitest.setup.ts'],
    /**
     * Coverage, reported honestly and ratcheted where it matters.
     *
     * The headline number is LOW -- around 3% -- and that is the correct number
     * to publish rather than one massaged upwards. Thirty untested components
     * are in the denominator because they genuinely are untested; excluding
     * them to print a prettier figure would turn this from a measurement into
     * decoration.
     *
     * What IS excluded is code with no runtime behaviour to cover: `src/types`
     * is type declarations that vanish at compile time and would otherwise
     * report a free 100%, dragging the real figure up for nothing.
     *
     * Two kinds of threshold, doing two different jobs:
     *
     *   * The GLOBAL floors sit just under today's numbers. They cannot mean
     *     "well covered" at 3%, and are not trying to -- they catch the case
     *     where tests are deleted or a large untested surface lands, both of
     *     which push the percentage down.
     *
     *   * The PER-FILE 100% entries are the real gate: modules that are fully
     *     covered today, and where a gap would hurt. `positionSizing` decides
     *     how much money goes on a trade; `discipline` duplicates a backend
     *     formula and has to keep agreeing with it; `format` fixes two bugs
     *     that already reached production; `download` is eight lines with
     *     three load-bearing ones; `investmentsApi` is the HTTP layer for a
     *     second book with no domain logic of its own to catch a wrong verb
     *     or URL; `useInvestments` wires thirteen mutations to cache keys, and
     *     eleven of them invalidate the SAME key for eleven different reasons
     *     -- exactly the shape where a new one could invalidate nothing, or
     *     the wrong thing, and every existing assertion would still pass. A
     *     single uncovered branch in any of these six fails the build.
     *
     *   * `PlanModal.tsx` is tested (30 cases) but deliberately NOT in this
     *     list. It lands at 82%/76%/75%/81% -- strong, but the edit-mode
     *     chart replace/delete flow and a few other paths were scoped out of
     *     that pass, so a 100% gate on it would fail today rather than guard
     *     anything. Its coverage still counts toward the global floor above;
     *     it earns the hard gate once those remaining paths are covered.
     *
     *   * `TradeInboxQueue.tsx` is tested (86 cases) and lands at
     *     98.6%/96.9%/100%/100% -- close enough to 100% that it is worth
     *     saying exactly what the remaining gap is, rather than leaving it
     *     looking like an oversight. Four spots, all confirmed unreachable
     *     through the rendered UI rather than merely skipped:
     *
     *       - `draft.disciplines_checked ?? {}` (two call sites): the field
     *         is seeded from `emptyDraft` and every `patchDraft` write
     *         preserves it, so a draft lacking it never exists.
     *       - `if (!position) return;` inside the delete dialog's
     *         `onConfirm`: `position` is `confirmingDelete`, and the button
     *         calling this handler only renders while that is already set.
     *       - `positionsQuery.data ?? []`: only reached after the
     *         isPending/isError early returns, past which React Query
     *         guarantees `data` is defined.
     *       - `if (!name) return;` inside the Manage Rules "Add" handler:
     *         the button is disabled under the exact same condition this
     *         guards against, and confirmed empirically (a probe test) that
     *         jsdom does not dispatch `click` on a disabled button at all --
     *         matching real browsers, not a jsdom quirk.
     *
     *     Not added to the 100% list below because it is not literally
     *     100% -- but nothing here is a gap worth closing, in contrast to
     *     PlanModal's genuinely-untested paths above.
     *
     *   * `src/app/analytics/page.tsx` is tested (105 cases) and lands at a
     *     literal 100%/100%/100%/100% (statements/branches/functions/lines,
     *     155/155 branches). An earlier pass here left one branch --
     *     the final `: null` arm of `metricsQuery.isPending ? ... :
     *     metricsQuery.isError ? ... : m ? ... : null` (around line 777) --
     *     excluded as "unreachable," reasoning that the only falsy value a
     *     mocked queryFn can resolve to is `undefined`, and TanStack Query
     *     throws on that (converting it into `isError`, which IS a real,
     *     separately-tested behaviour). Adversarial review caught that the
     *     reasoning was wrong: TanStack's guard is a strict `=== undefined`
     *     check, not a general falsiness check, so a queryFn resolving to
     *     `null` sails through it, lands the query in `isSuccess` with `m`
     *     falsy, and hits the arm directly. A test now exercises exactly
     *     that, closing the gap for real instead of documenting past it.
     *     Added to the 100% list below since the number is now literal.
     *
     *     Three findings from that same review round were pinned as tests
     *     rather than fixed, since none is a defect in this test suite --
     *     each is a genuine inconsistency in `page.tsx` itself, out of this
     *     pass's scope to change:
     *
     *       - StrategyBreakdownChart's per-row `title` promises a specific
     *         destination ("Open \"X\" in the strategy playbook"), but
     *         every row's `href` is the identical static `/strategies`,
     *         which has no id/query mechanism to receive which strategy was
     *         clicked -- two different tooltips, one indistinguishable
     *         destination.
     *       - The module-level comment "Free text is also allowed" above
     *         `MISTAKE_TAGS` is not backed by any control in `ReviewDrawer`
     *         -- there is no text input, only the fixed 8-tag chip row. A
     *         mistake string outside that list loads into state, lights up
     *         no chip, cannot be seen or removed, yet is still counted in
     *         the "N tags selected" caption and resubmitted verbatim on
     *         every Save.
     *       - (Documented in `page.test.tsx` itself, not repeated here:
     *         the same suite still carries the five findings from the
     *         original pass -- the missing empty-state message on
     *         `r_distribution`, the string-shape `isLoss`/`isUnassigned`
     *         heuristics, the `∞`-not-`—` profit-factor glyph, the
     *         disappearing toolbar window label, the misleading queue-error
     *         badge, and the review-drawer save that never dequeues.)
     *
     *   * `TradeLedger.tsx` is tested (110 cases) and lands at
     *     100%/100%/99.09%/98.06% (lines/functions/statements/branches) --
     *     the strongest result of the four components in this list, and
     *     still deliberately left off the 100% gate below because branches
     *     is not literal. Six spots remain, each confirmed unreachable by a
     *     DIFFERENT mechanism rather than the same one repeated:
     *
     *       - `toMarketDateTimeLocal`'s `?.value ?? '00'` fallback, and its
     *         `hour === '24' ? '00' : ...` branch: the first needs
     *         `Intl.DateTimeFormat.formatToParts` to omit a part type it
     *         always includes for a valid date; the second is a real but
     *         ICU-version-dependent quirk (some ICU builds render midnight
     *         as hour "24") that cannot be forced without mocking `Intl`
     *         itself, which would test the mock, not the component.
     *       - `parseNumber`'s `Number.isFinite(parsed) ? parsed : null`:
     *         every caller reads from a `type="number"` input, and jsdom
     *         (matching real browsers, per this project's own prior probes)
     *         never lets such a field's value become a non-empty,
     *         unparseable string -- invalid input collapses to `""`, which
     *         hits the earlier `if (!trimmed) return null` branch instead.
     *       - `RoundTripHeader`'s `rt.quantity ?? 0` inside
     *         `capitalCommitted`: reachable only with a null `quantity`,
     *         but the same render already calls the unguarded
     *         `quantity.toFixed(8)` inside `formatQuantity` first, which
     *         throws before this line's own fallback ever gets to run --
     *         unreachable WITHOUT crashing, not the same claim as the other
     *         entries here. (`entry_price ?? 0`, the fallback beside it, IS
     *         independently exercised -- a null `entry_price` alone does
     *         not crash anything else.)
     *       - `if (!rt.plan_trade_id) return;` inside `AttachPlanPanel`'s
     *         `onAttach`: that component returns `null` outright whenever
     *         `plan_trade_id` is falsy, so its own Attach button cannot
     *         exist to click in the state this guard checks for -- the
     *         same class of always-true invariant as the next entry.
     *       - `if (!target) return;` inside the fill-delete `ConfirmDialog`'s
     *         `onConfirm`: `target` is `confirmingFill`, captured at click
     *         time, and the Confirm button calling this handler only
     *         renders while that is already set.
     */
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/types/**', '**/*.test.{ts,tsx}'],
      reporter: ['text-summary', 'html', 'json-summary'],
      thresholds: {
        statements: 31,
        branches: 31,
        functions: 35,
        lines: 31,

        'src/lib/positionSizing.ts': {
          statements: 100, branches: 100, functions: 100, lines: 100,
        },
        'src/lib/discipline.ts': {
          statements: 100, branches: 100, functions: 100, lines: 100,
        },
        'src/lib/format.ts': {
          statements: 100, branches: 100, functions: 100, lines: 100,
        },
        'src/lib/download.ts': {
          statements: 100, branches: 100, functions: 100, lines: 100,
        },
        'src/lib/investmentsApi.ts': {
          statements: 100, branches: 100, functions: 100, lines: 100,
        },
        'src/hooks/useInvestments.ts': {
          statements: 100, branches: 100, functions: 100, lines: 100,
        },
        'src/app/analytics/page.tsx': {
          statements: 100, branches: 100, functions: 100, lines: 100,
        },
      },
    },

    // Call history is cleared between tests so one test's calls cannot be
    // counted by the next.
    //
    // clearMocks, NOT restoreMocks. `restoreMocks` calls mockRestore() on every
    // mock, which for a `vi.fn(impl)` created inside a `vi.mock` factory
    // discards the implementation entirely -- the stub then silently returns
    // undefined, and a hook fed undefined by a module it thinks it mocked fails
    // in ways that look like a bug in the hook.
    clearMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,
  },
  resolve: {
    // Mirrors the `@/*` path in tsconfig.json. fileURLToPath rather than
    // reading import.meta.url directly: on Windows a file URL's pathname comes
    // back as "/C:/..." and every import resolves against a directory that does
    // not exist.
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
});
