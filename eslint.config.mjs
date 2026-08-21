import next from 'eslint-config-next/core-web-vitals';

/**
 * ESLint, standing on its own for the first time.
 *
 * `package.json` has carried a `lint` script since the project started, but no
 * config ever existed -- so `next lint` could never actually run, and the one
 * check nobody noticed was missing because the command looked present. Next 16
 * removed `next lint` outright, which forced the issue rather than created it.
 *
 * Flat config, not `.eslintrc`: @next/eslint-plugin-next defaults to flat in 16
 * and ESLint 10 drops legacy config support entirely, so starting on the format
 * with a future costs nothing today.
 *
 * `core-web-vitals` rather than the base `next` config -- the same rules plus
 * the performance ones Next escalates to errors. This app is read over a ~218ms
 * round trip from Singapore, which makes those the rules most worth having on.
 *
 * THE WARNING BASELINE. `npm run lint` runs with `--max-warnings 23`, which is
 * exactly the count this config produces today. That turns the existing backlog
 * into a ratchet: the 23 known warnings pass, and the 24th fails the build. A
 * warning nobody is forced to look at is the same as a rule that is switched
 * off, and this is the cheapest way to keep them real without demanding a risky
 * refactor first. If you fix some, lower the number.
 */
const config = [
  {
    // Build output, dependencies, generated types, and the Python API.
    // `.next/dev` is new in 16, which split dev output so dev and build can run
    // concurrently.
    //
    // `coverage/**` matters more than it looks: the HTML reporter writes
    // istanbul's own bundled assets (prettify.js, sorter.js, block-navigation.js)
    // there, and linting them adds three warnings that have nothing to do with
    // this codebase -- enough to trip the --max-warnings ratchet on a run that
    // changed no source at all.
    ignores: [
      '.next/**',
      'coverage/**',
      'node_modules/**',
      'next-env.d.ts',
      'api/**',
    ],
  },

  ...next,

  {
    /**
     * The two React Compiler rules that arrived with eslint-plugin-react-hooks
     * v7, kept as WARNINGS rather than errors -- and deliberately not switched
     * off.
     *
     * They fire 22 times, and every instance inspected is a correct, idiomatic
     * pattern that this newer rule set simply disagrees with:
     *
     *   * `useEffect(() => setMounted(true), [])` -- the standard SSR-safe
     *     portal mount guard, in ConfirmDialog and every modal.
     *   * PlanChart deriving an object URL from a blob into state with a
     *     revoke on cleanup. That cannot move into render: it allocates a
     *     resource that has to be released.
     *   * SyncBrokerButton and useSyncRunWatcher reacting to a server-side
     *     run going from `running` to a terminal outcome -- reacting to
     *     external state is what an effect is for, and that behaviour is
     *     pinned by tests.
     *
     * So the honest severity is "worth reading", not "wrong". Escalating them
     * to errors would mean restructuring working, tested code inside a
     * dependency upgrade, trading real regression risk for no correctness gain.
     * Silencing them entirely would throw away the one signal that might
     * eventually matter -- if React's compiler ever makes these patterns
     * genuinely incorrect, the warnings are already here waiting.
     */
    rules: {
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/refs': 'warn',

      /**
       * Off, not warned: this app deliberately does not use `next/image`.
       *
       * The three `<img>` tags are plan chart screenshots served as blob URLs
       * from a private Supabase bucket, proxied through the API so the bucket
       * needs no public policy. `next/image` would route a trader's private
       * chart through Vercel's optimizer, need `remotePatterns` for a URL that
       * is generated per-render, and cannot handle a `blob:` source at all.
       * Avoiding it is also why `sharp` never loads -- the reason its CVEs are
       * unreachable here.
       */
      '@next/next/no-img-element': 'off',
    },
  },

  {
    // Tests legitimately do what application code must not: reach into a
    // module to force a path, and assert on shapes that are malformed on
    // purpose. An `any` here describes the test, not a lapse in it.
    files: ['**/*.test.ts', '**/*.test.tsx', 'vitest.setup.ts'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-unused-expressions': 'off',
    },
  },
];

export default config;
