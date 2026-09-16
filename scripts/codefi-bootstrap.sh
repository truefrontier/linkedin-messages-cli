#!/bin/bash
# Run ON Codefi Mac (kk). Does NOT touch chrome-profile or kill Chrome.
set -euo pipefail
REPO="${1:-/Users/kk/Sites/truefrontier/linkedin-messages-cli}"
STATUS="$HOME/.linkedin-messages-cli/login-status.txt"
KEEP_UX="/Users/kk/Sites/truefrontier/google-keep-cli/keep_cli/agent_ux.py"

echo "host=$(hostname) user=$(whoami) home=$HOME"
test -d "$REPO" || { echo "MISSING_REPO $REPO"; exit 1; }
cd "$REPO"

if [[ -f "$KEEP_UX" ]]; then
  cp "$KEEP_UX" limsg_cli/agent_ux.py
  echo "copied agent_ux from google-keep-cli"
else
  echo "WARN: keep agent_ux not found; keeping bundled copy"
fi

echo "polling $STATUS for 'ok messaging loaded' (up to ~8 min)..."
for i in $(seq 1 96); do
  if [[ -f "$STATUS" ]] && grep -q 'ok messaging loaded' "$STATUS"; then
    echo "LOGIN_OK at attempt $i"
    break
  fi
  if (( i == 96 )); then
    echo "LOGIN_TIMEOUT content=$(cat "$STATUS" 2>/dev/null || echo MISSING)"
    exit 4
  fi
  sleep 5
done

pipx install --force "$REPO"
command -v limsg
limsg --help | head -20

limsg messages list --limit 10 --compact > /tmp/limsg-list.json 2>/tmp/limsg-list.err || true
echo "--- stderr ---"; cat /tmp/limsg-list.err || true
python3 - <<'PY2'
import json,sys
p="/tmp/limsg-list.json"
try:
  data=json.load(open(p))
except Exception as e:
  print("LIST_PARSE_FAIL", e)
  sys.exit(5)
if not isinstance(data, list):
  print("LIST_UNEXPECTED", type(data).__name__)
  sys.exit(5)
print("THREAD_COUNT", len(data))
boardy=any("boardy" in str(r.get("peer") or "").lower() for r in data)
print("BOARDY_IN_TITLES", boardy)
PY2
