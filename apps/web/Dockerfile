# Next.js web app. Build from the repo root context: docker build -f apps/web/Dockerfile -t algo-web .
# Preserves the apps/web path so tsconfig's `extends: ../../tsconfig.base.json` resolves.
FROM node:20-slim AS build
WORKDIR /app
COPY tsconfig.base.json ./
COPY apps/web ./apps/web
WORKDIR /app/apps/web
RUN npm install
# NEXT_PUBLIC_* is inlined at build time. Default to EMPTY so lib/api.ts resolves the base at
# call time from window.location (same-origin behind the proxy, :8000 in local dev). Baking a
# non-empty default here wins over that logic and pins every browser to localhost:8000 -- which
# is the *user's own machine*, so login silently fails and no request ever reaches the server.
# Only set this to pin an explicit cross-origin API host.
ARG NEXT_PUBLIC_API_BASE=
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
RUN npm run build

FROM node:20-slim AS run
WORKDIR /app/apps/web
ENV NODE_ENV=production
COPY --from=build /app/apps/web ./
EXPOSE 3000
CMD ["npm", "start"]
