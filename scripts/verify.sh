#!/usr/bin/env bash
# Post-deployment verification: containers, n8n health, webhooks, .env status.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
set -a; [[ -f .env ]] && . ./.env; set +a
PORT="${N8N_PORT:-5678}"
ok()   { printf '✅'; }
bad()  { printf '❌'; }
warn() { printf '⚠️ '; }

echo "## 1. Containers"
printf '%-24s %-12s %-10s %s\n' CONTAINER STATE HEALTH PORTS
for c in social-hub-postgres social-hub-n8n social-hub-nocodb social-hub-portal social-hub-caddy; do
  if docker inspect "$c" >/dev/null 2>&1; then
    st=$(docker inspect -f '{{.State.Status}}' "$c")
    hl=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$c")
    pt=$(docker port "$c" 2>/dev/null | tr '\n' ' ')
    printf '%-24s %-12s %-10s %s\n' "$c" "$st" "$hl" "${pt:--}"
  else
    [[ "$c" == social-hub-caddy ]] && printf '%-24s %-12s\n' "$c" "(not enabled)" || printf '%-24s %-12s\n' "$c" "MISSING"
  fi
done

echo
echo "## 2. n8n HTTP checks (localhost:$PORT)"
check() { # name url expected-codes
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$2")
  if [[ " $3 " == *" $code "* ]]; then echo "$(ok) $1 -> HTTP $code"; else echo "$(bad) $1 -> HTTP $code (expected $3)"; fi
}
check "GET /healthz"            "http://127.0.0.1:$PORT/healthz" "200"
check "GET /healthz/readiness"  "http://127.0.0.1:$PORT/healthz/readiness" "200"
check "Editor UI /"             "http://127.0.0.1:$PORT/" "200"
check "Public API /api/v1 (needs X-N8N-API-KEY)" "http://127.0.0.1:$PORT/api/v1/workflows" "401 200"
check "NocoDB /api/v1/health"   "http://127.0.0.1:${NOCODB_PORT:-8080}/api/v1/health" "200"
check "Portal /healthz"         "http://127.0.0.1:${PORTAL_PORT:-3000}/healthz" "200"
check "Portal API needs login"  "http://127.0.0.1:${PORTAL_PORT:-3000}/api/summary" "401"

echo
echo "## 3. Workflows in n8n"
docker exec social-hub-n8n n8n list:workflow 2>/dev/null | grep -E '^sh' | sed 's/^/   /' || echo "   (could not list)"
echo "   Published/active:"
docker exec social-hub-postgres psql -U "$POSTGRES_USER" -d n8n -tAc \
  "select '   ' || id || '  ' || name || '  active=' || active from workflow_entity order by id" 2>/dev/null

echo
echo "## 4. Webhook endpoints"
echo "   n8n public base:   ${WEBHOOK_URL:-<unset>}"
[[ "${WEBHOOK_URL:-}" == https://* ]] || echo "   $(warn) WEBHOOK_URL is not https:// - Telegram will refuse to deliver updates"
hook_id=$(grep -o '"webhookId": "[^"]*"' n8n/workflows/02-telegram-approval-handler.json | head -1 | cut -d'"' -f4)
echo "   Expected Telegram: ${WEBHOOK_URL%/}/webhook/${hook_id}/webhook  (workflow 02)"
echo "   Workflows 01/03:   schedule-driven (08:00 daily / every 15 min), no public endpoint"
if [[ "${TELEGRAM_BOT_TOKEN:-REPLACE_ME}" != REPLACE_ME* ]]; then
  info=$(curl -s --max-time 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo")
  python3 - "$info" <<'PY'
import json, sys
try:
    r = json.loads(sys.argv[1]).get("result", {})
except Exception:
    print("   ❌ Telegram getWebhookInfo failed"); sys.exit()
url = r.get("url") or "(none registered - is workflow 02 active and WEBHOOK_URL public HTTPS?)"
print(f"   Telegram webhook:  {url}")
print(f"   pending updates:   {r.get('pending_update_count', 0)}")
if r.get("last_error_message"):
    print(f"   ⚠️  last error:     {r['last_error_message']}")
PY
else
  echo "   Telegram webhook:  $(warn) TELEGRAM_BOT_TOKEN is a placeholder, skipped"
fi

echo
echo "## 5. Environment (.env)"
printf '%-30s %s\n' VARIABLE STATUS
for v in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB N8N_ENCRYPTION_KEY N8N_PORT WEBHOOK_URL \
         TELEGRAM_BOT_TOKEN TELEGRAM_ADMIN_CHAT_ID ANTHROPIC_API_KEY \
         FACEBOOK_PAGE_ID FACEBOOK_PAGE_ACCESS_TOKEN \
         TWITTER_BEARER_TOKEN TWITTER_API_KEY TWITTER_API_SECRET TWITTER_ACCESS_TOKEN TWITTER_ACCESS_SECRET TWITTER_USER_ID; do
  val="${!v:-}"
  if [[ -z "$val" ]]; then s="$(bad) missing"
  elif [[ "$val" == REPLACE_ME* ]]; then s="$(warn) placeholder"
  elif [[ "$val" == *example.com* ]]; then s="$(warn) example domain"
  else s="$(ok) set"; fi
  printf '%-30s %s\n' "$v" "$s"
done
perm=$(stat -c '%a' .env 2>/dev/null)
[[ "$perm" == 600 ]] && echo "$(ok) .env permissions 600" || echo "$(warn) .env permissions are $perm (run: chmod 600 .env)"
