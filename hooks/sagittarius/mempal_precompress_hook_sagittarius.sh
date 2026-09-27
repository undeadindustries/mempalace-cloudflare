#!/bin/bash
# MEMPALACE SAGITTARIUS PRECOMPRESS HOOK — PreCompress event handler
#
# Advisory-only by harness design: Sagittarius fires PreCompress on a
# background goroutine, waits for nothing, and discards results — the
# hook cannot rewrite history. Session JSONL is append-only and
# compression never touches it, so a mine started just before
# compression reads the same bytes after. All we do is kick off a
# background mine of the live session file and return `{}` immediately.
#
# Maps to the Claude Code PreCompact hook (hooks/mempal_precompact_hook.sh).
#
# === STDIN === {trigger: auto|manual, + session_id/transcript_path/cwd/...}
# === STDOUT === always {}. Exit code always 0.
#
# `set -e` is intentionally NOT enabled.

_mempal_self="${BASH_SOURCE[0]:-$0}"
_mempal_dir="$(cd "$(dirname "$_mempal_self")" 2>/dev/null && pwd)"
# shellcheck source=lib/common.sh
. "$_mempal_dir/lib/common.sh"

MEMPAL_DIR="${MEMPAL_DIR:-}"

if mempal_is_disabled; then
    mempal_emit '{}'
    exit 0
fi

INPUT="$(cat)"
mempal_parse_stdin "$INPUT"

if [ "$MEMPAL_PARSE_OK" != "1" ]; then
    mempal_dump_bad_input "$INPUT" "PreCompress"
    mempal_emit '{}'
    exit 0
fi

if ! mempal_is_valid_transcript "$MEMPAL_TRANSCRIPT" \
    || [ ! -f "$MEMPAL_TRANSCRIPT" ]; then
    mempal_log "PreCompress" "$MEMPAL_SESSION" \
        "no readable transcript ($MEMPAL_TRANSCRIPT); nothing to mine"
    mempal_emit '{}'
    exit 0
fi

WING="$(mempal_infer_wing "$MEMPAL_CWD")"
mempal_log "PreCompress" "$MEMPAL_SESSION" \
    "spawning background mine wing=$WING"

# The whole probe + mine runs inside the backgrounded subshell (see
# common.sh): PreCompress output is discarded by the harness, so there
# is no foreground failure line to emit — a failure just spools for the
# SessionStart drain hook. Foreground returns in milliseconds.
(
    if mempal_is_runnable; then
        "$MEMPAL_PYTHON_BIN" -m mempalace mine "$MEMPAL_TRANSCRIPT" \
            --mode convos \
            --wing "$WING" \
            >> "$MEMPAL_SAGITTARIUS_LOG" 2>&1 < /dev/null \
        || mempal_spool "$MEMPAL_TRANSCRIPT" "$WING"
        if [ -n "$MEMPAL_DIR" ] && [ -d "$MEMPAL_DIR" ]; then
            "$MEMPAL_PYTHON_BIN" -m mempalace mine "$MEMPAL_DIR" \
                --mode projects \
                --wing "$WING" \
                >> "$MEMPAL_SAGITTARIUS_LOG" 2>&1 < /dev/null \
            || mempal_log "PreCompress" "$MEMPAL_SESSION" \
                "project mine failed for $MEMPAL_DIR"
        fi
        mempal_log "PreCompress" "$MEMPAL_SESSION" "background mine finished"
    else
        mempal_spool "$MEMPAL_TRANSCRIPT" "$WING"
        mempal_log "PreCompress" "$MEMPAL_SESSION" \
            "mempalace not runnable; spooled $MEMPAL_TRANSCRIPT"
    fi
) >/dev/null 2>&1 < /dev/null &
disown 2>/dev/null || true

mempal_emit '{}'
exit 0
