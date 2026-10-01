#!/usr/bin/env bash
# Sets .env values without an editor. Input is hidden and never lands in shell
# history, so it's safe for tokens pasted into a browser-based console.
#
#   scripts/set-env.sh TWITTER_API_KEY TWITTER_API_SECRET ...
#
# Setting TWITTER_ACCESS_TOKEN also fills TWITTER_USER_ID (the number before "-").
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "No .env - run scripts/generate-env.sh first."; exit 1; }
[[ $# -gt 0 ]] || { echo "usage: scripts/set-env.sh VAR [VAR...]"; exit 2; }

save() { # name value
  NAME="$1" VALUE="$2" python3 - <<'PY'
import os, re
n, v = os.environ["NAME"], os.environ["VALUE"]
s = open(".env").read()
line = f"{n}={v}"
s, count = re.subn(rf"(?m)^{re.escape(n)}=.*$", lambda m: line, s)
if not count:
    s = s.rstrip("\n") + "\n" + line + "\n"
open(".env", "w").write(s)
PY
}

for name in "$@"; do
  [[ "$name" =~ ^[A-Z_][A-Z0-9_]*$ ]] || { echo "skip: '$name' is not a variable name"; continue; }
  read -rsp "$name: " value; echo
  value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"  # trim
  [[ -n "$value" ]] || { echo "  $name left unchanged (empty input)"; continue; }
  save "$name" "$value"
  echo "  $name saved"
  if [[ "$name" == TWITTER_ACCESS_TOKEN && "$value" =~ ^([0-9]+)- ]]; then
    save TWITTER_USER_ID "${BASH_REMATCH[1]}"
    echo "  TWITTER_USER_ID set to ${BASH_REMATCH[1]}"
  fi
done
echo "Apply with: scripts/bootstrap.sh"
