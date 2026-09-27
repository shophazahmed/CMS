# Social Automation Hub

Human-in-the-loop social media automation: **n8n** orchestrates, **Telegram** is
the control panel, **Claude** writes and screens content, **PostgreSQL + NocoDB**
is the CMS, and approved posts go to a **Facebook Page** (Graph API) and **X**
(API v2). Nothing is published without a button press in Telegram.

```
 08:00 cron ──► 01 AI Generator ──► Claude (copy) ──► CMS draft ──► Telegram preview + buttons
 Team text/photo ─► 02 Telegram handler ─► Claude (adapt) ─► CMS draft ─┘        │
 every 15 min ──► 03 Mention listener ──► Claude (classify) ──► Telegram [Retweet][Ignore]
                                                                                 │
 Button press ──► 02 Telegram handler ──► Approve ──► 04 Publisher ──► FB /feed|/photos + X /2/tweets
                                      ├─► Regenerate / Edit / Reject      (status + message updated)
                                      └─► Retweet ──► X /2/users/:id/retweets
```

| Workflow | File | Trigger | What it does |
|---|---|---|---|
| 01 Daily AI Content Generator | `n8n/workflows/01-daily-ai-content-generator.json` | Cron `0 8 * * *` (in `TZ`) | Loads the top active campaign, asks Claude for an X post + FB post (JSON schema output), saves `PENDING_APPROVAL`, sends the Telegram preview with 6 buttons. Also called by 02 to regenerate or re-preview. |
| 02 Telegram Approval & Team Input | `n8n/workflows/02-telegram-approval-handler.json` | Telegram webhook | The single entry point for every button and message: Approve All / FB only / X only, Regenerate, Edit (ForceReply), Reject, Retweet, Ignore, and team-submitted text/photos. Only the admin chat can act. |
| 03 Twitter Mention Listener | `n8n/workflows/03-twitter-mention-listener.json` | Every 15 min | Polls `/2/users/:id/mentions` (since_id cursor), de-dupes, Claude classifies (score, spam, sentiment, safety), high-value ones go to Telegram with a Retweet button. |
| 04 Multi-Channel Publisher | `n8n/workflows/04-publisher.json` | Called by 02 | Publishes one approved post: text → `/feed`, photo → `/photos` (multipart); X media upload + `/2/tweets`. Records IDs or the exact API error. |

Workflows are generated from `n8n/src/build_workflows.py` (readable source,
fixed IDs). Edit that and run `python3 n8n/src/build_workflows.py`, or edit in the
n8n UI and export.

## Recommendations before you build (read this first)

1. **One n8n per server.** Your Hostinger `n8n-with-ai-assistant` template
   already runs an n8n container, usually with Traefik holding ports 80/443 and
   SQLite storage. Running this stack next to it gives two n8n instances and a
   port fight. Recommended: export anything you built there (Workflows →
   Download), stop that project in Hostinger's Docker Manager, and run this stack
   with `--https` (Caddy). If you'd rather keep Traefik, skip `--https` and point
   Traefik at `social-hub-n8n:5678` and `social-hub-nocodb:8080` instead.
   Check what's on the box first: `docker ps` and `ss -tlnp | grep -E ':(80|443|5678|8080)\b'`.
2. **Telegram needs public HTTPS.** Point a DNS A record (e.g. `n8n.yourdomain.com`,
   `cms.yourdomain.com`) at the VPS before deploying. No domain means no buttons.
3. **X (Twitter) API Basic tier (~$100/mo) is required** to post, upload media and
   read mentions. The access token must be generated **after** setting the app to
   *Read and Write*, or posting returns 403.
4. **Facebook: use a System User token** (Business Settings → System Users) with
   `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`. It doesn't
   expire like user tokens do. Graph `v19.0` (per spec) is past Meta's
   ~2-year support window; Meta auto-upgrades calls, but set `FB_GRAPH_VERSION` to
   a current version once you've tested.
5. **Back up `N8N_ENCRYPTION_KEY`.** Lose it and every stored credential is
   unrecoverable. Back up `.env` + a nightly `pg_dumpall` off the server.
6. **Postgres 16 works but n8n 2.x logs that it only gets "compatibility support".**
   On a fresh install you can set `POSTGRES_VERSION=17` in `.env` before first
   boot (upgrading later needs dump/restore).
7. n8n and NocoDB bind to `127.0.0.1` only; the public entry is Caddy on 443.
   Postgres has no host port at all. Keep it that way: firewall everything except
   22/80/443 (`ufw allow 22,80,443/tcp`).

## Deploy on the VPS

```bash
# 1. Get the code
sudo mkdir -p /opt/social-automation-hub && sudo chown $USER /opt/social-automation-hub
git clone <this repo> /opt/social-automation-hub && cd /opt/social-automation-hub

# 2. Task 1: environment file with generated secrets + placeholders
scripts/generate-env.sh            # writes .env (chmod 600)
nano .env                          # fill every REPLACE_ME_*, check domains and TZ

# 3. Tasks 2-3: start containers, wait for health, load credentials + workflows
scripts/bootstrap.sh --https --activate

# 4. Task 5: verification report
scripts/verify.sh
```

`bootstrap.sh` is safe to re-run (after changing tokens in `.env`, re-run it to
update the n8n credentials). Without `--activate` the workflows are imported but
left unpublished so you can review them in the UI first.

## Deploy with Hostinger Docker Manager (no SSH)

`deploy/hostinger/docker-compose.yml` is a single-file version of the stack for
hPanel's Docker Manager (or the Hostinger API), which deploys a compose file
without the rest of the repo. The DB scripts, Caddyfile and workflows are
embedded in it, and a one-shot `n8n-setup` container imports the credentials
and workflows, then publishes them before n8n starts.

1. Create DNS A records `n8n` and `cms` pointing to the VPS IP.
2. Free ports 80/443: stop the template n8n project if it runs Traefik.
3. Deploy the compose file and set the project environment to the contents of
   a `.env` made with `scripts/generate-env.sh` (all variables from the
   Configuration reference).
4. Put real tokens in the project environment and redeploy. `n8n-setup` re-runs,
   updates the stored credentials, and n8n re-registers the Telegram webhook.

Regenerate the file after changing workflows, the schema or docker-compose.yml:
`python3 deploy/hostinger/build_compose.py`.

### First-time setup checklist

- **n8n owner account:** open `https://n8n.yourdomain.com` and create the owner user.
- **Telegram:** create the bot with @BotFather, add it to your approval group, and
  either make it an admin or run `/setprivacy` → *Disable* (otherwise it can't see
  photos/text sent to the group). Send `/id` in the group; the bot replies with
  the chat ID → put it in `TELEGRAM_ADMIN_CHAT_ID`, re-run `bootstrap.sh`.
  Optionally restrict buttons to specific people with `TELEGRAM_ALLOWED_USER_IDS`.
- **NocoDB CMS:** open `https://cms.yourdomain.com`, create the admin user, then
  *Create Base → Connect External Data Source → PostgreSQL*: host `postgres`,
  port `5432`, database `social_hub`, user/password from `.env`. You now edit
  `campaigns`, `media_assets`, `content_queue`, `mentions` in a spreadsheet UI.
- **Campaigns:** edit the seeded "Default Brand Campaign" (tone, topics, CTA).
  The highest-`priority` active campaign drives the 08:00 draft. Add image URLs
  to `media_assets` to have one attached automatically.
- **X user ID:** `TWITTER_USER_ID` is the number before the `-` in your access token.

### Using it from Telegram

| You do | Result |
|---|---|
| Wait for 08:00 | AI draft with ✅ Approve All · 📘 FB Only · 🐦 X Only · ♻️ Regenerate · ✏️ Edit · ❌ Reject |
| Tap ✏️ Edit, reply to the prompt | Plain text replaces both; or `TW: …` / `FB: …` lines to set each |
| Send text and/or a photo | Claude adapts it into FB + X copy and sends it for approval |
| Start a message with `!raw` | Your text is used exactly as written |
| Tap 🔁 Retweet Now on a mention | Retweeted from the brand account |
| `/id` or `/help` | Shows the chat ID and usage |

Every decision edits the original message (✅ Published with links, ❌ Rejected,
⚠️ failure reason) and removes its buttons, so nothing can be approved twice.

## Configuration reference (`.env`)

Required (Task 1): `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
`N8N_ENCRYPTION_KEY`, `N8N_PORT`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID`,
`ANTHROPIC_API_KEY`, `FACEBOOK_PAGE_ID`, `FACEBOOK_PAGE_ACCESS_TOKEN`,
`TWITTER_BEARER_TOKEN`, `TWITTER_API_KEY`, `TWITTER_API_SECRET`,
`TWITTER_ACCESS_TOKEN`, `TWITTER_ACCESS_SECRET`.

Also needed: `N8N_DOMAIN`, `WEBHOOK_URL`, `NOCODB_DOMAIN`,
`TWITTER_USER_ID`, `TZ`. Tunables: `ANTHROPIC_MODEL_COPY` (default
`claude-sonnet-5`), `ANTHROPIC_MODEL_CLASSIFIER` (default `claude-haiku-4-5`),
`MENTION_MIN_SCORE` (default 7), `FB_GRAPH_VERSION`, `N8N_VERSION`,
`POSTGRES_VERSION`.

Secrets never enter the n8n container environment: `bootstrap.sh` streams them
into n8n's encrypted credential store. Workflows only read non-secret IDs via `$env`.

## Testing (offline, no real API keys)

`tests/` runs the whole stack against a mock of Telegram, Claude, Facebook and X
(including OAuth 1.0a signature verification) and drives workflow 02 through its
real signed webhook:

```bash
tests/make-test-env.sh                 # OVERWRITES .env with fake tokens + mock URLs
export COMPOSE_FILE=docker-compose.yml:tests/docker-compose.test.yml
scripts/bootstrap.sh --activate
python3 tests/run_e2e.py               # 46 checks across 13 scenarios
```

Never run the test override on the production server.

## Operations

```bash
docker compose logs -f n8n                                  # live logs
docker compose pull && docker compose up -d                 # upgrade (pin N8N_VERSION in prod)
docker exec social-hub-postgres pg_dumpall -U socialhub | gzip > backup-$(date +%F).sql.gz
```

Known limits of this MVP: one Facebook Page and one X account (the
`social_accounts` table is ready for multi-account); `scheduled_at` is stored
but posts publish at approval time; photos from Telegram are fetched at publish
time rather than copied to S3/Supabase (add a storage step if you need
permanent public URLs); X `/2/media/upload` was only verified against the mock,
so check the first real photo tweet.
