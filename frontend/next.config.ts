import type { NextConfig } from "next";

// Build mode:
//   BUILD_MODE=export  → static HTML export for bundling into eval_mcp/viewer_static (default for eval-mcp viewer).
//   BUILD_MODE=standalone → Node server for Docker/K8s deployment of the full platform.
const buildMode = (process.env.BUILD_MODE || "export") as "export" | "standalone";

const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: buildMode,
  // Rewrites only apply in standalone mode; the static export is served by
  // the Python viewer, which handles routing locally.
  async rewrites() {
    if (buildMode === "export") return [];
    return [
      { source: "/api/auth/:path*", destination: `${backendUrl}/api/auth/:path*` },
      { source: "/api/chat/:path*", destination: `${backendUrl}/api/chat/:path*` },
      { source: "/api/documents/:path*", destination: `${backendUrl}/api/documents/:path*` },
      { source: "/api/sessions", destination: `${backendUrl}/api/sessions` },
      { source: "/api/datasets", destination: `${backendUrl}/api/datasets` },
      { source: "/api/datasets/:path*", destination: `${backendUrl}/api/datasets/:path*` },
      { source: "/api/judges", destination: `${backendUrl}/api/judges` },
      { source: "/api/judges/:path*", destination: `${backendUrl}/api/judges/:path*` },
      { source: "/api/compare/:path*", destination: `${backendUrl}/api/compare/:path*` },
      { source: "/api/inspect/:path*", destination: `${backendUrl}/api/inspect/:path*` },
      { source: "/inspect/:path*", destination: `${backendUrl}/inspect/:path*` },
    ];
  },
};

export default nextConfig;
