#!/bin/bash
# MEMPALACE SAGITTARIUS SAVE HOOK — AfterAgent event handler
#
# Sagittarius fires AfterAgent each time the agent's turn ends. Every
# SAVE_INTERVAL-th user turn we background-mine the live session file
# into the user's MemPalace and return immediately. Silent by default —
# stdout is `{}` on every success path so nothing lands in the TUI.
#
# Why turn_index instead of counting the transcript (the Claude Code
# hook's approach): Sagittarius session JSONL uses top-level
# {"type": "user"} records, so `hook_shell count-human-messages` always
# returns 0. Sagittarius supplies turn_index directly — genuine user
# turns, tool results excluded, surviving --resume — so no per-session
# counter files are needed at all.
#
# Why a single file instead of its directory: `mempalace mine` accepts
# one conversation file with --mode convos. Mining the chats/ directory
# would re-mine every past session on each save turn; the single live
# file keeps each save proportional to the current session.
#
# === STDIN (snake_case; see STDIN_SHAPE.md) ===
# {
#   "session_id": "<id>",
#   "transcript_path": "/abs/path/chats/session-....jsonl",
#   "cwd": "/abs/project/path",
#   "hook_event_name": "AfterAgent",
#   "timestamp": "2026-09-27T01:00:00Z",
#   "turn_index": 15,
#   "prompt": "<this turn's prompt>",
#   "prompt_response": "<this turn's reply>"
# }
#
# === STDOUT ===
# {} on success. {"systemMessage": "..."} only when MemPalace itself is
# unrunnable (capture paused, transcript spooled for the SessionStart
# drain hook). Exit code is always 0: a memory hook must never break a turn.
#
# `set -e` is intentionally NOT enabled — a broken hook must not block
# the user's conversation.

# ── Locate this script + source common helpers ───────────────────────
_mempal_self="${BASH_SOURCE[0]:-$0}"
_mempal_dir="$(cd "$(dirname "$_mempal_self")" 2>/dev/null && pwd)"
# shellcheck source=lib/common.sh
. "$_mempal_dir/lib/common.sh"

SAVE_INTERVAL="$(mempal_save_interval)"

# Optional additional project directory to mine on save (parity with
# the Claude Code hook's MEMPAL_DIR knob — purely additive, never an
# override for the transcript mine).
MEMPAL_DIR="${MEMPAL_DIR:-}"

# ── Kill switch — emit `{}` so the turn proceeds untouched ────────────
if mempal_is_disabled; then
    mempal_emit '{}'
    exit 0
fi

# Opportunistic, daily-throttled GC of stale pending markers. After the
# kill switch so a disabled hook touches nothing.
mempal_gc_stale_state

INPUT="$(cat)"
mempal_parse_stdin "$INPUT"

if [ "$MEMPAL_PARSE_OK" != "1" ]; then
    mempal_dump_bad_input "$INPUT" "AfterAgent"
    # Fail-open: don't break the turn on a parse error.
    mempal_emit '{}'
    exit 0
fi

# ── Turn gate (stateless: turn_index IS the counter) ──────────────────
#
# turn_index counts genuine user turns and continues across --resume,
# so `turn % interval == 0` fires every Nth turn with no state files.
# INTERVAL is floored to >= 1 by mempal_save_interval: the modulo can
# never divide by zero even with MEMPAL_SAVE_INTERVAL=0.
if [ "$MEMPAL_TURN" -le 0 ] || [ "$((MEMPAL_TURN % SAVE_INTERVAL))" -ne 0 ]; then
    mempal_emit '{}'
    exit 0
fi

mempal_log "AfterAgent" "$MEMPAL_SESSION" \
    "turn=$MEMPAL_TURN interval=$SAVE_INTERVAL cwd=$MEMPAL_CWD"

# ── Validate transcript ───────────────────────────────────────────────
if ! mempal_is_valid_transcript "$MEMPAL_TRANSCRIPT" \
    || [ ! -f "$MEMPAL_TRANSCRIPT" ]; then
    mempal_log "AfterAgent" "$MEMPAL_SESSION" \
        "save turn but no readable transcript ($MEMPAL_TRANSCRIPT); nothing to mine"
    mempal_emit '{}'
    exit 0
fi

# ── Pending-mine guard ────────────────────────────────────────────────
#
# AfterAgent turns are sequential, but a slow mine can still overlap the
# next save turn. Skip while the previous mine is in flight; markers
# older than 1 hour are reclaimed as stale (crashed mine).
if mempal_pending_active "$MEMPAL_SESSION"; then
    mempal_log "AfterAgent" "$MEMPAL_SESSION" \
        "previous mine still in flight; skipping turn $MEMPAL_TURN"
    mempal_emit '{}'
    exit 0
fi

WING="$(mempal_infer_wing "$MEMPAL_CWD")"

# ── Runnability probe (foreground, cheap) ─────────────────────────────
#
# A bare `import mempalace` — NOT `mempalace --version`, which pays the
# full mine-parser cold-start import. On failure the transcript is
# spooled for the SessionStart drain hook and ONE systemMessage line
# surfaces in the TUI (the "visible on failure" convention from the
# Sagittarius hooks doc: a week of silent failed saves is the real hazard).
if ! mempal_is_runnable; then
    mempal_spool "$MEMPAL_TRANSCRIPT" "$WING"
    mempal_log "AfterAgent" "$MEMPAL_SESSION" \
        "mempalace not importable via $MEMPAL_PYTHON_BIN; spooled $MEMPAL_TRANSCRIPT"
    mempal_emit '{"systemMessage":"MemPalace unavailable (cannot import mempalace) - memory capture paused, transcript spooled for retry."}'
    exit 0
fi

# ── Trigger save ──────────────────────────────────────────────────────
mempal_log "AfterAgent" "$MEMPAL_SESSION" \
    "TRIGGERING SAVE turn=$MEMPAL_TURN wing=$WING transcript=$MEMPAL_TRANSCRIPT"

# Drop the pending marker BEFORE spawning so a near-simultaneous fire
# sees it. The mine subshell clears it on exit (see common.sh).
mempal_set_pending "$MEMPAL_SESSION"
mempal_mine_file_bg "$MEMPAL_TRANSCRIPT" "$WING" "$MEMPAL_SESSION"

# ── Always emit `{}` ──────────────────────────────────────────────────
#
# Never a decision/block: AfterAgent is observational. The background
# mine is the verbatim-capture path (normalize.py parses Sagittarius
# JSONL via _try_sagittarius_jsonl); no diary nudge is needed.
mempal_emit '{}'
exit 0
