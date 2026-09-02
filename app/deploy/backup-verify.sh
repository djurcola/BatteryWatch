#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${BATTERYWATCH_DATABASE_URL:?BATTERYWATCH_DATABASE_URL is required}"
if [[ $# -ne 1 ]]; then
  printf 'usage: %s BACKUP_DIRECTORY\n' "$0" >&2
  exit 64
fi
backup_dir=$1
mkdir -p -- "$backup_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$backup_dir/batterywatch-$stamp.dump"
manifest="$backup.sha256"

database_url=$BATTERYWATCH_DATABASE_URL
unset BATTERYWATCH_DATABASE_URL

pg_dump --dbname="$database_url" --format=custom --file="$backup"
unset database_url
pg_restore --list "$backup" >/dev/null
sha256sum "$backup" >"$manifest"
printf 'backup=%s\nmanifest=%s\n' "$backup" "$manifest"
