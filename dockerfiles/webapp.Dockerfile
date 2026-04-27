# Two-stage build: a `dev` target for `vite dev` (hot reload) and a
# `prod` target that builds static assets and serves them via nginx.
# docker-compose picks the target with `target:` per environment.

# ────────────────────────────────────────────────
# Common base — install deps once
# ────────────────────────────────────────────────
FROM node:22-alpine AS base
WORKDIR /app
COPY webapp/package.json webapp/package-lock.json* ./
# `npm install` rather than `npm ci` because lockfile is generated on
# first install — once present, swap to `npm ci` for reproducibility.
RUN npm install


# ────────────────────────────────────────────────
# dev — vite dev server with HMR
# ────────────────────────────────────────────────
FROM base AS dev
COPY webapp/ ./
EXPOSE 5173
# `--host 0.0.0.0` is required so the dev server is reachable from outside
# the container (default binds to 127.0.0.1).
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]


# ────────────────────────────────────────────────
# build — produce dist/ for the prod stage
# ────────────────────────────────────────────────
FROM base AS build
COPY webapp/ ./
RUN npm run build


# ────────────────────────────────────────────────
# prod — nginx serves /dist and reverse-proxies /api/* to the API
# ────────────────────────────────────────────────
FROM nginx:1.27-alpine AS prod
COPY --from=build /app/dist /usr/share/nginx/html
COPY dockerfiles/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
