FROM node:20-alpine
RUN apk add --no-cache android-tools tzdata
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY server.js ./
COPY lib ./lib
COPY public ./public
# ADB-ключи, список ТВ и сертификат хранятся в /app/data (volume)
ENV HOME=/app/data DATA_DIR=/app/data PORT=8080 HTTPS_PORT=8443
VOLUME ["/app/data"]
EXPOSE 8080 8443
CMD ["node", "server.js"]
