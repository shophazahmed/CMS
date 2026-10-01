# Test-only build of the portal: identical to portal/Dockerfile, plus the CA of
# the sandbox's HTTPS-inspecting proxy so npm can verify registry.npmjs.org.
FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY tests/.work/ca.crt /tmp/ca.crt
COPY portal/package.json portal/package-lock.json ./
RUN NODE_EXTRA_CA_CERTS=/tmp/ca.crt npm install --omit=dev --no-audit --no-fund && npm cache clean --force
COPY portal/server.js portal/auth.js ./
COPY portal/public ./public
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD wget -qO- http://127.0.0.1:3000/healthz >/dev/null 2>&1 || exit 1
CMD ["node", "server.js"]
