# The "web" container: builds the frontend, then serves the static
# output and reverse-proxies /api to the backend container -- one
# image covers both the UI and the reverse proxy the manifest's own
# packaging section (§5.3) calls for, since Caddy already does both
# jobs well (same pattern already used elsewhere on this team's
# infrastructure). Same-origin in production (VITE_API_BASE_URL is
# baked as the relative "/api/v1"), so no CORS is needed at all.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ENV VITE_API_BASE_URL=/api/v1
# Release version (repo-root VERSION, ADR 0024), passed by the bundle
# builder / compose as a build arg since this build context is frontend/.
ARG RESURGAM_VERSION=dev
ENV VITE_RESURGAM_VERSION=${RESURGAM_VERSION}
RUN npm run build

FROM caddy:2-alpine
ARG RESURGAM_VERSION=dev
LABEL org.opencontainers.image.version="${RESURGAM_VERSION}"
COPY --from=build /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile
