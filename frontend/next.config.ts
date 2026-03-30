import type { NextConfig } from "next";

// Backend URL: localhost:8000 for local dev, backend:8080 for k8s
const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone",  // Required for Docker deployment
  async rewrites() {
    return [
      // Auth routes (handled by backend)
      {
        source: "/api/auth/:path*",
        destination: `${backendUrl}/api/auth/:path*`,
      },
      // Chat API
      {
        source: "/api/chat/:path*",
        destination: `${backendUrl}/api/chat/:path*`,
      },
      // Document upload
      {
        source: "/api/documents/:path*",
        destination: `${backendUrl}/api/documents/:path*`,
      },
      // Sessions
      {
        source: "/api/sessions",
        destination: `${backendUrl}/api/sessions`,
      },
      // Viewer proxy
      {
        source: "/api/viewer/:path*",
        destination: `${backendUrl}/api/viewer/:path*`,
      },
      {
        source: "/viewer/:path*",
        destination: `${backendUrl}/viewer/:path*`,
      },
    ];
  },
};

export default nextConfig;
