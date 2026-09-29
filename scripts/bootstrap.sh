#!/usr/bin/env bash
# One-shot deploy: start the stack, wait for health, load n8n credentials from
# .env, import the workflows and (optionally) publish/activate them.
#
#   scripts/bootstrap.sh                 # start + import (workflows stay inactive)
#   scripts/bootstrap.sh --https         # also start Caddy (Let's Encrypt)
#   scripts/bootstrap.sh --activate      # also publish all 4 workflows
#
# Safe to re-run: credentials and workflows are upserted by fixed ID.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

HTTPS=0; ACTIVATE=0
for a in "$@"; do
  case "$a" in
    --https) HTTPS=1 ;;
    --activate) ACTIVATE=1 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done

[[ -f .env ]] || { echo "No .env - run scripts/generate-env.sh first."; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required (apt install python3)."; exit 1; }

TZ_VALUE=$(grep -E '^TZ=' .env | tail -1 | cut -d= -f2-)
if [[ -n "$TZ_VALUE" && -d /usr/share/zoneinfo && ! -f "/usr/share/zoneinfo/$TZ_VALUE" ]]; then
  echo "TZ=$TZ_VALUE in .env is not a valid timezone (n8n workflows would fail)."
  echo "Use an IANA name such as Asia/Dhaka, Europe/London, UTC."
  exit 1
fi

# Upgrade older .env files in place: add secrets for components added later.
ensure_secret() { # name bytes
  if ! grep -qE "^$1=.+" .env; then
    sed -i "/^$1=/d" .env
    echo "$1=$(openssl rand -hex "$2")" >> .env
    echo "   added $1 to .env"
  fi
}
ensure_secret PORTAL_SESSION_SECRET 32
ensure_secret PORTAL_WEBHOOK_SECRET 32

envval() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
PORTAL_DOMAIN_V=$(envval PORTAL_DOMAIN)
if [[ -z "$PORTAL_DOMAIN_V" ]]; then
  echo "PORTAL_DOMAIN is missing from .env. The portal needs its own hostname, e.g.:"
  echo "  scripts/set-env.sh PORTAL_DOMAIN      # cms.example.com"
  echo "and give NocoDB a different one (NOCODB_DOMAIN / NOCODB_PUBLIC_URL, e.g. db.example.com)."
  exit 1
fi
for other in N8N_DOMAIN NOCODB_DOMAIN; do
  if [[ "$(envval $other)" == "$PORTAL_DOMAIN_V" ]]; then
    echo "PORTAL_DOMAIN and $other are both $PORTAL_DOMAIN_V - each service needs its own hostname."
    echo "Move NocoDB with: scripts/set-env.sh NOCODB_DOMAIN NOCODB_PUBLIC_URL   (e.g. db.example.com / https://db.example.com)"
    exit 1
  fi
done

COMPOSE=(docker compose)  # honours COMPOSE_FILE if set
[[ $HTTPS -eq 1 ]] && COMPOSE+=(--profile https)

if grep -qE '^[A-Z_]+=REPLACE_ME' .env; then
  echo "⚠  These .env values are still placeholders (the matching workflows will fail until set):"
  grep -oE '^[A-Z_]+=REPLACE_ME' .env | cut -d= -f1 | sed 's/^/     - /'
fi

echo "==> Starting containers"
"${COMPOSE[@]}" up -d --build

echo "==> Waiting for health checks"
for svc in postgres n8n nocodb portal; do
  for i in $(seq 1 60); do
    st=$(docker inspect -f '{{.State.Health.Status}}' "social-hub-$svc" 2>/dev/null || echo missing)
    [[ "$st" == "healthy" ]] && { echo "   $svc: healthy"; break; }
    [[ $i -eq 60 ]] && { echo "   $svc: $st (timed out)"; "${COMPOSE[@]}" logs --tail 50 "$svc"; exit 1; }
    sleep 3
  done
done

set -a; . ./.env; set +a

echo "==> Applying database migrations"
for f in db/migrations/*.sql; do
  [[ -e "$f" ]] || continue
  "${COMPOSE[@]}" exec -T postgres psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$f"
  echo "   $(basename "$f")"
done

echo "==> Importing n8n credentials (encrypted with N8N_ENCRYPTION_KEY)"
# Rendered to stdout and streamed into the container, so the plaintext never
# touches the host disk or the container's environment.
python3 "$ROOT/scripts/render_credentials.py" "$ROOT/.env" \
  | "${COMPOSE[@]}" exec -T n8n sh -c \
    'umask 077; cat > /tmp/sh-creds.json && n8n import:credentials --input=/tmp/sh-creds.json; rc=$?; rm -f /tmp/sh-creds.json; exit $rc'

# Importing unpublishes a workflow, so remember which ones were live and
# re-publish them; otherwise a re-run (e.g. to rotate a token) takes the bot offline.
WAS_ACTIVE=$("${COMPOSE[@]}" exec -T postgres psql -U "$POSTGRES_USER" -d n8n -tAc \
  "select id from workflow_entity where active and id like 'sh%'" 2>/dev/null | tr -d '\r' || true)
EXISTED=$("${COMPOSE[@]}" exec -T postgres psql -U "$POSTGRES_USER" -d n8n -tAc \
  "select id from workflow_entity where id like 'sh%'" 2>/dev/null | tr -d '\r' || true)

echo "==> Importing workflows"
"${COMPOSE[@]}" exec -T n8n n8n import:workflow --separate --input=/opt/social-hub/workflows

# 04 and 01 first: they are called as sub-workflows by 02.
ORDER=(shPublisher00004 shDailyAiGen0001 shTwListener0003 shTgApproval0002 shPortalActn0005)
TO_PUBLISH=()
for id in "${ORDER[@]}"; do
  # publish if asked, if it was live before, or if it's a workflow added by this update
  if [[ $ACTIVATE -eq 1 ]] || grep -qx "$id" <<<"$WAS_ACTIVE" || { [[ -n "$EXISTED" ]] && ! grep -qx "$id" <<<"$EXISTED"; }; then TO_PUBLISH+=("$id"); fi
done

if [[ ${#TO_PUBLISH[@]} -gt 0 ]]; then
  echo "==> Publishing workflows: ${TO_PUBLISH[*]}"
  for id in "${TO_PUBLISH[@]}"; do
    "${COMPOSE[@]}" exec -T n8n n8n publish:workflow --id="$id"
  done
  echo "==> Restarting n8n so the published workflows (and the Telegram webhook) go live"
  "${COMPOSE[@]}" restart n8n
  for i in $(seq 1 60); do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' social-hub-n8n)" == healthy ]] && break; sleep 3
  done
fi

echo
echo "Done. Run scripts/verify.sh for the health report."
