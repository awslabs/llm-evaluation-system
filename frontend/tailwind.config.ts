import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        foreground: "var(--foreground)",
        claude: {
          bg: "#1a1a1a",
          surface: "#2d2d2d",
          border: "#3d3d3d",
          text: "#f5f5f5",
          muted: "#a0a0a0",
          accent: "#6366f1",
          hover: "#4f46e5",
        },
      },
    },
  },
  plugins: [],
};
export default config;
