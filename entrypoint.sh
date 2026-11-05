#!/bin/sh
# Starts as root only to make the mounted data directory writable (platform disks arrive root-owned),
# then drops to the unprivileged 'dc' user before running anything. Extra args replace the default command.
set -e
mkdir -p /var/lib/dc
chown dc:dc /var/lib/dc 2>/dev/null || true
if [ "$#" -eq 0 ]; then
  set -- uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 \
         --proxy-headers --forwarded-allow-ips "*"
fi
exec setpriv --reuid=dc --regid=dc --init-groups -- "$@"
