# shellcheck shell=bash
# MEMPALACE SAGITTARIUS HOOK — shared helpers
#
# Sourced by the four Sagittarius hooks (save / drain / precompress /
# session-end). Mirrors the conventions of the Cursor and Antigravity
# hook libs (hooks/cursor/lib/common.sh, hooks/antigravity/lib/common.sh)
# so a user who already debugs one knows how to debug the other:
#
#   * STATE_DIR layout under ~/.mempalace/hook_state/
#   * MEMPAL_PYTHON resolution order (override → $PATH → bare python3)
#   * MEMPALACE_HOOKS_AUTO_SAVE=false kill switch (config.json fallback)
#   * sentinel-guarded Python parser via `sed -n 'Np'` (bash 3.2 safe)
#   * fail-open on internal errors: emit `{}` and log, never break the
#     agent turn
#
# Sagittarius-specific notes (see hooks/sagittarius/STDIN_SHAPE.md):
#
#   * The hook payload is snake_case: session_id, transcript_path, cwd,
#     hook_event_name, timestamp, turn_index, plus prompt/prompt_response
#     (AfterAgent), source (SessionStart), trigger (PreCompress) and
#     reason (SessionEnd). Env vars SAGITTARIUS_CWD / SAGITTARIUS_PROJECT_DIR
#     are also set for every hook execution.
#   * turn_index counts genuine user turns and survives --resume, so the
#     save hook gates on `turn_index % INTERVAL` directly — no per-session
#     counter files needed (unlike Cursor/Antigravity).
#   * transcript_path points at the live session file
#     (~/.sagittarius/tmp/<project>/chats/session-*.jsonl). `mempalace mine`
#     accepts a single conversation file with --mode convos, so the hooks
#     mine that file — never the chats/ directory (which would re-mine
#     every past session on each save turn).
#
# This file is sourced, not executed, so it intentionally has no
# shebang. The `# shellcheck shell=bash` directive above tells
# shellcheck to treat it as bash when run standalone.

# ── State directory + log path ────────────────────────────────────────
#
# Honour MEMPAL_STATE_DIR while keeping the default identical to the
# other hooks so a user running several harnesses keeps a single state
# directory. The spool path reuses the pre-existing `unmined.spool`
# name so entries spooled by the retired mempal-autosave.sh still drain.
MEMPAL_STATE_DIR="${MEMPAL_STATE_DIR:-$HOME/.mempalace/hook_state}"
mkdir -p "$MEMPAL_STATE_DIR" 2>/dev/null
MEMPAL_SAGITTARIUS_LOG="$MEMPAL_STATE_DIR/sagittarius_hook.log"
MEMPAL_SAGITTARIUS_SPOOL="$MEMPAL_STATE_DIR/unmined.spool"

# ── Python interpreter resolution ─────────────────────────────────────
#
# Same contract as the other hooks:
#   1. $MEMPAL_PYTHON        — explicit user override (absolute path)
#   2. $(command -v python3) — first python3 on the hook's PATH
#   3. bare "python3"        — last-resort fallback
mempal_resolve_python() {
    local p="${MEMPAL_PYTHON:-}"
    if [ -n "$p" ] && [ -x "$p" ]; then
        printf '%s' "$p"
        return 0
    fi
    p="$(command -v python3 2>/dev/null || true)"
    if [ -n "$p" ]; then
        printf '%s' "$p"
        return 0
    fi
    printf '%s' "python3"
}
MEMPAL_PYTHON_BIN="$(mempal_resolve_python)"

# ── Logging ───────────────────────────────────────────────────────────
#
# Lines are `[ISO8601Z] [event=...] [session=...] message`, matching the
# Cursor hook log format so the files grep the same way.
mempal_log() {
    local event="${1:-?}"
    local session="${2:-unknown}"
    local msg="${3:-}"
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf '[%s] [event=%s] [session=%s] %s\n' "$ts" "$event" "$session" "$msg" \
        >> "$MEMPAL_SAGITTARIUS_LOG" 2>/dev/null
}

# ── Kill switch ───────────────────────────────────────────────────────
#
# Disabled if ANY of:
#   * MEMPAL_DISABLE_HOOK is a truthy string
#   * MEMPALACE_HOOKS_AUTO_SAVE is false/0/no/off (Claude Code convention)
#   * ~/.mempalace/config.json has hooks.auto_save == false
#
# Returns 0 (true in shell) when disabled, 1 when enabled.
mempal_is_disabled() {
    case "${MEMPAL_DISABLE_HOOK:-}" in
        1|true|yes|on) return 0 ;;
    esac
    case "${MEMPALACE_HOOKS_AUTO_SAVE:-}" in
        false|0|no|off) return 0 ;;
    esac
    local cfg="$HOME/.mempalace/config.json"
    if [ -f "$cfg" ]; then
        local result
        # python -c with the config path as argv[1]: a heredoc body with
        # parens inside $(...) trips the bash 3.2.57 parser bug (macOS
        # /bin/bash default). Same rationale as the Cursor hook lib.
        result="$("$MEMPAL_PYTHON_BIN" -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    print(str(cfg.get("hooks", {}).get("auto_save", True)).lower())
except Exception:
    print("true")
' "$cfg" 2>/dev/null)"
        if [ "$result" = "false" ]; then
            return 0
        fi
    fi
    return 1
}

# ── Save interval ─────────────────────────────────────────────────────
#
# Coerce empty, non-numeric, AND zero to the default: INTERVAL=0 would
# crash bash on the modulo check ($((TURN % 0)) is "division by 0").
mempal_save_interval() {
    local raw="${MEMPAL_SAVE_INTERVAL:-15}"
    case "$raw" in
        ''|*[!0-9]*|0) printf '15' ;;
        *) printf '%s' "$raw" ;;
    esac
}

# ── Stdin parser ──────────────────────────────────────────────────────
#
# Reads a Sagittarius hook payload from $1 and exports:
#   MEMPAL_SESSION   — session_id, falls back to "unknown"
#   MEMPAL_TURN      — integer turn_index (0 if absent / non-numeric)
#   MEMPAL_TRANSCRIPT — transcript_path, may be empty
#   MEMPAL_CWD       — cwd, falls back to SAGITTARIUS_CWD, then
#                      SAGITTARIUS_PROJECT_DIR, then $PWD
#   MEMPAL_EVENT     — hook_event_name, may be empty
#   MEMPAL_PARSE_OK  — "1" if parser ran cleanly, "0" otherwise
#
# Same sentinel + `sed -n 'Np'` extraction as the other hook libs for
# bash 3.2 compatibility. Each output line is pre-sanitised by the
# Python side to a shell-safe character set.
mempal_parse_stdin() {
    local input="${1:-}"
    local parsed
    # python -c (not a heredoc): a heredoc body would shadow Python's
    # stdin, leaving json.load(sys.stdin) reading nothing. Only
    # double-quoted Python strings inside the single-quoted bash string.
    parsed="$(
        umask 077
        printf '%s' "$input" | "$MEMPAL_PYTHON_BIN" -c '
import json, re, sys

def safe_str(value):
    return re.sub(r"[^a-zA-Z0-9_/.\-~]", "", str(value or ""))

def safe_path(value):
    return re.sub(r"[^a-zA-Z0-9_/.\-~ ]", "", str(value or ""))

def safe_int(value):
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "0"

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(data, dict):
    sys.exit(1)

print("__MEMPAL_PARSE_OK__")
print(safe_str(data.get("session_id") or ""))
print(safe_int(data.get("turn_index", 0)))
print(safe_path(data.get("transcript_path", "")))
print(safe_path(data.get("cwd", "")))
print(safe_str(data.get("hook_event_name", "")))
' 2>"$MEMPAL_STATE_DIR/sagittarius_last_python_err.log"
    )"

    if [ -s "$MEMPAL_STATE_DIR/sagittarius_last_python_err.log" ]; then
        chmod 600 "$MEMPAL_STATE_DIR/sagittarius_last_python_err.log" 2>/dev/null
    else
        rm -f "$MEMPAL_STATE_DIR/sagittarius_last_python_err.log" 2>/dev/null
    fi

    local marker
    marker="$(printf '%s\n' "$parsed" | sed -n '1p')"
    if [ "$marker" = "__MEMPAL_PARSE_OK__" ]; then
        MEMPAL_PARSE_OK="1"
        MEMPAL_SESSION="$(printf '%s\n' "$parsed" | sed -n '2p')"
        MEMPAL_TURN="$(printf '%s\n' "$parsed" | sed -n '3p')"
        MEMPAL_TRANSCRIPT="$(printf '%s\n' "$parsed" | sed -n '4p')"
        MEMPAL_CWD="$(printf '%s\n' "$parsed" | sed -n '5p')"
        MEMPAL_EVENT="$(printf '%s\n' "$parsed" | sed -n '6p')"
    else
        MEMPAL_PARSE_OK="0"
        MEMPAL_SESSION=""
        MEMPAL_TURN="0"
        MEMPAL_TRANSCRIPT=""
        MEMPAL_CWD=""
        MEMPAL_EVENT=""
    fi

    MEMPAL_SESSION="${MEMPAL_SESSION:-unknown}"
    case "$MEMPAL_TURN" in
        ''|*[!0-9]*) MEMPAL_TURN="0" ;;
    esac
    # Sagittarius sets SAGITTARIUS_CWD / SAGITTARIUS_PROJECT_DIR env vars
    # for every hook execution (docs/hooks.md), so a parse failure or an
    # empty cwd field still leaves a usable workspace for wing inference.
    if [ -z "$MEMPAL_CWD" ]; then
        if [ -n "${SAGITTARIUS_CWD:-}" ]; then
            MEMPAL_CWD="${SAGITTARIUS_CWD}"
        elif [ -n "${SAGITTARIUS_PROJECT_DIR:-}" ]; then
            MEMPAL_CWD="${SAGITTARIUS_PROJECT_DIR}"
        elif [ -n "${GEMINI_CWD:-}" ]; then
            MEMPAL_CWD="${GEMINI_CWD}"
        else
            MEMPAL_CWD="${PWD:-/}"
        fi
    fi

    case "$MEMPAL_TRANSCRIPT" in
        '~/'*) MEMPAL_TRANSCRIPT="$HOME/${MEMPAL_TRANSCRIPT#~/}" ;;
    esac
}

# ── Defense-in-depth: dump unparseable stdin ──────────────────────────
#
# Bounded to 4096 bytes, overwritten (never appended), 0600 perms: the
# dump mirrors the raw hook payload (transcript_path reveals the user's
# home + project layout).
mempal_dump_bad_input() {
    local input="${1:-}"
    if [ -z "$input" ]; then
        return 0
    fi
    mempal_log "${2:-?}" "${MEMPAL_SESSION:-unknown}" \
        "WARN: input parse failed (sentinel missing); see $MEMPAL_STATE_DIR/sagittarius_last_input.log + sagittarius_last_python_err.log"
    (
        umask 077
        printf '%s' "$input" | head -c 4096 > "$MEMPAL_STATE_DIR/sagittarius_last_input.log"
    )
    chmod 600 "$MEMPAL_STATE_DIR/sagittarius_last_input.log" 2>/dev/null
}

# ── Pending-mine marker ───────────────────────────────────────────────
#
# Guards against overlapping background mines for the same session: the
# save hook drops the marker before spawning and the mine subshell
# removes it on exit. Markers older than 1 hour are treated as stale
# (crashed mine) and reclaimed. Same shape as the Antigravity hook.
_mempal_pending_path() {
    local session="${1:-unknown}"
    local safe_session
    safe_session="$(printf '%s' "$session" | tr -cd 'a-zA-Z0-9_.-')"
    if [ -z "$safe_session" ]; then
        safe_session="unknown"
    fi
    printf '%s/sagittarius_pending_%s' "$MEMPAL_STATE_DIR" "$safe_session"
}

mempal_pending_active() {
    local session="${1:-unknown}"
    local path
    path="$(_mempal_pending_path "$session")"
    if [ ! -f "$path" ]; then
        return 1
    fi
    local mtime now
    if mtime=$("$MEMPAL_PYTHON_BIN" -c 'import os, sys; print(int(os.path.getmtime(sys.argv[1])))' "$path" 2>/dev/null) \
       && now=$(date '+%s' 2>/dev/null) \
       && [ -n "$mtime" ] \
       && [ "$((now - mtime))" -lt 3600 ]; then
        return 0
    fi
    mempal_log "save" "$session" "stale pending marker reclaimed"
    rm -f "$path" 2>/dev/null
    return 1
}

mempal_set_pending() {
    local session="${1:-unknown}"
    : > "$(_mempal_pending_path "$session")" 2>/dev/null || return 1
    chmod 600 "$(_mempal_pending_path "$session")" 2>/dev/null
}

mempal_clear_pending() {
    local session="${1:-unknown}"
    rm -f "$(_mempal_pending_path "$session")" 2>/dev/null
}

# ── Spool (failed-mine retry queue) ───────────────────────────────────
#
# One `transcript_path|wing` line per failed mine. The SessionStart hook
# drains it. Bare paths (no `|`, as written by the retired
# mempal-autosave.sh) still drain — they mine without --wing.
mempal_spool() {
    local transcript="${1:-}"
    local wing="${2:-}"
    [ -n "$transcript" ] || return 1
    printf '%s|%s\n' "$transcript" "$wing" >> "$MEMPAL_SAGITTARIUS_SPOOL" 2>/dev/null
    chmod 600 "$MEMPAL_SAGITTARIUS_SPOOL" 2>/dev/null
}

# ── Stale state GC ────────────────────────────────────────────────────
#
# Opportunistic daily-throttled sweep of sagittarius_pending_* markers.
# Globs are Sagittarius-specific and suffix-anchored, so Cursor /
# Antigravity / Claude state sharing the directory is never touched.
# (No counter files exist: turn gating is stateless via turn_index.)
# Fail-open: every step is best-effort.
mempal_gc_stale_state() {
    [ -d "$MEMPAL_STATE_DIR" ] || return 0

    local marker="$MEMPAL_STATE_DIR/sagittarius_last_sweep"
    if [ -f "$marker" ]; then
        local mtime now
        if mtime=$("$MEMPAL_PYTHON_BIN" -c 'import os, sys; print(int(os.path.getmtime(sys.argv[1])))' "$marker" 2>/dev/null) \
           && now=$(date '+%s' 2>/dev/null) \
           && [ -n "$mtime" ] \
           && [ "$((now - mtime))" -lt 86400 ]; then
            return 0
        fi
    fi
    : > "$marker" 2>/dev/null

    local ttl_raw="${MEMPAL_STATE_TTL_DAYS:-30}"
    local ttl="30"
    case "$ttl_raw" in
        ''|*[!0-9]*) ;;
        *) ttl="$ttl_raw" ;;
    esac

    find "$MEMPAL_STATE_DIR" -maxdepth 1 -type f \
        -name 'sagittarius_pending_*' -mtime +"$ttl" \
        -exec rm -f {} + 2>/dev/null
    return 0
}

# ── Workspace → wing inference ────────────────────────────────────────
#
# basename(cwd), matching config.normalize_wing_name: lowercase,
# anything outside [a-z0-9_] becomes an underscore, runs collapsed.
# Same contract as the Cursor hook's mempal_infer_wing; the default
# here is "sagittarius_session".
mempal_infer_wing() {
    local raw="${1:-}"
    if [ -z "$raw" ]; then
        printf 'sagittarius_session'
        return 0
    fi
    while [ "$raw" != "/" ] && [ "${raw%/}" != "$raw" ]; do
        raw="${raw%/}"
    done
    if [ "$raw" = "/" ]; then
        printf 'root'
        return 0
    fi
    local base="${raw##*/}"
    case "$base" in
        *\\*) base="${base##*\\}" ;;
    esac
    base="$(printf '%s' "$base" \
        | tr '[:upper:]' '[:lower:]' \
        | tr -c 'a-z0-9_' '_' \
        | tr -s '_' \
        | sed 's/^_//; s/_$//')"
    if [ -z "$base" ]; then
        printf 'sagittarius_session'
        return 0
    fi
    printf '%s' "$base"
}

# ── Transcript path validation ────────────────────────────────────────
#
# Mirrors the Claude Code hook's is_valid_transcript_path: non-empty,
# .json/.jsonl suffix, no .. traversal segments.
mempal_is_valid_transcript() {
    local path="${1:-}"
    [ -n "$path" ] || return 1
    case "$path" in
        *.json|*.jsonl) ;;
        *) return 1 ;;
    esac
    case "/$path/" in
        */../*) return 1 ;;
    esac
    return 0
}

# ── Mempalace runnability probe ───────────────────────────────────────
#
# A bare `import mempalace` (not `mempalace --version`: building the
# mine argument parser pays the full chromadb/onnx cold-start import).
# Callers decide foreground vs background: the save/drain hooks probe
# in the foreground so a broken install surfaces one visible
# systemMessage; the mine itself always runs backgrounded.
mempal_is_runnable() {
    "$MEMPAL_PYTHON_BIN" -c 'import mempalace' >/dev/null 2>&1
}

# ── Background mine ───────────────────────────────────────────────────
#
# Mines a single conversation FILE (never a directory) with
# --mode convos --wing, plus MEMPAL_DIR additively with --mode projects.
# Always backgrounded by the caller; this helper only builds the
# subshell body. On mine failure the transcript is spooled for the
# SessionStart drain hook.
mempal_mine_file_bg() {
    local transcript="${1:-}"
    local wing="${2:-sagittarius_session}"
    local session="${3:-unknown}"
    (
        "$MEMPAL_PYTHON_BIN" -m mempalace mine "$transcript" \
            --mode convos \
            --wing "$wing" \
            >> "$MEMPAL_SAGITTARIUS_LOG" 2>&1 < /dev/null \
        || {
            mempal_spool "$transcript" "$wing"
            mempal_log "save" "$session" \
                "mine failed for $transcript; spooled for SessionStart drain"
        }
        if [ -n "${MEMPAL_DIR:-}" ] && [ -d "$MEMPAL_DIR" ]; then
            "$MEMPAL_PYTHON_BIN" -m mempalace mine "$MEMPAL_DIR" \
                --mode projects \
                --wing "$wing" \
                >> "$MEMPAL_SAGITTARIUS_LOG" 2>&1 < /dev/null \
            || mempal_log "save" "$session" \
                "project mine failed for $MEMPAL_DIR"
        fi
        mempal_clear_pending "$session"
        mempal_log "save" "$session" "background mine finished wing=$wing"
    ) >/dev/null 2>&1 < /dev/null &
}

# ── JSON emit ─────────────────────────────────────────────────────────
#
# Final stdout write. `printf '%s'` instead of `echo` (echo interprets
# -n/-e/-E as flags; backslash handling varies). A non-JSON stdout is
# degraded by Sagittarius to a plain-text system message, so every path
# must emit valid JSON — '{}' for silence, or {"systemMessage": ...}
# for the one visible failure line.
mempal_emit() {
    printf '%s\n' "${1:-{\}}"
}
