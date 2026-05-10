# Frontend Dockerfile - Next.js
FROM public.ecr.aws/docker/library/node:22-alpine AS base

# Dependencies stage
FROM base AS deps
WORKDIR /app

# Copy package files
COPY frontend/package*.json ./

# Install dependencies
RUN npm ci

# Builder stage
FROM base AS builder
WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ .

# Build the Next.js app
# NEXT_PUBLIC_SHOW_CHAT is baked in at build time; the EKS platform has chat,
# so enable it. The standalone MCP viewer (eval_mcp/viewer_static) is built
# separately via `npm run build:viewer` without this flag, so chat stays hidden.
ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_PUBLIC_SHOW_CHAT=true
RUN npm run build

# Runner stage
FROM base AS runner
RUN apk add --no-cache tini
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Create non-root user (for k8s to switch to)
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Copy built app (public/ omitted - empty in this project)
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "server.js"]
