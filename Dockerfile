# The website is built here and copied into the bot's image below: one image,
# one container, bot and site together.
FROM node:26-alpine@sha256:143494b1da2945f061539253adc65e4f1569ddf07da2d384c022c791a9d90a4a AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

# Set working directory
WORKDIR /app

# Python packages, at the exact versions in constraints.txt. pip, setuptools and
# wheel are pinned here too, so every build installs the same ones (bump them by
# hand). A compiler is only needed while installing, so it's removed in the same
# step and never ships.
COPY requirements.txt constraints.txt ./
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    pip install --no-cache-dir pip==26.2.1 setuptools==84.0.0 wheel==0.48.0 -c constraints.txt && \
    pip install --no-cache-dir -r requirements.txt -c constraints.txt && \
    apt-get purge -y gcc && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Copy application code
COPY . .

# The built website, served by the bot on WEB_PORT (7979 unless set; 0 turns it off)
COPY --from=web /web/dist /app/web/dist

# The settings template, copied into an empty config folder on first start
COPY config/.env.example /app/defaults/.env.example

# Create necessary directories
RUN mkdir -p /app/config /app/logs /app/cache

# The website on 7979, webhooks on 7980
EXPOSE 7979 7980

# Unraid shows this icon and opens this page from the Docker tab. The icon is the
# repository's copy, so it appears once the repository (or the image) is public.
LABEL org.opencontainers.image.title="Plexbie" \
      org.opencontainers.image.description="Looks after a household Plex server, from Discord and its own website" \
      org.opencontainers.image.source="https://github.com/NovaOra/plexbie" \
      net.unraid.docker.icon="https://raw.githubusercontent.com/NovaOra/plexbie/main/web/public/brand/plexbie-512.png" \
      net.unraid.docker.webui="http://[IP]:[PORT:7979]/"

# Runs as PUID:PGID (99:100 unless set), not root: see docker/entrypoint.sh.
ENV PUID=99 PGID=100
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "-u", "bot.py"]
