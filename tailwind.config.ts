import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        obsidian: {
          bg: "#090D16",      // Pure dark canvas
          card: "#111726",    // Slightly lighter dark for structural depth
          border: "#1E293B",  // Subdued lines
          muted: "#64748B",   // Text colors for secondary notes
        },
        win: {
          DEFAULT: "#10B981",       // Vivid Emerald for metrics/text
          glow: "rgba(16, 185, 129, 0.08)",
          border: "rgba(16, 185, 129, 0.25)",
        },
        loss: {
          DEFAULT: "#F43F5E",       // Vivid Rose for drawdowns
          glow: "rgba(244, 63, 94, 0.08)",
          border: "rgba(244, 63, 94, 0.25)",
        }
      },
      boxShadow: {
        "win-glow": "0 0 15px -3px rgba(16, 185, 129, 0.2)",
        "loss-glow": "0 0 15px -3px rgba(244, 63, 94, 0.2)",
      }
    },
  },
  plugins: [],
};
export default config;
