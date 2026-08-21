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
     *   * The PER-FILE 100% entries are the real gate. Those four modules are
     *     fully covered today, and they are the ones where a gap would hurt:
     *     position sizing decides how much money goes on a trade, discipline
     *     duplicates a backend formula, format fixes two bugs that reached
     *     production, and download is eight lines with three load-bearing ones.
     *     A single uncovered branch appearing in any of them fails the build.
     */
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/types/**', '**/*.test.{ts,tsx}'],
      reporter: ['text-summary', 'html', 'json-summary'],
      thresholds: {
        statements: 3,
        branches: 3,
        functions: 1,
        lines: 3,

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
