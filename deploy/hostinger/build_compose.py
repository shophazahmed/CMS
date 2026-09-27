#!/usr/bin/env python3
"""Builds deploy/hostinger/docker-compose.yml: a single-file version of the stack.

Hostinger's Docker Manager (and its API) deploys a compose file on its own, without
the rest of the repo. So everything the main docker-compose.yml reads from disk
(DB init scripts, n8n workflows) is embedded as inline `configs`, and
the work scripts/bootstrap.sh does over `docker exec` is done by a one-shot
`n8n-setup` container that runs before n8n starts.

HTTPS: Hostinger VPSs with Docker Manager already run a shared Traefik
(project "traefik", host network, entrypoint `websecure`, cert resolver
`letsencrypt`) that owns ports 80/443 for every app on the server. So instead of
Caddy, n8n and NocoDB get Traefik labels, like Hostinger's own templates.

    python3 deploy/hostinger/build_compose.py
"""
import copy
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "deploy/hostinger/docker-compose.yml"

WORKFLOW_ORDER = ["shPublisher00004", "shDailyAiGen0001", "shTwListener0003", "shTgApproval0002"]

# Runs inside the n8n image. Builds the credentials file from the environment
# (same IDs as scripts/render_credentials.py) so secrets never touch the repo.
RENDER_CREDS_JS = r"""
const e = process.env;
const g = (k) => e[k] || '';
const creds = [
  { id: 'shCredPostgres01', name: 'Social Hub DB', type: 'postgres',
    data: { host: 'postgres', port: 5432, database: g('POSTGRES_DB'), user: g('POSTGRES_USER'),
            password: g('POSTGRES_PASSWORD'), ssl: 'disable', allowUnauthorizedCerts: false, maxConnections: 10 } },
  { id: 'shCredTelegram01', name: 'Telegram Bot', type: 'telegramApi',
    data: { accessToken: g('TELEGRAM_BOT_TOKEN'), baseUrl: g('TELEGRAM_API_BASE_URL') || 'https://api.telegram.org' } },
  { id: 'shCredAnthropic1', name: 'Anthropic API Key', type: 'httpHeaderAuth',
    data: { name: 'x-api-key', value: g('ANTHROPIC_API_KEY') } },
  { id: 'shCredFbPage0001', name: 'Facebook Page Token', type: 'httpQueryAuth',
    data: { name: 'access_token', value: g('FACEBOOK_PAGE_ACCESS_TOKEN') } },
  { id: 'shCredXBearer001', name: 'X Bearer Token', type: 'httpHeaderAuth',
    data: { name: 'Authorization', value: 'Bearer ' + g('TWITTER_BEARER_TOKEN') } },
  { id: 'shCredXOAuth1001', name: 'X OAuth1 User Context', type: 'oAuth1Api',
    data: { authUrl: 'https://api.x.com/oauth/authorize', accessTokenUrl: 'https://api.x.com/oauth/access_token',
            requestTokenUrl: 'https://api.x.com/oauth/request_token',
            consumerKey: g('TWITTER_API_KEY'), consumerSecret: g('TWITTER_API_SECRET'), signatureMethod: 'HMAC-SHA1',
            oauthTokenData: { oauth_token: g('TWITTER_ACCESS_TOKEN'), oauth_token_secret: g('TWITTER_ACCESS_SECRET') } } },
];
require('fs').writeFileSync('/tmp/sh-creds.json', JSON.stringify(creds), { mode: 0o600 });
"""

SETUP_SH = """#!/bin/sh
# One-shot: load credentials + workflows into n8n's database, then publish them.
# Runs on every deploy, so changing a token in the project environment and
# redeploying updates the stored credential.
set -e
node /opt/setup/render-creds.js
n8n import:credentials --input=/tmp/sh-creds.json
rm -f /tmp/sh-creds.json
n8n import:workflow --separate --input=/opt/setup/workflows
for id in %s; do n8n publish:workflow --id="$id"; done
echo "social-hub setup complete"
""" % " ".join(WORKFLOW_ORDER)


class Literal(str):
    pass


yaml.add_representer(Literal, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|"))


def content(text):
    # `$` must be doubled or compose would try to interpolate $json / ${...}.
    return {"content": Literal(text.replace("$", "$$"))}


def main():
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    svc = base["services"]
    configs = {
        "pg_init_dbs": content((ROOT / "db/init/01-create-databases.sh").read_text()),
        "pg_schema": content((ROOT / "db/init/02-schema.sql").read_text()),
        "setup_sh": content(SETUP_SH),
        "render_creds_js": content(RENDER_CREDS_JS.strip() + "\n"),
    }
    wf_mounts = []
    for f in sorted((ROOT / "n8n/workflows").glob("*.json")):
        key = "wf_" + f.stem.replace("-", "_")
        configs[key] = content(f.read_text())
        wf_mounts.append({"source": key, "target": f"/opt/setup/workflows/{f.name}"})

    # Postgres: init scripts from configs instead of the ./db/init bind mount.
    svc["postgres"]["volumes"] = ["postgres_data:/var/lib/postgresql/data"]
    svc["postgres"]["configs"] = [
        {"source": "pg_init_dbs", "target": "/docker-entrypoint-initdb.d/01-create-databases.sh", "mode": 0o755},
        {"source": "pg_schema", "target": "/docker-entrypoint-initdb.d/02-schema.sql"},
    ]

    n8n = svc["n8n"]
    n8n["volumes"] = ["n8n_data:/home/node/.n8n"]

    # Setup container: same image/DB settings as n8n, plus the secrets it needs
    # to build credentials. The long-running n8n never sees the secrets.
    setup = {
        "image": n8n["image"],
        "container_name": "social-hub-n8n-setup",
        "restart": "no",
        "user": "node",
        "entrypoint": ["/bin/sh", "/opt/setup/setup.sh"],
        "depends_on": {"postgres": {"condition": "service_healthy"}},
        "environment": {
            **{k: v for k, v in n8n["environment"].items() if k.startswith(("DB_", "N8N_ENCRYPTION_KEY", "GENERIC_TIMEZONE", "TZ"))},
            "N8N_RUNNERS_BROKER_PORT": 5690,
            "POSTGRES_USER": "${POSTGRES_USER}",
            "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
            "POSTGRES_DB": "${POSTGRES_DB}",
            "TELEGRAM_BOT_TOKEN": "${TELEGRAM_BOT_TOKEN}",
            "TELEGRAM_API_BASE_URL": "${TELEGRAM_API_BASE_URL:-}",
            "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}",
            "FACEBOOK_PAGE_ACCESS_TOKEN": "${FACEBOOK_PAGE_ACCESS_TOKEN}",
            "TWITTER_BEARER_TOKEN": "${TWITTER_BEARER_TOKEN}",
            "TWITTER_API_KEY": "${TWITTER_API_KEY}",
            "TWITTER_API_SECRET": "${TWITTER_API_SECRET}",
            "TWITTER_ACCESS_TOKEN": "${TWITTER_ACCESS_TOKEN}",
            "TWITTER_ACCESS_SECRET": "${TWITTER_ACCESS_SECRET}",
        },
        "volumes": ["n8n_data:/home/node/.n8n"],
        "configs": [
            {"source": "setup_sh", "target": "/opt/setup/setup.sh"},
            {"source": "render_creds_js", "target": "/opt/setup/render-creds.js"},
            *wf_mounts,
        ],
        "networks": ["backend"],
    }
    n8n["depends_on"] = {
        "postgres": {"condition": "service_healthy"},
        "n8n-setup": {"condition": "service_completed_successfully"},
    }

    # Route through the server's shared Traefik. Traefik runs with host
    # networking, so it reaches the containers on their bridge IP; pin which
    # network to use because both services sit on two networks.
    def traefik_labels(router, domain_var, port):
        return {
            "traefik.enable": "true",
            "traefik.docker.network": "social-hub_frontend",
            f"traefik.http.routers.{router}.rule": f"Host(`${{{domain_var}}}`)",
            f"traefik.http.routers.{router}.entrypoints": "websecure",
            f"traefik.http.routers.{router}.tls.certresolver": "letsencrypt",
            f"traefik.http.services.{router}.loadbalancer.server.port": str(port),
        }
    n8n["labels"] = traefik_labels("social-hub-n8n", "N8N_DOMAIN", 5678)
    svc["nocodb"]["labels"] = traefik_labels("social-hub-cms", "NOCODB_DOMAIN", 8080)

    services = {}
    for name in ["postgres", "n8n-setup", "n8n", "nocodb"]:
        services[name] = setup if name == "n8n-setup" else svc[name]
    out = {"name": base["name"], "services": services, "networks": base["networks"],
           "volumes": {k: v for k, v in base["volumes"].items() if not k.startswith("caddy")},
           "configs": configs}
    for k in list(out["services"]):
        out["services"][k] = copy.deepcopy(out["services"][k])
        out["services"][k].pop("<<", None)
        out["services"][k].setdefault("restart", "unless-stopped")

    header = (
        "# GENERATED by deploy/hostinger/build_compose.py from docker-compose.yml - do not edit.\n"
        "# Self-contained stack for Hostinger Docker Manager: DB init scripts and\n"
        "# n8n workflows are embedded as configs; n8n-setup imports credentials + workflows.\n"
        "# HTTPS comes from the server's shared Traefik (labels on n8n and nocodb).\n"
        "# Set the variables from .env in the project's environment.\n"
    )
    OUT.write_text(header + yaml.dump(out, sort_keys=False, width=1000, allow_unicode=True))
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
