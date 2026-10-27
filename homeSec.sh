#!/usr/bin/env bash
#
# homeSec - e-mail webcam pictures that motion has saved.
#
# Usage:
#   homesec watch                 Run as a service: mail every new .jpg in IMAGE_DIR.
#   homesec [send] [--test] [IMAGE]
#                                 Mail one picture. Without IMAGE a picture is taken
#                                 with fswebcam. --test ignores the cooldown.
#
# Settings come from an env file (KEY=value lines), first one found of:
#   $HOMESEC_ENV, ./.env (next to this script), /etc/homesec/homesec.env
# See homesec.env.example.

set -euo pipefail
umask 002   # files must stay writable for the "motion" group

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SCRIPT_DIR/${BASH_SOURCE[0]##*/}"

usage() {
    cat <<'EOF'
Usage:
  homesec watch                      mail every new .jpg in IMAGE_DIR (service mode)
  homesec [send] [--test] [IMAGE]    mail one picture (takes one with fswebcam if no IMAGE)

--test ignores the cooldown. Settings: /etc/homesec/homesec.env (see homesec.env.example).
EOF
}

log() { printf '%s homeSec: %s\n' "$(date '+%F %T')" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---- settings ---------------------------------------------------------------
for envfile in "${HOMESEC_ENV:-}" "$SCRIPT_DIR/.env" /etc/homesec/homesec.env; do
    if [[ -n $envfile && -r $envfile ]]; then
        set -a
        # shellcheck source=/dev/null
        source "$envfile"
        set +a
        break
    fi
done

: "${EMAIL:=}"
: "${MAIL_FROM:=}"
: "${SUBJECT:=Motion detected}"
: "${IMAGE_DIR:=/var/lib/homesec}"
: "${VIDEO_DEVICE:=/dev/video0}"
: "${RESOLUTION:=1280x720}"
: "${SKIP_FRAMES:=10}"     # frames fswebcam throws away so auto-exposure can settle
: "${COOLDOWN:=300}"       # minimum seconds between two e-mails
: "${KEEP_DAYS:=7}"        # delete pictures in IMAGE_DIR older than this

# ---- subcommand ----------------------------------------------------------------
cmd=send
case ${1:-} in
    watch)     cmd=watch; shift ;;
    send)      shift ;;
    -h|--help) usage; exit 0 ;;
esac

if [[ $EMAIL != *@* ]]; then
    log "ERROR: EMAIL is not set. Edit /etc/homesec/homesec.env (see homesec.env.example)."
    exit 78   # EX_CONFIG: systemd unit does not restart on this
fi
mkdir -p "$IMAGE_DIR"

# ---- send one picture ------------------------------------------------------------
send() {
    local test_mode=0 image="" arg
    for arg in "$@"; do
        case $arg in
            --test)    test_mode=1 ;;
            -h|--help) usage; exit 0 ;;
            -*)        die "unknown option: $arg" ;;
            *)         image=$arg ;;
        esac
    done

    # Only one run at a time, and not more than one e-mail per COOLDOWN.
    exec 9>"$IMAGE_DIR/.homesec.lock"
    flock -n 9 || { log "another run is in progress, skipping"; return 0; }

    local stamp_file="$IMAGE_DIR/.homesec.last" now last=0
    now=$(date +%s)
    [[ -r $stamp_file ]] && last=$(<"$stamp_file")
    [[ $last =~ ^[0-9]+$ ]] || last=0
    if (( ! test_mode && now - last < COOLDOWN )); then
        log "cooldown active ($(( COOLDOWN - (now - last) ))s left), skipping"
        return 0
    fi

    if [[ -z $image ]]; then
        image="$IMAGE_DIR/$(date +%Y-%m-%d_%H%M%S).jpg"
        log "taking picture from $VIDEO_DEVICE"
        fswebcam -q -d "$VIDEO_DEVICE" -r "$RESOLUTION" -S "$SKIP_FRAMES" --no-banner "$image" \
            || die "fswebcam failed (is motion already using $VIDEO_DEVICE? Let motion take the pictures)"
    fi
    [[ -s $image ]] || die "picture not found or empty: $image"

    local mutt_opts=()
    if [[ -n $MAIL_FROM ]]; then
        mutt_opts+=(-e "set from=\"$MAIL_FROM\"" -e "set use_envelope_from=yes")
    fi

    log "sending $(basename "$image") to $EMAIL"
    if printf 'Motion detected on %s at %s.\n' "$(hostname)" "$(date '+%F %T')" \
        | timeout 90 mutt "${mutt_opts[@]}" -s "$SUBJECT" -a "$image" -- "$EMAIL"; then
        echo "$now" > "$stamp_file"
    else
        die "sending e-mail failed (check /etc/msmtprc and 'journalctl -t msmtp')"
    fi

    # Housekeeping: don't fill up the SD card.
    find "$IMAGE_DIR" -maxdepth 1 -type f -name '*.jpg' -mtime +"$KEEP_DAYS" -delete
}

# ---- service mode: mail every new picture motion saves ----------------------------
watch() {
    command -v inotifywait >/dev/null || die "inotifywait not found (sudo apt install inotify-tools)"
    log "watching $IMAGE_DIR for new pictures (cooldown ${COOLDOWN}s)"
    # close_write: the file is complete. A failed mail must not stop the watcher.
    inotifywait -m -q -e close_write -e moved_to --format '%w%f' "$IMAGE_DIR" \
        | while IFS= read -r file; do
            [[ $file == *.jpg ]] || continue
            "$SELF" send "$file" || log "could not send $file"
        done
    die "inotifywait stopped unexpectedly"
}

case $cmd in
    watch) watch ;;
    send)  send "$@" ;;
esac
