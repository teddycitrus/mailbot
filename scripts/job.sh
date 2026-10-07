#!/bin/sh
# Every scheduled job on the server, one entry point.
#
# The POSIX twin of the .ps1 wrappers beside it, which stay for the laptop.
# Same command sequences, same log files, same rule about exit codes: report
# the worst code any step returned, because systemd judges the unit by it and
# a run whose enrich failed is not a run that succeeded.
#
# prep and tick write to send-<date>.log, as they do on Windows, so the health
# check and the digest find one file per day however the job was started.

set -u

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root" || exit 2
py="$root/.venv/bin/python"
job=${1:?usage: job.sh <prep|tick|draft|build|reprobe|health|bounce|digest>}

case "$job" in
    prep|tick) log_name=send ;;
    *)         log_name=$job ;;
esac
mkdir -p "$root/logs"
log="$root/logs/$log_name-$(date +%F).log"

worst=0
run() {
    out=$("$py" -m src.main "$@" 2>&1)
    code=$?
    printf '%s\n' "$out" >> "$log"
    if [ "$code" -gt "$worst" ]; then worst=$code; fi
    return 0
}

printf '=== %s run %s ===\n' "$job" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$log"

case "$job" in
    prep)
        # The slow full mailbox scan, once, before the window opens.
        run inbox
        ;;
    tick)
        # Bounces and opt-outs first: someone who says stop at 09:00 must not
        # be mailed by the 09:15 tick. Then mirror, which notices anything
        # sent by hand, and only then send.
        run inbox --days 1 --limit 40
        run mirror --limit 25 --days 2
        run send --limit 2
        run followup --limit 1
        ;;
    draft)
        run queue --limit 25
        run mirror --limit 50
        ;;
    build)
        run hn --limit 60 --months 3
        run discover --limit 60
        run gh --limit 60
        run enrich --limit 90
        run queue --limit 40
        run mirror --limit 60
        run stats
        ;;
    reprobe)
        # Port 25 is blocked here, so probes go through the laptop's tunnel.
        # Exits in a second when the laptop is off; when it is on, re-probes
        # what the other jobs could not and enriches a few companies.
        run reprobe --limit 25 --enrich 5
        ;;
    health)
        run health
        ;;
    bounce)
        run inbox --days 1 --limit 80
        run bounce-report
        ;;
    digest)
        run digest --days 7
        ;;
    *)
        echo "unknown job: $job" >&2
        exit 2
        ;;
esac

printf '=== finished %s exit=%s ===\n' "$(date +%H:%M:%S)" "$worst" >> "$log"
exit "$worst"
