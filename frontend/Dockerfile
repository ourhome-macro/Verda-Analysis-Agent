FROM --platform=$BUILDPLATFORM node:22-alpine3.23 AS build

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM caddy:2.11.4-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /build/dist /srv

EXPOSE 80 443
