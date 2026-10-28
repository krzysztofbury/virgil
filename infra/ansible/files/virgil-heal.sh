#!/bin/sh
# Restart Virgil when Docker reports it unhealthy (#174). Docker's restart
# policy only covers a process that exits, and the autoheal container this
# replaces needed the Docker socket. Runs from virgil-heal.timer as root.
set -eu

status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' virgil 2>/dev/null) || exit 0
[ "$status" = unhealthy ] || exit 0

logger -t virgil-heal "virgil is unhealthy, restarting it"
docker restart --timeout 30 virgil >/dev/null

# Optional: the same JSON {"text": ...} alert the app sends (ALERT_WEBHOOK_URL).
if [ -r /etc/virgil/heal.env ]; then
    # shellcheck disable=SC1091
    . /etc/virgil/heal.env
fi
if [ -n "${ALERT_WEBHOOK_URL:-}" ]; then
    ALERT_WEBHOOK_URL="$ALERT_WEBHOOK_URL" python3 -c '
import json, os, socket, urllib.request
body = json.dumps({"text": f"Virgil on {socket.gethostname()} was unhealthy and has been restarted."}).encode()
request = urllib.request.Request(os.environ["ALERT_WEBHOOK_URL"], body, {"Content-Type": "application/json"})
urllib.request.urlopen(request, timeout=10)
' || logger -t virgil-heal "the restart alert could not be sent"
fi
