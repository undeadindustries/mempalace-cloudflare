#!/bin/bash
# MEMPALACE SAGITTARIUS DRAIN HOOK — SessionStart event handler
#
# Recovery half of the save hook. Session JSONL is append-only and never
# discarded, so a mine that failed while MemPalace was broken loses
# nothing permanently — it just needs re-running. Draining at session
# start turns "MemPalace was broken yesterday" into a background catch-up
# on next launch instead of a manual repair.
#
# Spool lines are `transcript_path|wing` (bare paths from the retired
# mempal-autosave.sh still drain — they mine without --wing).
#
# === STDIN === {source: startup|resume|clear, + session_id/cwd/...}
# === STDOUT === {} normally; one systemMessage line while catching up
# or when MemPalace is still broken. Exit code always 0: never fail startup.
#
# `set -e` is intentionally NOT enabled.

_mempal_self="${BASH_SOURCE[0]:-$0}"
_mempal_dir="$(cd "$(dirname "$_mempal_self")" 2>/dev/null && pwd)"
# shellcheck source=lib/common.sh
. "$_mempal_dir/lib/common.sh"

# Drain stdin so the writer never blocks (we ignore the payload —
# the spool is the entire work queue).
cat >/dev/null

# A disabled hook drains nothing: mining now would defeat the kill switch.
if mempal_is_disabled; then
    mempal_emit '{}'
    exit 0
fi

mempal_gc_stale_state

if [ ! -s "$MEMPAL_SAGITTARIUS_SPOOL" ]; then
    mempal_emit '{}'
    exit 0
fi

# Still broken: leave the spool intact and say so, rather than clearing it.
if ! mempal_is_runnable; then
    pending="$(wc -l < "$MEMPAL_SAGITTARIUS_SPOOL" 2>/dev/null | tr -d ' ')"
    mempal_log "SessionStart" "unknown" \
        "MemPalace still unavailable; $pending transcript(s) pending"
    mempal_emit "{\"systemMessage\":\"MemPalace still unavailable - ${pending:-?} transcript(s) pending.\"}"
    exit 0
fi

# Claim the spool by moving it, so a concurrent session does not mine
# the same entries twice and a failure during this drain can re-spool
# cleanly into a fresh file.
claim="$MEMPAL_SAGITTARIUS_SPOOL.$$"
mv "$MEMPAL_SAGITTARIUS_SPOOL" "$claim" 2>/dev/null || {
    mempal_emit '{}'
    exit 0
}

pending="$(wc -l < "$claim" 2>/dev/null | tr -d ' ')"
mempal_log "SessionStart" "unknown" "draining $pending spooled transcript(s)"

(
    # Deduplicate: several failed turns in one session spool the same file.
    sort -u "$claim" | while IFS= read -r line; do
        transcript="${line%%|*}"
        wing="${line#*|}"
        [ -f "$transcript" ] || continue
        if ! mempal_is_valid_transcript "$transcript"; then
            continue
        fi
        if [ -n "$wing" ] && [ "$wing" != "$line" ]; then
            "$MEMPAL_PYTHON_BIN" -m mempalace mine "$transcript" \
                --mode convos --wing "$wing" \
                >> "$MEMPAL_SAGITTARIUS_LOG" 2>&1 < /dev/null \
            || mempal_spool "$transcript" "$wing"
        else
            "$MEMPAL_PYTHON_BIN" -m mempalace mine "$transcript" \
                --mode convos \
                >> "$MEMPAL_SAGITTARIUS_LOG" 2>&1 < /dev/null \
            || mempal_spool "$transcript" ""
        fi
    done
    rm -f "$claim"
    mempal_log "SessionStart" "unknown" "drain complete"
) >/dev/null 2>&1 < /dev/null &
disown 2>/dev/null || true

mempal_emit "{\"systemMessage\":\"MemPalace: catching up on ${pending:-?} pending transcript(s) in the background.\"}"
exit 0
