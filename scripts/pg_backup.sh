#!/bin/bash
set -euo pipefail

BACKUP_DIR="/opt/discord-archiver/backups"
mkdir -p "$BACKUP_DIR"

TS=$(date +%Y%m%d-%H%M)
FILE="$BACKUP_DIR/discord_archiver_$TS.dump"

echo "[backup] Starting VACUUM ANALYZE"
psql -U archiver -d discord_archiver -c "VACUUM ANALYZE;"

echo "[backup] Writing $FILE"
pg_dump -U archiver -Fc discord_archiver > "$FILE"

echo "[backup] Keeping last 2 backups"
ls -1t "$BACKUP_DIR"/discord_archiver_*.dump | tail -n +3 | xargs -r rm -f

echo "[backup] Done"
