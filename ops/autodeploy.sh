#!/bin/bash
# Continuous deployment for Dream Job on this server (org.witysk.dreamjob.autodeploy).
#
# launchd runs this every minute.  It fetches main from GitHub and, when main
# has moved, deploys the new commit with that commit's own ops/deploy.sh - but
# only once GitHub Actions CI (.github/workflows/ci.yml) has passed on it.
#
# The server pulls; GitHub never pushes.  Nothing from GitHub runs here except
# commits on main, which only the repository's owner can put there, and CI's
# result is read from the public API without a token.  (A self-hosted Actions
# runner would have let a pull request to this public repository run code on
# the production host.)
#
# Rules:
#   - CI pending: wait.  CI failed: do not deploy (a re-run that passes is
#     picked up).  No CI run 30 minutes after the commit: do not deploy.
#   - A deploy that failed and rolled back is not retried for that commit;
#     push a fix, or run ops/deploy.sh <commit> by hand.
#   - Pause deploys with `touch logs/deploy/PAUSED`; remove it to resume.
#   - Several pushes in a row deploy only the newest.
#
# Everything is logged to logs/deploy.log, each situation once rather than
# once a minute.
#
# Install (once):
#   cp ops/org.witysk.dreamjob.autodeploy.plist ~/Library/LaunchAgents/
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.witysk.dreamjob.autodeploy.plist

REPO=stepvda/dreamjob
BRANCH=main
#: GitHub's anonymous API allows 60 requests an hour; ask at most every 2 minutes.
CI_CHECK_EVERY_SECONDS=110
#: A commit with no CI run after this long is not going to get one.
NO_CI_AFTER_SECONDS=1800

log() {
    printf '%s autodeploy: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"
}

# Log a situation once, not once a minute.
notice() {
    local key="$1"
    shift
    if [ "$(cat "$STATE/notice" 2>/dev/null)" != "$key" ]; then
        printf '%s' "$key" >"$STATE/notice"
        log "$*"
    fi
}

ci_state() {
    local json
    json="$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/$REPO/commits/$1/check-runs?per_page=100" 2>/dev/null)" ||
        { echo error; return; }
    printf '%s' "$json" | /usr/bin/python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin)["check_runs"]
except Exception:
    print("error")
    raise SystemExit
if not runs:
    print("none")
elif any(r.get("status") != "completed" for r in runs):
    print("pending")
elif all(r.get("conclusion") in ("success", "skipped", "neutral") for r in runs):
    print("success")
else:
    print("failure")
'
}

main() {
    set -uo pipefail
    ROOT="${DREAMJOB_ROOT:-$HOME/Dev/dreamjob}"
    export PATH="/opt/homebrew/bin:/opt/homebrew/opt/node/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    export GIT_TERMINAL_PROMPT=0
    cd "$ROOT" || exit 1
    LOG="$ROOT/logs/deploy.log"
    STATE="$ROOT/logs/deploy"
    mkdir -p "$STATE"

    [ -e "$STATE/PAUSED" ] && { notice paused "paused ($STATE/PAUSED exists)"; return 0; }

    # One at a time; a lock older than an hour is from a run that died.
    if ! mkdir "$STATE/lock" 2>/dev/null; then
        if [ -n "$(find "$STATE/lock" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
            rmdir "$STATE/lock" 2>/dev/null
            log "removed a stale lock"
        fi
        return 0
    fi
    trap 'rmdir "$STATE/lock" 2>/dev/null' EXIT

    if ! git fetch --quiet origin "$BRANCH" 2>/dev/null; then
        notice fetch-failed "could not fetch $BRANCH from GitHub; will keep trying"
        return 0
    fi
    local target current short now last age
    target="$(git rev-parse "origin/$BRANCH")"
    current="$(git rev-parse HEAD)"
    short="${target:0:7}"
    [ "$target" = "$current" ] && return 0
    [ "$(cat "$STATE/failed" 2>/dev/null)" = "$target" ] && return 0

    # What deploy.sh would refuse, said once here instead of every two minutes.
    if [ "$(git symbolic-ref --quiet --short HEAD)" != "$BRANCH" ]; then
        notice "refused-branch-$target" "not deploying $short: the checkout is not on $BRANCH"
        return 0
    fi
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        notice "refused-dirty-$target" "not deploying $short: local changes to tracked files"
        return 0
    fi
    if ! git merge-base --is-ancestor "$current" "$target"; then
        notice "refused-diverged-$target" \
            "not deploying $short: the checkout has commits that are not on origin/$BRANCH"
        return 0
    fi

    now="$(date +%s)"
    last="$(cat "$STATE/ci_checked_at" 2>/dev/null || echo 0)"
    [ $((now - last)) -lt "$CI_CHECK_EVERY_SECONDS" ] && return 0
    printf '%s' "$now" >"$STATE/ci_checked_at"

    case "$(ci_state "$target")" in
        success) ;;
        pending)
            notice "pending-$target" "waiting for CI on $short"
            return 0
            ;;
        failure)
            notice "failure-$target" "CI failed on $short; not deploying it"
            return 0
            ;;
        none)
            age=$((now - $(git show -s --format=%ct "$target")))
            if [ "$age" -gt "$NO_CI_AFTER_SECONDS" ]; then
                notice "none-$target" "no CI run for $short after 30 minutes; not deploying it"
            else
                notice "pending-$target" "waiting for CI to start on $short"
            fi
            return 0
            ;;
        *)
            notice "error-$target" "could not read CI status for $short; will keep trying"
            return 0
            ;;
    esac

    log "CI passed on $short; deploying"
    git show "$target:ops/deploy.sh" >"$STATE/deploy.sh" || {
        log "$short has no ops/deploy.sh; not deploying it"
        printf '%s' "$target" >"$STATE/failed"
        return 0
    }
    /bin/bash "$STATE/deploy.sh" "$target" </dev/null
    case $? in
        0) rm -f "$STATE/failed" ;;
        2) notice "refused-$target" "deploy of $short refused; see the line above" ;;
        *) printf '%s' "$target" >"$STATE/failed" ;;
    esac
    printf '' >"$STATE/notice"
    return 0
}

main "$@"
exit $?
