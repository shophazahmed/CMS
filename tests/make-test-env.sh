#!/usr/bin/env bash
# Writes a .env wired to the offline mocks (fake tokens the mocks accept).
# Test machines only: this overwrites .env.
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/generate-env.sh --force .env >/dev/null
python3 - <<'PY'
import pathlib, re
p = pathlib.Path(".env"); s = p.read_text()
vals = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN", "TELEGRAM_ADMIN_CHAT_ID": "-100500",
    "ANTHROPIC_API_KEY": "test-anthropic-key", "FACEBOOK_PAGE_ID": "pg42",
    "FACEBOOK_PAGE_ACCESS_TOKEN": "test-fb-token", "TWITTER_BEARER_TOKEN": "test-bearer",
    "TWITTER_API_KEY": "test-consumer-key", "TWITTER_API_SECRET": "test-consumer-secret",
    "TWITTER_ACCESS_TOKEN": "42-test-token", "TWITTER_ACCESS_SECRET": "test-token-secret",
    "TWITTER_USER_ID": "42", "WEBHOOK_URL": "http://localhost:5678/", "N8N_SECURE_COOKIE": "false",
    "N8N_PROTOCOL": "http", "N8N_DOMAIN": "localhost",
}
for k, v in vals.items():
    s = re.sub(rf"^{k}=.*$", f"{k}={v}", s, flags=re.M)
s += ("\n# --- test-only mock endpoints ---\n" + "".join(
    f"{k}=http://mock:9000\n" for k in
    ["CLAUDE_API_BASE_URL", "FB_GRAPH_BASE_URL", "TWITTER_API_BASE_URL", "TELEGRAM_API_BASE_URL"]))
p.write_text(s)
PY
mkdir -p tests/.work && chmod 777 tests/.work
echo "Test .env written. Now:"
echo "  export COMPOSE_FILE=docker-compose.yml:tests/docker-compose.test.yml"
echo "  scripts/bootstrap.sh --activate && python3 tests/run_e2e.py"
