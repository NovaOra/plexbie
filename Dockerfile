# The website is built here and copied into the bot's image below: one image,
# one container, bot and site together.
FROM node:26-alpine@sha256:0b36e8c136b94cd4fcf02188228e76c31ad5872eef3fec8cbd2eee500cfd9e80 AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.11-slim@sha256:bab1b7ef4b450c81002278d035eff85ebe394ae94df904f7a3ba14f7e16e487b

# Set working directory
WORKDIR /app

# Python packages, at the exact versions in constraints.txt. A compiler is only
# needed while installing, so it's removed in the same step and never ships.
COPY requirements.txt constraints.txt ./
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    pip install --no-cache-dir --upgrade pip setuptools wheel && \
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
