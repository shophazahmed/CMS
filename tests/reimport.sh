#!/usr/bin/env bash
# Re-import + publish workflows and restart n8n (test helper).
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T n8n n8n import:workflow --separate --input=/opt/social-hub/workflows >/dev/null 2>&1
for id in shPublisher00004 shDailyAiGen0001 shTwListener0003 shTgApproval0002; do
  docker compose exec -T n8n n8n publish:workflow --id="$id" >/dev/null 2>&1
done
docker compose up -d --force-recreate n8n >/dev/null 2>&1
for i in $(seq 60); do [ "$(docker inspect -f '{{.State.Health.Status}}' social-hub-n8n)" = healthy ] && break; sleep 2; done
sleep 3
