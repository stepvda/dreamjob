#!/bin/bash
# Deploy one commit of Dream Job on this server (macstudio).
#
#   ops/deploy.sh <commit>
#
# ops/autodeploy.sh runs this once CI has passed on a new commit of main, from
# the copy of it inside that commit (extracted with `git show`), so a change to
# the procedure takes effect in the deploy that ships it.  It is also safe to
# run by hand to deploy a commit that autodeploy skipped.
#
# In order, doing only what the commit needs:
#   1. refuses unless the checkout is on main, has no local changes to tracked
#      files, and the commit is a fast-forward - it never discards work;
#   2. fast-forwards the checkout;
#   3. installs Python dependencies when requirements.txt or
#      requirements.lock changed, at the locked versions CI tested;
#   4. builds the frontend when frontend/ changed (npm ci when the lockfile
#      did) into frontend/dist.new - not yet served;
#   5. restarts the backend when anything it runs changed (reloading the
#      launchd job when its plist changed); migrations apply on startup and
#      interrupted jobs resume (NFR-401);
#   6. waits for /api/health from the *new* process;
#   7. swaps the new frontend build in whole, keeping the old one as
#      frontend/dist.prev, and writes /version.json (commit, time) into it, so
#      https://dreamjob.one.witysk.org/version.json says what is live;
#   8. reloads Caddy when ops/Caddyfile.dreamjob changed, the way onevoice's
#      caddy_run.sh starts it: with BACKEND_PORT exported.  A bare
#      `caddy reload` broke every one.witysk.org API call once.
#
# When the new backend does not come up healthy, the checkout is put back on
# the previous commit and the old backend restarted; the new frontend build
# was never served.  Database migrations are not undone - they are additive,
# and the previous code runs on the newer schema - and dependencies are only
# ever added or upgraded.
#
# Exit status: 0 deployed (or nothing to do), 1 failed and rolled back,
# 2 refused before changing anything.

# Overridable only so the procedure can be rehearsed against a stand-in job.
LABEL="${DREAMJOB_LAUNCHD_LABEL:-org.witysk.dreamjob}"
HEALTH_URL="${DREAMJOB_HEALTH_URL:-http://127.0.0.1:8420/api/health}"
#: Startup runs the migrations, so allow a slow one before calling it failed.
HEALTH_TIMEOUT_SECONDS="${DREAMJOB_HEALTH_TIMEOUT_SECONDS:-300}"

log() {
    printf '%s deploy %s: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${short:-?}" "$*"
}

refuse() {
    log "REFUSED: $*"
    exit 2
}

backend_pid() {
    launchctl list | awk -v label="$LABEL" '$3 == label { print $1 }'
}

# Healthy means: a process other than the one we stopped, answering ok.
wait_healthy() {
    local stopped="$1" deadline pid
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        pid="$(backend_pid)"
        if [ -n "$pid" ] && [ "$pid" != "-" ] && [ "$pid" != "$stopped" ] &&
            curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null | grep -q '"ok"'; then
            log "backend healthy (pid $pid)"
            return 0
        fi
        sleep 2
    done
    return 1
}

restart_backend() {
    local stopped plist="$HOME/Library/LaunchAgents/$LABEL.plist" waited=0
    stopped="$(backend_pid)"
    if [ "$reload_plist" = 1 ]; then
        log "reloading the launchd job with the new plist"
        cp ops/$LABEL.plist "$plist"
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
        # bootout returns before the job is gone; bootstrap refuses a label
        # that is still loaded.
        while launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && [ "$waited" -lt 60 ]; do
            sleep 1
            waited=$((waited + 1))
        done
        launchctl bootstrap "$DOMAIN" "$plist"
    else
        log "restarting the backend (pid ${stopped:-none})"
        launchctl kickstart -k "$DOMAIN/$LABEL"
    fi
    wait_healthy "$stopped"
}

rollback() {
    log "ROLLING BACK to ${previous:0:7}: $*"
    rm -rf frontend/dist.new
    git reset --hard --quiet "$previous"
    if [ "$restarted" = 1 ]; then
        if restart_backend; then
            log "rolled back; the previous version is running"
        else
            log "ROLLBACK DID NOT COME UP HEALTHY - the backend needs a look"
        fi
    fi
    exit 1
}

reload_caddy() {
    local onevoice="$HOME/Dev/onevoice" port caddyfile
    port="$(head -1 "$onevoice/react/local/run/backend_port" 2>/dev/null | tr -d '[:space:]')"
    case "$port" in
        '' | *[!0-9]*) port="" ;;
    esac
    caddyfile="$onevoice/react/Caddyfile.$(hostname -s)"
    [ -f "$caddyfile" ] || caddyfile="$onevoice/react/Caddyfile"
    (
        if [ -n "$port" ]; then export BACKEND_PORT="$port"; fi
        caddy adapt --config "$caddyfile" >/dev/null && caddy reload --config "$caddyfile"
    )
}

main() {
    set -euo pipefail
    target="${1:?usage: deploy.sh <commit>}"
    short="${target:0:7}"
    ROOT="${DREAMJOB_ROOT:-$HOME/Dev/dreamjob}"
    DOMAIN="gui/$(id -u)"
    export PATH="/opt/homebrew/bin:/opt/homebrew/opt/node/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    export GIT_TERMINAL_PROMPT=0
    cd "$ROOT"

    mkdir -p logs
    if [ -t 1 ]; then
        exec > >(tee -a logs/deploy.log) 2>&1
    else
        exec >>logs/deploy.log 2>&1
    fi

    # 1. Never discard anything.
    [ "$(git symbolic-ref --quiet --short HEAD || true)" = main ] ||
        refuse "the checkout is not on main"
    [ -z "$(git status --porcelain --untracked-files=no)" ] ||
        refuse "there are local changes to tracked files"
    git cat-file -e "$target^{commit}" 2>/dev/null || git fetch --quiet origin main
    target="$(git rev-parse "$target^{commit}")"
    short="${target:0:7}"
    previous="$(git rev-parse HEAD)"
    if [ "$previous" = "$target" ]; then
        log "already running $short"
        exit 0
    fi
    git merge-base --is-ancestor "$previous" "$target" ||
        refuse "$short is not a fast-forward of ${previous:0:7}"

    changed="$(git diff --name-only "$previous" "$target")"
    log "deploying $(git log -1 --format='%s' "$target") (from ${previous:0:7}, $(wc -l <<<"$changed" | tr -d ' ') files)"

    restart=0 reload_plist=0 frontend=0 npm_ci=0 deps=0 caddy=0 restarted=0
    while IFS= read -r path; do
        case "$path" in
            requirements.txt | requirements.lock | pyproject.toml) deps=1 restart=1 ;;
            frontend/package-lock.json | frontend/package.json) frontend=1 npm_ci=1 ;;
            frontend/*) frontend=1 ;;
            ops/$LABEL.plist) reload_plist=1 restart=1 ;;
            ops/Caddyfile.dreamjob) caddy=1 ;;
            ops/deploy.sh | ops/autodeploy.sh | ops/$LABEL.autodeploy.plist) ;;
            docs/* | tests/* | .github/* | *.md | .gitignore | "") ;;
            *) restart=1 ;;
        esac
    done <<<"$changed"
    [ -f frontend/dist/index.html ] || frontend=1

    # 2. The code.
    git merge --ff-only --quiet "$target"

    # 3. Dependencies.
    if [ "$deps" = 1 ]; then
        log "installing Python dependencies"
        .venv/bin/python -m pip install --quiet --disable-pip-version-check \
            -r requirements.txt -c requirements.lock ||
            rollback "pip install failed"
    fi

    # 4. The frontend, built aside.
    if [ "$frontend" = 1 ]; then
        log "building the frontend"
        (
            cd frontend
            if [ "$npm_ci" = 1 ] || [ ! -d node_modules ]; then
                npm ci --no-audit --no-fund
            fi
            rm -rf dist.new
            npm run build -- --outDir dist.new --emptyOutDir
            [ -f dist.new/index.html ]
        ) || rollback "the frontend did not build"
    fi

    # 5-6. The backend.
    if [ "$restart" = 1 ]; then
        restarted=1
        restart_backend || rollback "the new backend did not answer $HEALTH_URL"
    else
        log "no backend change; not restarting"
    fi

    # 7. Serve the new frontend.
    if [ "$frontend" = 1 ]; then
        rm -rf frontend/dist.prev
        if [ -d frontend/dist ]; then mv frontend/dist frontend/dist.prev; fi
        mv frontend/dist.new frontend/dist
        log "frontend swapped in"
    fi
    if [ -d frontend/dist ]; then
        printf '{"commit": "%s", "deployed_at": "%s"}\n' \
            "$target" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >frontend/dist/version.json
    fi

    # 8. The proxy.  A failed reload leaves Caddy on its previous config, and
    # the app itself is deployed, so this fails the deploy without undoing it.
    if [ "$caddy" = 1 ]; then
        log "reloading Caddy"
        reload_caddy || {
            log "CADDY RELOAD FAILED - Caddy kept its previous configuration"
            exit 1
        }
    fi

    log "deployed $short"
}

main "$@"
exit $?
