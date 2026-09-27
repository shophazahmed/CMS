#!/usr/bin/env bash
# Creates .env with strong random secrets and placeholder API tokens.
# Usage: scripts/generate-env.sh [--force] [path/to/.env]
set -euo pipefail

FORCE=0
if [[ "${1:-}" == "--force" ]]; then FORCE=1; shift; fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.env}"

if [[ -f "$ENV_FILE" && $FORCE -eq 0 ]]; then
  echo "Refusing to overwrite $ENV_FILE (it holds your live secrets)."
  echo "Re-run with --force only if you really want new ones. Changing"
  echo "N8N_ENCRYPTION_KEY or POSTGRES_PASSWORD on an existing install breaks it."
  exit 1
fi

# Hex only: safe inside URLs (NocoDB's NC_DB) and shell without quoting.
rand() { openssl rand -hex "$1"; }

umask 077
cat > "$ENV_FILE" <<EOF
# ---------------------------------------------------------------------------
# Social Automation Hub - environment (generated $(date -u +%Y-%m-%dT%H:%M:%SZ))
# chmod 600. Never commit this file.
# ---------------------------------------------------------------------------

# ---- Compose files ---------------------------------------------------------
# docker-compose.traefik.yml adds labels for a shared Traefik on 80/443 (Hostinger
# Docker Manager servers have one). Harmless without Traefik; on a bare server
# run scripts/bootstrap.sh --https to use the bundled Caddy instead.
COMPOSE_FILE=docker-compose.yml:docker-compose.traefik.yml

# ---- PostgreSQL ------------------------------------------------------------
POSTGRES_USER=socialhub
POSTGRES_PASSWORD=$(rand 24)
POSTGRES_DB=social_hub

# ---- n8n -------------------------------------------------------------------
# BACK THIS KEY UP. Without it, every stored n8n credential is unreadable.
N8N_ENCRYPTION_KEY=$(rand 32)
N8N_PORT=5678
N8N_BIND_IP=127.0.0.1
N8N_VERSION=latest
# Public HTTPS hostname. Telegram webhooks will NOT work without it.
N8N_DOMAIN=n8n.seenu.online
N8N_PROTOCOL=https
WEBHOOK_URL=https://n8n.seenu.online/
N8N_SECURE_COOKIE=true
# Your local timezone (IANA name) - the 08:00 cron uses it, e.g. Asia/Dhaka, Europe/London
TZ=UTC

# ---- NocoDB (CMS UI) -------------------------------------------------------
NOCODB_PORT=8080
NOCODB_BIND_IP=127.0.0.1
NOCODB_DOMAIN=cms.seenu.online
NOCODB_PUBLIC_URL=https://cms.seenu.online
NOCODB_JWT_SECRET=$(rand 32)

# ---- Telegram --------------------------------------------------------------
# From @BotFather. Send /id to the bot in your approval group to get the chat ID.
TELEGRAM_BOT_TOKEN=REPLACE_ME_123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_ADMIN_CHAT_ID=REPLACE_ME_-1001234567890
# Optional comma-separated Telegram user IDs allowed to press buttons (empty = anyone in the admin chat)
TELEGRAM_ALLOWED_USER_IDS=

# ---- Anthropic (Claude) ----------------------------------------------------
ANTHROPIC_API_KEY=REPLACE_ME_sk-ant-xxxxxxxxxxxxxxxx
ANTHROPIC_MODEL_COPY=claude-sonnet-5
ANTHROPIC_MODEL_CLASSIFIER=claude-haiku-4-5

# ---- Facebook (Meta Graph API) ---------------------------------------------
FACEBOOK_PAGE_ID=REPLACE_ME_123456789012345
# Long-lived Page token (System User token recommended): pages_manage_posts,
# pages_read_engagement, pages_show_list
FACEBOOK_PAGE_ACCESS_TOKEN=REPLACE_ME_EAAGxxxxxxxxxxxxxxxx
FB_GRAPH_VERSION=v19.0

# ---- Twitter / X (API v2, Basic tier or higher) ----------------------------
TWITTER_BEARER_TOKEN=REPLACE_ME_AAAAAAAAAAAAAAAAAAAAAxxxxxxxx
TWITTER_API_KEY=REPLACE_ME_consumer_key
TWITTER_API_SECRET=REPLACE_ME_consumer_secret
# Access token/secret must be generated with Read+Write permission.
TWITTER_ACCESS_TOKEN=REPLACE_ME_123456-xxxxxxxx
TWITTER_ACCESS_SECRET=REPLACE_ME_xxxxxxxx
# Numeric user ID of your brand account (the first part of TWITTER_ACCESS_TOKEN)
TWITTER_USER_ID=REPLACE_ME_1234567890
TWITTER_USERNAME=YourBrand
# Mentions scoring >= this (0-10) are sent to Telegram
MENTION_MIN_SCORE=7
EOF
chmod 600 "$ENV_FILE"
echo "Wrote $ENV_FILE (mode 600). Now replace every REPLACE_ME_ value and the example.com domains."
