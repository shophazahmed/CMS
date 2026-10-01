#!/usr/bin/env bash
# Sets the portal's browser login (username + password). Only a scrypt hash is
# stored in .env; the password itself is never written anywhere.
#
#   scripts/set-portal-password.sh            # then: scripts/bootstrap.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "No .env - run scripts/generate-env.sh first."; exit 1; }

current=$(grep -E '^PORTAL_ADMIN_USER=' .env | tail -1 | cut -d= -f2- || true)
read -rp "Portal username [${current:-admin}]: " user
user=$(echo "${user:-${current:-admin}}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
[[ "$user" =~ ^[a-z0-9._-]{3,32}$ ]] || { echo "Username: 3-32 chars, letters/digits/._- only."; exit 1; }

read -rsp "New password (min 12 characters): " p1; echo
read -rsp "Repeat password: " p2; echo
[[ "$p1" == "$p2" ]] || { echo "Passwords don't match."; exit 1; }
(( ${#p1} >= 12 )) || { echo "Password must be at least 12 characters."; exit 1; }

PORTAL_PW="$p1" PORTAL_USER="$user" python3 - <<'PY'
import base64, hashlib, os, re
pw, user = os.environ["PORTAL_PW"], os.environ["PORTAL_USER"]
N, r, p, salt = 16384, 8, 1, os.urandom(16)
key = hashlib.scrypt(pw.encode(), salt=salt, n=N, r=r, p=p, dklen=64, maxmem=64 * 1024 * 1024)
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
h = f"scrypt:{N}:{r}:{p}:{b64(salt)}:{b64(key)}"   # same format the portal verifies
s = open(".env").read()
for name, val in (("PORTAL_ADMIN_USER", user), ("PORTAL_ADMIN_PASSWORD_HASH", h)):
    line = f"{name}={val}"
    s, n = re.subn(rf"(?m)^{name}=.*$", lambda m: line, s)
    if not n:
        s = s.rstrip("\n") + "\n" + line + "\n"
open(".env", "w").write(s)
PY
unset p1 p2
echo "Saved login for '$user'. Apply with: scripts/bootstrap.sh"
