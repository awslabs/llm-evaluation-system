import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// The app calls the backend same-origin via relative /api/* (and /inspect/*)
// URLs. In production a single FastAPI process serves both the built SPA and
// those routes, so there is no proxy. In dev, Vite serves the SPA and proxies
// those prefixes to whichever backend you point BACKEND_URL at:
//   - `eval-mcp view`  (the MCP results viewer, mode: "viewer") → :4001
//   - the full backend (mode: "full", chat/sessions/auth)       → :8000
const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";
const proxy = Object.fromEntries(
  ["/api", "/inspect", "/oauth2"].map((p) => [
    p,
    { target: backendUrl, changeOrigin: true },
  ]),
);

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: { port: 5173, proxy },
  // Single-origin production: emit into the dir the Python viewer serves and
  // that the wheel ships (see build:viewer / pyproject.toml package data).
  build: { outDir: "dist", emptyOutDir: true },
});
