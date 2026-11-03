#!/bin/sh
# HGXZ container entrypoint.
#
# Responsibilities, in order:
#   1. Make sure the four volume mount points exist and are writable.
#   2. Seed /config/config.json from the bundled example on first run.
#   3. Translate the environment into a usable runtime (TZ, umask, uid/gid).
#   4. exec python so signals (docker stop) reach the process directly.
set -eu

log() { printf '[entrypoint] %s\n' "$*" >&2; }

CONFIG_DIR="$(dirname "${HGXZ_CONFIG:-/config/config.json}")"
STATE_DIR="$(dirname "${HGXZ_STATE_DB:-/data/state.sqlite3}")"
LOG_DIR="${HGXZ_LOG_DIR:-/logs}"
MEDIA_DIR="${HGXZ_MEDIA_ROOT:-/media}"

# ---------------------------------------------------------------------------
# 1. volumes
# ---------------------------------------------------------------------------
for dir in "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR" "$MEDIA_DIR"; do
    mkdir -p "$dir" 2>/dev/null || true
    if [ ! -w "$dir" ]; then
        log "WARNING: $dir is not writable by uid $(id -u)."
        log "         On UGREEN/Unraid/Synology check the shared-folder ACL or set PUID/PGID."
    fi
done

# ---------------------------------------------------------------------------
# 2. config bootstrap
# ---------------------------------------------------------------------------
CONFIG_FILE="${HGXZ_CONFIG:-/config/config.json}"
if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f /app/config.example.json ]; then
        cp /app/config.example.json "$CONFIG_FILE"
        log "created $CONFIG_FILE from the bundled example"
    else
        log "WARNING: $CONFIG_FILE missing and no example to copy; the app will use built-in defaults"
    fi
fi

# ---------------------------------------------------------------------------
# 3. runtime environment
# ---------------------------------------------------------------------------
if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
fi

# Allow the host's proxy settings to reach urllib, which only reads lowercase
# names on some Python builds.
if [ -n "${HTTP_PROXY:-}" ] && [ -z "${http_proxy:-}" ]; then export http_proxy="$HTTP_PROXY"; fi
if [ -n "${HTTPS_PROXY:-}" ] && [ -z "${https_proxy:-}" ]; then export https_proxy="$HTTPS_PROXY"; fi
if [ -n "${NO_PROXY:-}" ] && [ -z "${no_proxy:-}" ]; then export no_proxy="$NO_PROXY"; fi

if [ -n "${UMASK:-}" ]; then
    # `umask` only accepts octal; a bad value must not abort the container.
    if umask "$UMASK" 2>/dev/null; then
        log "umask set to $UMASK"
    else
        log "WARNING: invalid UMASK=$UMASK, keeping the default"
    fi
fi

# PUID/PGID mirror the LinuxServer.io convention NAS users already know. The
# app re-execs itself as that uid/gid (see HGXZ.cli.drop_privileges), so this
# only needs to export the values and make the volumes belong to them.
if [ -n "${PUID:-}" ] || [ -n "${PGID:-}" ]; then
    log "will drop privileges to PUID=${PUID:-0} PGID=${PGID:-0}"
fi

log "starting: media=$MEDIA_DIR state=$STATE_DIR config=$CONFIG_FILE port=${HGXZ_PORT:-7731} api=${HGXZ_API_PORT:-7732} tz=${TZ:-unset}"

# ---------------------------------------------------------------------------
# 4. hand over to the app
# ---------------------------------------------------------------------------
exec python -m HGXZ.cli "$@"
