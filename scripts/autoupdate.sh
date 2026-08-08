#!/usr/bin/env bash
#
# Auto-update the CC RMS MQTT monitor from git, restart only if something
# changed. Intended to run as root from the systemd timer (so it can restart
# the service); git and pip run as the repo owner.
#
# Configurable via environment (the systemd unit sets these):
#   CC_DIR      checkout to update     (default /home/ops/source/CC_Utils/MQTT_monitor)
#   CC_USER     repo/venv owner        (default ops)
#   CC_VENV     virtualenv             (default /home/<CC_USER>/vRMS)
#   CC_SERVICE  systemd service        (default cc-rms-monitor)
#   CC_BRANCH   branch to track        (default master)
#
set -euo pipefail

DIR="${CC_DIR:-/home/ops/source/CC_Utils/MQTT_monitor}"
RUN_USER="${CC_USER:-ops}"
VENV="${CC_VENV:-/home/${RUN_USER}/vRMS}"
SERVICE="${CC_SERVICE:-cc-rms-monitor}"
BRANCH="${CC_BRANCH:-master}"
# A blocked update used to exit 0 and print to a journal nobody reads, so a station could
# sit on old code indefinitely with nothing to show for it. Record the reason where the
# monitor can publish it, and exit non-zero so systemd marks the unit failed.
MARKER="${CC_UPDATE_MARKER:-/var/lib/cc-rms-monitor/update_blocked}"
# Stations are meant to be identical clean installs whose ONLY local file is the
# gitignored config.yaml, so a modified tracked file is a mistake -- and one that
# silently freezes the station on old code. Default to recovering from it instead of
# giving up. Nothing is destroyed: local commits are kept on a rescue branch and
# uncommitted edits in a stash, both recoverable on the box. config.yaml is gitignored,
# so git never touches it.
# Opt OUT on a box with deliberate local changes (a dev machine):
#   touch "$DIR/.no-autoupdate-force"      or set CC_UPDATE_FORCE=0
FORCE="${CC_UPDATE_FORCE:-1}"
mkdir -p "$(dirname "$MARKER")" 2>/dev/null || true
blocked() {
    printf '%s\n' "$1" > "$MARKER" 2>/dev/null || true
    echo "$1" >&2
    exit 1
}

# Run a command as the repo owner when we're root; otherwise run it directly.
run_as() {
    if [ "$(id -u)" -eq 0 ]; then
        sudo -u "$RUN_USER" -H "$@"
    else
        "$@"
    fi
}

before="$(run_as git -C "$DIR" rev-parse HEAD)"
run_as git -C "$DIR" fetch --quiet origin "$BRANCH"

# Fast-forward only: never clobber local commits / diverged history.
if ! run_as git -C "$DIR" merge --ff-only "origin/$BRANCH" >/dev/null 2>&1; then
    if [ "$FORCE" = "1" ] && [ ! -f "$DIR/.no-autoupdate-force" ]; then
        ts="$(date -u +%Y%m%d-%H%M%S)"
        what="$(run_as git -C "$DIR" status --porcelain 2>/dev/null | head -10 | tr '\n' ';')"
        ahead="$(run_as git -C "$DIR" rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)"
        # keep local COMMITS on a branch and uncommitted TRACKED edits in a stash
        run_as git -C "$DIR" branch --force "cc-autoupdate-rescue-$ts" HEAD >/dev/null 2>&1 || true
        if ! run_as git -C "$DIR" diff --quiet 2>/dev/null; then
            run_as git -C "$DIR" stash push -m "cc-autoupdate rescue $ts" >/dev/null 2>&1 || true
        fi
        if run_as git -C "$DIR" reset --hard "origin/$BRANCH" >/dev/null 2>&1; then
            echo "Local changes blocked the update; rescued to branch cc-autoupdate-rescue-$ts" \
                 "(commits ahead: $ahead; modified: ${what:-none}) and reset to origin/$BRANCH." >&2
            rm -f "$MARKER" 2>/dev/null || true
        else
            blocked "auto-update blocked: reset to origin/$BRANCH failed after rescue (modified: ${what:-none})"
        fi
    else
    # Name the offending files. The usual cause is a TRACKED file edited in place -- the
    # repo has several plausibly-named "config" files and only config.yaml is ignored:
    #   config.yaml            <- gitignored, safe to edit (this is the one to edit)
    #   config.example.yaml    <- TRACKED template; editing it blocks updates
    #   cc_mqtt_monitor/config.py <- TRACKED source; editing it blocks updates
    # Without the file list the operator has no way to know which, so say it explicitly.
    dirty="$(run_as git -C "$DIR" status --porcelain 2>/dev/null | head -10 | tr '\n' ';')"
    ahead="$(run_as git -C "$DIR" rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo '?')"
    blocked "auto-update blocked: cannot fast-forward to origin/$BRANCH (local commits ahead: ${ahead}; modified: ${dirty:-none}). Edit config.yaml only -- config.example.yaml and cc_mqtt_monitor/config.py are tracked. Fix with: git -C $DIR checkout -- <file>  (or stash), then the next run recovers automatically."
    fi
fi

after="$(run_as git -C "$DIR" rev-parse HEAD)"

rm -f "$MARKER" 2>/dev/null || true          # we fast-forwarded fine: not blocked

if [ "$before" = "$after" ]; then
    echo "Already up to date ($after)."
    exit 0
fi

echo "Updated $before -> $after; reinstalling and restarting $SERVICE."
# Reinstall (cheap; picks up any dependency/entry-point changes).
run_as "$VENV/bin/pip" install --quiet -e "$DIR"
systemctl restart "$SERVICE"
echo "Restarted $SERVICE."
