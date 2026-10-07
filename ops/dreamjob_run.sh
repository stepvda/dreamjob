#!/bin/bash
# Launch wrapper for the Dream Job backend (org.witysk.dreamjob).
#
# Kept as a script rather than inlined into the plist so the port, the graceful
# shutdown budget and the proxy-header trust list are readable and changeable in
# one place, and so `ops/dreamjob_run.sh` can be run by hand to reproduce
# exactly what launchd runs.
#
# --proxy-headers with --forwarded-allow-ips 127.0.0.1: Caddy terminates TLS and
# forwards over loopback, so without these uvicorn logs every client as 127.0.0.1
# and sees scheme http. Trusting ONLY 127.0.0.1 matters — a wider list would let
# a client spoof X-Forwarded-For through the proxy.
#
# --timeout-graceful-shutdown: pipeline requests can hold a connection for
# minutes (collection, profiling, generation). Without a bound, a restart waits
# on them forever; with too small a bound, a restart kills real work. 30s ends
# the idle keep-alives and lets launchd restart promptly.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT/backend"

exec "$ROOT/.venv/bin/python" -m uvicorn dreamjob.main:app \
    --host 127.0.0.1 \
    --port 8420 \
    --proxy-headers \
    --forwarded-allow-ips 127.0.0.1 \
    --timeout-graceful-shutdown 30
