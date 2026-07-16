#!/bin/sh
set -eu

VOHIVE_CONFIG=/opt/vohive/config/config.yaml
VOHIVE_BACKUP=/opt/vohive/config/config.yaml.before-keepalive-gateway
KEEPALIVE_ENV=/etc/vohive-keepalive/service.env

if [ ! -f "$VOHIVE_BACKUP" ]; then
    echo "VoHive backup not found: $VOHIVE_BACKUP" >&2
    exit 1
fi

systemctl stop nginx.service
cp "$VOHIVE_BACKUP" "$VOHIVE_CONFIG"

python3 - "$KEEPALIVE_ENV" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
result = []
found = False
for line in lines:
    if line.startswith("VOHIVE_BASE_URL="):
        result.append("VOHIVE_BASE_URL=http://127.0.0.1:7575/api")
        found = True
    else:
        result.append(line)
if not found:
    result.append("VOHIVE_BASE_URL=http://127.0.0.1:7575/api")
path.write_text("\n".join(result) + "\n")
path.chmod(0o600)
PY

systemctl restart vohive.service
systemctl restart vohive-keepalive.service
echo "VoHive gateway rolled back; VoHive is listening on port 7575 directly."
