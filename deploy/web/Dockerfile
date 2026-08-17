# The web tier: the built SPA plus the nginx that fronts the API, so the
# browser talks same-origin exactly as it does behind the Vite dev proxy.
# frontend/dist is not committed, so it is built here.
#
# Build context is the repo root (it needs both frontend/ and deploy/web/).
FROM node:22-slim AS build
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# `tsc --noEmit && vite build` — the typecheck fails on openapi.json drift,
# which is deliberate: a stale committed spec must not ship.
RUN npm run build
# Precompress for nginx's gzip_static: the maplibre chunk alone drops ~4x.
RUN find dist -type f \( -name '*.js' -o -name '*.mjs' -o -name '*.css' \
    -o -name '*.html' -o -name '*.svg' \) -exec gzip -k -9 {} +

FROM nginx:stable
COPY deploy/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html

EXPOSE 80

# Static only: hitting /health here would report the API's health, not this
# container's, and would flap the web tier whenever the API restarts.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1/ -o /dev/null || exit 1
