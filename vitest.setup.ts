/**
 * React 18 refuses to flush effects inside `act()` unless this flag is set, and
 * @testing-library/react's automatic act-wrapping keys off it too.
 *
 * Without it, `render()` mounts the component but no `useEffect` ever runs — and
 * nothing warns. Every test asserting that an effect DID something fails, while
 * every test asserting an effect did NOT do something passes for entirely the
 * wrong reason. That second half is why this lives here rather than in each test
 * file: a suite that is green because its effects never ran is worse than no
 * suite at all.
 */

import '@testing-library/jest-dom/vitest';

// `export {}` makes this a module, which is what `declare global` requires.
export {};

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
