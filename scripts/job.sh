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

# The second sending account, when one is set up: .env.volume beside .env.
# It runs after main in every job that sends or reads mail, so main always
# gets first pick of the shared queue.
volume() {
    if [ -f "$root/.env.volume" ]; then run --lane volume "$@"; fi
}

printf '=== %s run %s ===\n' "$job" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$log"

case "$job" in
    prep)
        # The slow full mailbox scan, once, before the window opens.
        run inbox
        volume inbox
        ;;
    tick)
        # Bounces and opt-outs first: someone who says stop at 09:00 must not
        # be mailed by the 09:15 tick. Then mirror, which notices anything
        # sent by hand, and only then send. The limits are only a backstop:
        # send and followup spread the day's cap over the ticks left in each
        # recipient's window themselves.
        run inbox --days 1 --limit 40
        volume inbox --days 1 --limit 40
        run mirror --limit 25 --days 2
        run send --limit 50
        run followup --limit 50
        volume send --limit 50
        volume followup --limit 50
        # The volume lane cannot reach main's Drafts, so a copy of a draft
        # it just sent is cleared here, in the same tick, not 15 minutes on.
        if [ -f "$root/.env.volume" ]; then run mirror --limit 25 --days 2; fi
        ;;
    draft)
        run queue --limit 25
        run mirror --limit 50
        ;;
    build)
        run hn --limit 60 --months 3
        run discover --limit 300
        run gh --limit 60
        run vc --limit 150
        # Every parked catch-all company back in, for the volume lane's
        # guesses. Does nothing unless VOLUME_GUESSES is on.
        run reopen-catchall --limit 400
        run enrich --limit 400
        # Next permutation for anyone whose guess hard-bounced, so it is
        # drafted below. Also a no-op unless VOLUME_GUESSES is on.
        run retry-guesses
        run queue --limit 300
        run mirror --limit 60
        run stats
        ;;
    reprobe)
        # Re-probes what the other jobs could not and enriches a few
        # companies. Exits in a second when port 25 is unreachable (or, with
        # SMTP_PROBE_RELAY set, when the relay is down).
        run reprobe --limit 25 --enrich 5
        ;;
    health)
        # Gmail files the odd real reply as spam. Once a day, before the
        # window opens, so it stops that morning's follow-up.
        run inbox --spam --days 3 --limit 300
        volume inbox --spam --days 3 --limit 300
        run health
        volume health
        ;;
    bounce)
        run inbox --days 1 --limit 80
        run bounce-report
        volume inbox --days 1 --limit 80
        volume bounce-report
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
