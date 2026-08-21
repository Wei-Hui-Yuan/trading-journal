/** @type {import('postcss-load-config').Config} */
const config = {
  plugins: {
    // Tailwind v4 split its PostCSS plugin into this separate package.
    // autoprefixer is gone too -- v4 handles vendor prefixing itself.
    '@tailwindcss/postcss': {},
  },
};

export default config;
