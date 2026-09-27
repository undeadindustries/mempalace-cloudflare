#!/bin/bash
# MEMPALACE SAGITTARIUS SESSION-END HOOK — SessionEnd event handler
#
# Final save on clean exit. Sagittarius fires SessionEnd synchronously
# with a bounded 5s timeout (Runner.Close), so this hook does minimal
# foreground work — parse, validate, spawn — and returns `{}` in
# milliseconds. The orphaned background mine finishes the save after the
# session has exited; on failure it spools for next launch's drain hook.
#
# This REPLACES the previous wiring to
# $HOME/src/mempalace/hooks/mempal_session_end_hook.sh with
# MEMPALACE_HOOK_HARNESS=sagittarius, which never worked: hooks_cli.py
# only accepts claude-code/codex/dsh harnesses and rejects sagittarius.
# Mining the transcript file directly needs no harness support at all.
#
# === STDIN === {reason: exit, + session_id/transcript_path/cwd/...}
# === STDOUT === always {}. Exit code always 0: never fail session exit.
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

# Capture stdin BEFORE backgrounding — the parent's stdin is gone once
# we return. Same rationale as the Claude Code session-end hook.
INPUT="$(cat)"
mempal_parse_stdin "$INPUT"

if [ "$MEMPAL_PARSE_OK" != "1" ]; then
    mempal_dump_bad_input "$INPUT" "SessionEnd"
    mempal_emit '{}'
    exit 0
fi

if ! mempal_is_valid_transcript "$MEMPAL_TRANSCRIPT" \
    || [ ! -f "$MEMPAL_TRANSCRIPT" ]; then
    mempal_log "SessionEnd" "$MEMPAL_SESSION" \
        "no readable transcript ($MEMPAL_TRANSCRIPT); nothing to mine"
    mempal_emit '{}'
    exit 0
fi

WING="$(mempal_infer_wing "$MEMPAL_CWD")"
mempal_log "SessionEnd" "$MEMPAL_SESSION" \
    "spawning final background mine wing=$WING"

# Entire probe + mine inside the backgrounded subshell: the 5s SessionEnd
# budget must not be spent on a cold `import mempalace`, and stdout is
# useless here (the session is exiting). The orphan survives Runner.Close;
# only a machine shutdown mid-mine loses it — and the next SessionStart
# drain cannot know, so spool optimistically ONLY on mine failure.
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
            || mempal_log "SessionEnd" "$MEMPAL_SESSION" \
                "project mine failed for $MEMPAL_DIR"
        fi
        mempal_log "SessionEnd" "$MEMPAL_SESSION" "final mine finished"
    else
        mempal_spool "$MEMPAL_TRANSCRIPT" "$WING"
        mempal_log "SessionEnd" "$MEMPAL_SESSION" \
            "mempalace not runnable; spooled $MEMPAL_TRANSCRIPT"
    fi
) >/dev/null 2>&1 < /dev/null &
disown 2>/dev/null || true

# Return immediately so the harness never blocks on session exit.
mempal_emit '{}'
exit 0
