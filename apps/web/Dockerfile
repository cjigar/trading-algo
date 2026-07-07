# Next.js web app. Build from the repo root context: docker build -f apps/web/Dockerfile -t algo-web .
FROM node:20-slim AS build
WORKDIR /app/web
COPY apps/web/package.json ./
RUN npm install
COPY apps/web ./
# NEXT_PUBLIC_* is inlined at build time; point at the browser-reachable API URL.
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
RUN npm run build

FROM node:20-slim AS run
WORKDIR /app/web
ENV NODE_ENV=production
COPY --from=build /app/web ./
EXPOSE 3000
CMD ["npm", "start"]
