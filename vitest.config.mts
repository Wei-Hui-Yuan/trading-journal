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
