#!/usr/bin/env bash
# Turns a Meta System User token into the Page ID + Page access token and saves
# both into .env. The Page token inherits "never expires" from the System User token.
#
#   scripts/setup-facebook.sh            # prompts for the token (input hidden)
#   PAGE=2 scripts/setup-facebook.sh     # pick a page when the token sees several
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "No .env - run scripts/generate-env.sh first."; exit 1; }

GRAPH="${FB_GRAPH_BASE_URL:-https://graph.facebook.com}"
VER=$(grep -E '^FB_GRAPH_VERSION=' .env | cut -d= -f2-); VER=${VER:-v19.0}

read -rsp "Meta System User token: " SUT; echo
umask 077
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
curl -sS --max-time 20 -G "$GRAPH/$VER/me/accounts" \
  --data-urlencode "fields=id,name,access_token,tasks" \
  --data-urlencode "access_token=$SUT" > "$TMP"
unset SUT

PAGE="${PAGE:-}" python3 - "$TMP" <<'PY'
import json, os, re, sys
d = json.load(open(sys.argv[1]))
if "data" not in d:
    err = d.get("error", d)
    sys.exit(f"Meta returned an error: {err.get('message', err)}\n"
             "Check the token was generated for your app with pages_manage_posts, "
             "pages_read_engagement and pages_show_list, and that the System User "
             "has the Page assigned.")
pages = d["data"]
if not pages:
    sys.exit("The token can't see any Page. In Business Settings, assign your Page to the "
             "System User (Full control), then generate a new token.")
for i, p in enumerate(pages, 1):
    print(f"  {i}. {p['name']}  (id {p['id']})")
choice = os.environ.get("PAGE")
if len(pages) > 1 and not choice:
    sys.exit("Several Pages found - re-run with PAGE=<number> scripts/setup-facebook.sh")
p = pages[int(choice) - 1 if choice else 0]
if "CREATE_CONTENT" not in (p.get("tasks") or ["CREATE_CONTENT"]):
    print("  warning: the System User can't create content on this Page; give it Full control.")
env = open(".env").read()
env = re.sub(r"(?m)^FACEBOOK_PAGE_ID=.*$", lambda m: "FACEBOOK_PAGE_ID=" + p["id"], env)
env = re.sub(r"(?m)^FACEBOOK_PAGE_ACCESS_TOKEN=.*$",
             lambda m: "FACEBOOK_PAGE_ACCESS_TOKEN=" + p["access_token"], env)
open(".env", "w").write(env)
print(f"Saved Page '{p['name']}' (id {p['id']}) and its access token to .env.")
print("Now run: scripts/bootstrap.sh")
PY
