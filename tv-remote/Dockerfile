FROM node:20-alpine
RUN apk add --no-cache android-tools tzdata tar
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY server.js agent.js ./
COPY lib ./lib
COPY public ./public
# ADB-ключи, список ТВ и сертификат хранятся в /app/data (volume)
ENV HOME=/app/data DATA_DIR=/app/data PORT=8080 HTTPS_PORT=8443
VOLUME ["/app/data"]
EXPOSE 8080 8443
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:${PORT}/healthz >/dev/null || exit 1
CMD ["node", "server.js"]
