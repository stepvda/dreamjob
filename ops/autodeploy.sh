#!/bin/bash
# Continuous deployment for Dream Job on this server (org.witysk.dreamjob.autodeploy).
#
# launchd runs this every minute.  It fetches the `deploy` branch from GitHub
# and, when that has moved, deploys the new commit with the commit's own
# ops/deploy.sh.
#
# `deploy` is moved only by CI (.github/workflows/ci.yml): once the build and
# the tests have passed on a push to main, its last job fast-forwards `deploy`
# to that commit.  So the gate is a git ref, and this needs nothing but
# `git fetch` - no GitHub API, no token, no rate limit shared with whatever
# else on this machine talks to GitHub.  The server pulls; GitHub never
# reaches into it, and no runner is installed here (a self-hosted runner on a
# public repository would let a pull request run code on the production host).
#
# Rules:
#   - A deploy that failed and rolled back is not retried for that commit;
#     push a fix, or run ops/deploy.sh <commit> by hand.
#   - Pause deploys with `touch logs/deploy/PAUSED`; remove it to resume.
#   - Several pushes in a row deploy only the newest that passed CI.
#
# Everything is logged to logs/deploy.log, each situation once rather than
# once a minute.
#
# Install (once):
#   cp ops/org.witysk.dreamjob.autodeploy.plist ~/Library/LaunchAgents/
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.witysk.dreamjob.autodeploy.plist

#: Moved by CI to each commit of main that passed it.
DEPLOY_BRANCH=deploy
#: What the checkout is on, and what `deploy` commits are always part of.
BRANCH=main

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

    # Both: deploy names the commit, main is what the checkout fast-forwards
    # along (ops/deploy.sh fetches main when it does not have the commit).
    if ! git fetch --quiet origin "$DEPLOY_BRANCH" "$BRANCH" 2>/dev/null; then
        notice fetch-failed "could not fetch $DEPLOY_BRANCH from GitHub; will keep trying"
        return 0
    fi
    local target current short
    target="$(git rev-parse "origin/$DEPLOY_BRANCH")"
    current="$(git rev-parse HEAD)"
    short="${target:0:7}"
    [ "$target" = "$current" ] && return 0
    [ "$(cat "$STATE/failed" 2>/dev/null)" = "$target" ] && return 0
    # deploy behind the checkout (a commit deployed by hand): nothing newer.
    git merge-base --is-ancestor "$target" "$current" && return 0

    # What deploy.sh would refuse, said once here instead of every minute.
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
            "not deploying $short: the checkout has commits that are not on origin/$DEPLOY_BRANCH"
        return 0
    fi

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
