#!/bin/sh
# Plexbie doesn't run as root. Started as root (the default), this hands the
# folders Plexbie writes to PUID:PGID - 99:100 is Unraid's "nobody:users", the
# owner of a normal share - and then runs Plexbie as that user. Started as
# someone else already (docker run --user), it just runs.
#
# PUID or PGID 0 would mean running as root after all, so it's refused (99:100
# is used instead) unless PLEXBIE_ALLOW_ROOT=1 says that's really wanted.
#
# What Plexbie writes (settings, database, logs) is kept from other users of the
# machine: owner read-write, group read-only (Unraid's "users"), nobody else.
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"
if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
    if [ "${PLEXBIE_ALLOW_ROOT:-0}" = "1" ]; then
        echo "Running as root because PLEXBIE_ALLOW_ROOT=1." >&2
    else
        echo "PUID/PGID 0 would run Plexbie as root; using 99:100 instead (set PLEXBIE_ALLOW_ROOT=1 to insist)." >&2
        PUID=99
        PGID=100
    fi
fi
umask 027
if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/config /app/logs /app/cache
    chown -R "$PUID:$PGID" /app/config /app/logs /app/cache
    chmod -R g-w,o-rwx /app/config /app/logs /app/cache
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups env HOME=/tmp "$@"
fi
exec "$@"
