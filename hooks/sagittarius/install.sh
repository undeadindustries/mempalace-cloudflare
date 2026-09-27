#!/bin/bash
# MEMPALACE SAGITTARIUS HOOK INSTALLER
#
# Optional helper. Copies the four Sagittarius hook scripts to a stable
# install location and merges entries into the Sagittarius `settings.json`
# — without clobbering unrelated hooks, MCP servers, providers, or any
# other settings already in that file.
#
# This is NEVER auto-invoked. Editor config is sacred; we do not modify a
# user's settings.json without explicit consent. The user runs this script
# (or wires the hooks manually) as a documented opt-in.
#
# === USAGE ===
#
#   hooks/sagittarius/install.sh [options]
#
# Options:
#   --scope user|project   Target scope. Default: user.
#                          - user:    merges into ~/.sagittarius/settings.json
#                          - project: merges into <target>/.sagittarius/settings.json
#   --target <path>        Project root for --scope project (default: $PWD).
#                          Ignored for --scope user.
#   --install-dir <path>   Where to copy the hook scripts.
#                          Default: ~/.sagittarius/hooks
#   --variant full|minimal Which hook set to wire.
#                          - full:    AfterAgent + SessionStart + PreCompress + SessionEnd
#                          - minimal: AfterAgent only
#                          Default: full.
#   --dry-run              Print the would-be JSON to stdout, do not write
#                          and do not copy scripts.
#   --uninstall            Remove MemPalace entries from the target
#                          settings.json (preserves unrelated hooks and all
#                          non-hooks settings).
#                          Does NOT delete the installed scripts.
#   -h, --help             Show this help and exit.
#
# === SAGITTARIUS SHAPE ===
#
# Hooks live under the top-level "hooks" object. Each event maps to a
# list of groups; each group has a "hooks" list of hook definitions:
#
#   {"hooks": {"AfterAgent": [{"hooks": [{
#       "type": "command",
#       "name": "mempalace-autosave",
#       "command": "$HOME/.sagittarius/hooks/mempal_save_hook_sagittarius.sh",
#       "timeout": 5
#   }]}]}}
#
# $HOME works because commands run through `sh -c`. Global hooks
# (~/.sagittarius/settings.json) are implicitly trusted; project hooks
# need `/hooks trust <name>` after install.
#
# === PORTABILITY ===
#
# Pure bash 3.2 + POSIX tools + python3 (which the hook scripts
# themselves already require). No `jq` dependency.
#
# Python helpers are materialised to temp files rather than piped via
# `$(... <<'PYEOF' ... PYEOF)` to dodge the bash 3.2.57 parser bug
# that trips on parens nested inside a heredoc body that lives inside
# a `$(...)` command substitution.

set -e

usage() {
    sed -n '2,58p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ── Defaults ──────────────────────────────────────────────────────
SCOPE="user"
TARGET=""
INSTALL_DIR="$HOME/.sagittarius/hooks"
VARIANT="full"
DRY_RUN=0
UNINSTALL=0

# ── Parse args ────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --scope)
            shift
            SCOPE="${1:-}"
            ;;
        --target)
            shift
            TARGET="${1:-}"
            ;;
        --install-dir)
            shift
            INSTALL_DIR="${1:-}"
            ;;
        --variant)
            shift
            VARIANT="${1:-}"
            ;;
        --dry-run) DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'install.sh: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 64
            ;;
    esac
    shift || true
done

case "$SCOPE" in
    user|project) ;;
    *)
        printf 'install.sh: --scope must be "user" or "project" (got "%s")\n' \
            "$SCOPE" >&2
        exit 64
        ;;
esac

case "$VARIANT" in
    full|minimal) ;;
    *)
        printf 'install.sh: --variant must be "full" or "minimal" (got "%s")\n' \
            "$VARIANT" >&2
        exit 64
        ;;
esac

# ── Resolve paths ─────────────────────────────────────────────────
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
SOURCE_DIR="$_script_dir"

# Resolve --install-dir to an absolute path before it gets baked into
# settings.json. Sagittarius invokes hook commands from the project
# directory, so a relative command path would silently fail to launch.
case "$INSTALL_DIR" in
    /*) ;;
    *) INSTALL_DIR="$PWD/$INSTALL_DIR" ;;
esac

if [ -n "${MEMPAL_PYTHON:-}" ] && [ -x "$MEMPAL_PYTHON" ]; then
    PYTHON_BIN="$MEMPAL_PYTHON"
else
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
    printf 'install.sh: python3 not found on PATH; cannot proceed.\n' >&2
    printf 'Set $MEMPAL_PYTHON to an interpreter path or install python3.\n' >&2
    exit 1
fi

# Determine the target settings.json path. Project scope needs
# `/hooks trust <name>` inside Sagittarius after install (project hooks
# are fingerprinted); user scope is implicitly trusted.
case "$SCOPE" in
    user)
        if [ -n "$TARGET" ]; then
            printf 'install.sh: --target is only meaningful with --scope project; ignoring.\n' >&2
        fi
        TARGET_DIR="$HOME/.sagittarius"
        ;;
    project)
        TARGET_DIR="${TARGET:-$PWD}/.sagittarius"
        ;;
esac
TARGET_FILE="$TARGET_DIR/settings.json"

# Commands point at the install location, NOT the source repo — once
# the user runs install.sh they can move / delete the cloned repo
# without breaking the wiring.
SAVE_CMD="$INSTALL_DIR/mempal_save_hook_sagittarius.sh"
DRAIN_CMD="$INSTALL_DIR/mempal_drain_hook_sagittarius.sh"
PRECOMPRESS_CMD="$INSTALL_DIR/mempal_precompress_hook_sagittarius.sh"
SESSIONEND_CMD="$INSTALL_DIR/mempal_session_end_hook_sagittarius.sh"

# ── Step 1: copy scripts (skipped on --dry-run / --uninstall) ─────
if [ "$UNINSTALL" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$INSTALL_DIR/lib"
    cp "$SOURCE_DIR/lib/common.sh" "$INSTALL_DIR/lib/common.sh"
    cp "$SOURCE_DIR/mempal_save_hook_sagittarius.sh"         "$INSTALL_DIR/"
    cp "$SOURCE_DIR/mempal_drain_hook_sagittarius.sh"        "$INSTALL_DIR/"
    cp "$SOURCE_DIR/mempal_precompress_hook_sagittarius.sh"  "$INSTALL_DIR/"
    cp "$SOURCE_DIR/mempal_session_end_hook_sagittarius.sh"  "$INSTALL_DIR/"
    chmod +x "$INSTALL_DIR/mempal_save_hook_sagittarius.sh" \
             "$INSTALL_DIR/mempal_drain_hook_sagittarius.sh" \
             "$INSTALL_DIR/mempal_precompress_hook_sagittarius.sh" \
             "$INSTALL_DIR/mempal_session_end_hook_sagittarius.sh"
    printf 'install.sh: copied scripts to %s\n' "$INSTALL_DIR" >&2
fi

# ── Short-circuit: uninstall with no existing file is a no-op ─────
if [ "$UNINSTALL" -eq 1 ] && [ ! -f "$TARGET_FILE" ]; then
    printf 'install.sh: nothing to uninstall (%s does not exist)\n' \
        "$TARGET_FILE" >&2
    exit 0
fi

# ── Step 2: merge / unmerge settings.json via python3 ─────────────
#
# Materialise the merge logic to a temp .py file (bash 3.2 rationale
# at the top of this script). The Python script is responsible for:
#   * tolerating a missing or empty settings.json (starts from {})
#   * preserving unrelated hooks AND all non-hooks settings
#     (mcpServers, providers, ui, ...) on install and uninstall
#   * recognising MemPalace entries by basename in the `command` field
#   * replacing (not duplicating) on re-install
#   * retiring the pre-1.0 script names (mempal-autosave.sh,
#     mempal-drain.sh) whose wiring this hook set supersedes

# mktemp portability: explicit absolute template (BSD vs GNU `-t`
# differ). Honour TMPDIR, fall back to /tmp.
MERGE_PY="$(mktemp "${TMPDIR:-/tmp}/mempal-sagittarius-merge.XXXXXX")"
trap 'rm -f "$MERGE_PY"' EXIT

cat > "$MERGE_PY" <<'PYEOF'
"""settings.json merge helper for hooks/sagittarius/install.sh.

Argv:
    sys.argv[1]: path to settings.json (may not exist)
    sys.argv[2]: variant ("full" or "minimal")
    sys.argv[3]: uninstall flag ("1" or "0")
    sys.argv[4]: save_cmd absolute path
    sys.argv[5]: drain_cmd absolute path
    sys.argv[6]: precompress_cmd absolute path
    sys.argv[7]: sessionend_cmd absolute path

Output: prints the merged JSON to stdout. Exits 2 on a malformed
existing config (refuses to overwrite a broken file).
"""
import json
import os
import sys

target_file = sys.argv[1]
variant = sys.argv[2]
uninstall = sys.argv[3] == "1"
save_cmd, drain_cmd, precompress_cmd, sessionend_cmd = sys.argv[4:8]

# Basenames this installer owns. The first four are the current hook
# set; the rest are retired predecessors whose wiring is superseded:
# mempal-autosave.sh / mempal-drain.sh (pre-1.0 scripts), and the
# upstream Claude Code scripts (mempal_precompact_hook.sh /
# mempal_session_end_hook.sh), which cannot work on Sagittarius
# payloads — the precompact parser expects Claude's PreCompact shape
# and the session-end wrapper passes --harness sagittarius, which
# hooks_cli rejects (SUPPORTED_HARNESSES is claude-code/codex/dsh).
# Entries are replaced on install and removed on uninstall; the files
# themselves are left alone.
MEMPAL_BASENAMES = (
    "mempal_save_hook_sagittarius.sh",
    "mempal_drain_hook_sagittarius.sh",
    "mempal_precompress_hook_sagittarius.sh",
    "mempal_session_end_hook_sagittarius.sh",
    "mempal-autosave.sh",
    "mempal-drain.sh",
    "mempal_precompact_hook.sh",
    "mempal_session_end_hook.sh",
)

if os.path.exists(target_file):
    with open(target_file, "r", encoding="utf-8") as fh:
        try:
            cfg = json.load(fh)
        except Exception as exc:
            sys.stderr.write(
                "install.sh: existing %s is not valid JSON: %s\n"
                "Refusing to overwrite. Fix the file and retry.\n"
                % (target_file, exc)
            )
            sys.exit(2)
else:
    cfg = {}

if not isinstance(cfg, dict):
    sys.stderr.write(
        "install.sh: %s top level must be a JSON object; got %s\n"
        % (target_file, type(cfg).__name__)
    )
    sys.exit(2)

cfg.setdefault("hooks", {})
if not isinstance(cfg["hooks"], dict):
    sys.stderr.write(
        "install.sh: %s 'hooks' must be a JSON object\n" % target_file
    )
    sys.exit(2)


def is_mempal_hook(defn):
    if not isinstance(defn, dict):
        return False
    cmd = defn.get("command", "")
    if not isinstance(cmd, str):
        return False
    return os.path.basename(cmd) in MEMPAL_BASENAMES


def clean_group(group):
    """Remove MemPalace defns from one {"hooks": [...]} group."""
    if not isinstance(group, dict):
        return group
    defns = group.get("hooks")
    if not isinstance(defns, list):
        return group
    group["hooks"] = [d for d in defns if not is_mempal_hook(d)]
    return group


def clean_event(entries):
    """Clean all groups of one event; drop groups left empty."""
    if not isinstance(entries, list):
        return entries
    kept = []
    for group in entries:
        group = clean_group(group)
        if isinstance(group, dict) and not group.get("hooks"):
            continue
        kept.append(group)
    return kept


def upsert(event, name, command, timeout, env=None):
    existing = cfg["hooks"].get(event, [])
    if not isinstance(existing, list):
        sys.stderr.write(
            "install.sh: %s hooks[%s] must be a list\n" % (target_file, event)
        )
        sys.exit(2)
    cleaned = [clean_group(g) for g in existing]
    cleaned = [g for g in cleaned if isinstance(g, dict) and g.get("hooks")]
    defn = {
        "type": "command",
        "name": name,
        "command": command,
        "timeout": timeout,
    }
    if env:
        defn["env"] = env
    if cleaned:
        cleaned[0].setdefault("hooks", []).append(defn)
    else:
        cleaned.append({"hooks": [defn]})
    cfg["hooks"][event] = cleaned


if uninstall:
    for event in list(cfg["hooks"].keys()):
        cfg["hooks"][event] = clean_event(cfg["hooks"][event])
        if not cfg["hooks"][event]:
            del cfg["hooks"][event]
else:
    upsert("AfterAgent", "mempalace-autosave", save_cmd, 5,
           {"MEMPAL_SAVE_INTERVAL": "15"})
    if variant == "full":
        upsert("SessionStart", "mempalace-drain", drain_cmd, 10)
        upsert("PreCompress", "mempalace-precompress", precompress_cmd, 60)
        upsert("SessionEnd", "mempalace-session-end", sessionend_cmd, 10)

print(json.dumps(cfg, indent=2, sort_keys=False))
PYEOF

NEW_JSON="$("$PYTHON_BIN" "$MERGE_PY" \
    "$TARGET_FILE" "$VARIANT" "$UNINSTALL" \
    "$SAVE_CMD" "$DRAIN_CMD" "$PRECOMPRESS_CMD" "$SESSIONEND_CMD")"

# ── Step 3: emit, write, or remove ────────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'install.sh: --dry-run; would write to %s\n' "$TARGET_FILE" >&2
    printf '%s\n' "$NEW_JSON"
    exit 0
fi

mkdir -p "$TARGET_DIR"

# Timestamped backup before overwriting an existing settings file —
# settings.json holds providers and MCP servers; never rewrite it
# without a rollback copy.
if [ -f "$TARGET_FILE" ]; then
    BACKUP="$TARGET_FILE.bak.$(date '+%Y-%m-%d-%H%M%S')"
    cp "$TARGET_FILE" "$BACKUP"
    printf 'install.sh: backed up %s to %s\n' "$TARGET_FILE" "$BACKUP" >&2
fi

TMP_FILE="${TARGET_FILE}.tmp.$$"
printf '%s\n' "$NEW_JSON" > "$TMP_FILE"
mv "$TMP_FILE" "$TARGET_FILE"

if [ "$UNINSTALL" -eq 1 ]; then
    printf 'install.sh: removed MemPalace entries from %s\n' "$TARGET_FILE" >&2
else
    printf 'install.sh: wrote %s\n' "$TARGET_FILE" >&2
    if [ "$SCOPE" = "project" ]; then
        printf 'install.sh: project hooks need trust: run /hooks trust-all inside Sagittarius\n' >&2
    else
        printf 'install.sh: restart Sagittarius (or run /hooks reload)\n' >&2
    fi
fi
