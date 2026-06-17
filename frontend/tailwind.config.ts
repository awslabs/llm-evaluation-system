import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-geist)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-geist-mono)", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
        serif: ["var(--font-instrument-serif)", "ui-serif", "Georgia", "serif"],
      },
      colors: {
        // Observatory palette — warm dark instrument
        ink: {
          DEFAULT: "#0c0a08",
          elev: "#15120e",
          raised: "#1d1812",
          pressed: "#252017",
        },
        bone: {
          DEFAULT: "#ece6d8",
          dim: "#a39a87",
          mute: "#6f6759",
        },
        rule: {
          DEFAULT: "#2a241d",
          soft: "#1f1a14",
        },
        ember: {
          DEFAULT: "#d87858",
          soft: "#3a1f15",
          deep: "#a35336",
        },
        sage: "#9bb556",
        wheat: "#d4a72c",
        oxide: "#c5524e",
        paper: "#f5efe7",
      },
      letterSpacing: {
        eyebrow: "0.14em",
      },
      keyframes: {
        reveal: {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        cursorPulse: {
          "50%": { opacity: "0" },
        },
      },
      animation: {
        reveal: "reveal 0.7s cubic-bezier(0.2, 0.7, 0.2, 1) both",
        cursor: "cursorPulse 1.05s steps(2) infinite",
      },
    },
  },
  plugins: [],
};
export default config;
