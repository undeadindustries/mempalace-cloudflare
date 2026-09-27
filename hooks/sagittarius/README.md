# MemPalace hooks for Sagittarius

Background memory capture for [Sagittarius](https://github.com/undeadindustries/sagittarius)
(the Go terminal agent). Every 15th user turn, the live session file is mined
into MemPalace — silently, in the background, with zero chat-window noise.
Mirrors the Claude Code (`hooks/mempal_save_hook.sh`), Cursor
(`hooks/cursor/`), and Antigravity (`hooks/antigravity/`) integrations.

## What gets installed

| Sagittarius event | Script | Behaviour |
|---|---|---|
| `AfterAgent` | `mempal_save_hook_sagittarius.sh` | Every 15th turn (`MEMPAL_SAVE_INTERVAL`), background-mine the live session file with `--mode convos --wing <cwd basename>`. Emits `{}`; one `systemMessage` line only if MemPalace itself is unrunnable. |
| `SessionStart` | `mempal_drain_hook_sagittarius.sh` | Re-mines transcripts spooled by failed saves (background catch-up). |
| `PreCompress` | `mempal_precompress_hook_sagittarius.sh` | Advisory background mine before history compression (harness waits for nothing). |
| `SessionEnd` | `mempal_session_end_hook_sagittarius.sh` | Final background mine on clean exit (5s harness budget — returns in milliseconds). |

All four exit `0` on every path: a memory hook must never break a turn,
fail startup, or block session exit.

## Install

```bash
hooks/sagittarius/install.sh --scope user        # ~/.sagittarius/settings.json
hooks/sagittarius/install.sh --scope project --target /path/to/repo
hooks/sagittarius/install.sh --variant minimal   # AfterAgent only
hooks/sagittarius/install.sh --dry-run           # preview, change nothing
hooks/sagittarius/install.sh --uninstall         # remove our entries only
```

The installer copies the scripts to `~/.sagittarius/hooks/`, backs up
`settings.json` (`settings.json.bak.<timestamp>`), and merges the four
events without touching unrelated hooks, MCP servers, or providers.
User-scope hooks are implicitly trusted; project-scope hooks need
`/hooks trust-all` inside Sagittarius afterwards. Reload with
`/hooks reload` (or restart Sagittarius).

If MemPalace lives in a venv / pipx / uv tool (not on the hook's PATH),
export `MEMPAL_PYTHON` to its interpreter — the hooks and the installer
honour the same variable.

## How it works

Sagittarius session JSONL (`~/.sagittarius/tmp/<project>/chats/session-*.jsonl`)
uses top-level `{"type": "user"}` / `{"type": "gemini"}` records, so the
Claude Code transcript counter never fires there. Sagittarius instead
supplies `turn_index` (genuine user turns, tool results excluded,
surviving `--resume`) in every hook payload — the save hook gates on
`turn_index % 15` directly, with no per-session counter files.

`mempalace mine <transcript file> --mode convos` accepts a single
conversation file, so each save mines the live session file only — never
the `chats/` directory (which would re-mine every past session). Verbatim
fidelity comes from the `_try_sagittarius_jsonl` parser in
`mempalace/normalize.py`: only `text` blocks become drawers;
`functionCall` invocations and `functionResponse` tool results are
dropped, and `{"$set": ...}` metadata updates are skipped.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MEMPAL_SAVE_INTERVAL` | `15` | Save every Nth user turn |
| `MEMPAL_DIR` | _(unset)_ | Extra project dir mined additively with `--mode projects` |
| `MEMPAL_PYTHON` | `python3` on PATH | Interpreter that can `import mempalace` |
| `MEMPAL_STATE_DIR` | `~/.mempalace/hook_state` | State dir (pending markers, spool, logs) |
| `MEMPAL_STATE_TTL_DAYS` | `30` | Pending-marker retention for the daily GC sweep |
| `MEMPAL_DISABLE_HOOK=1`, `MEMPALACE_HOOKS_AUTO_SAVE=false`, or `~/.mempalace/config.json` `hooks.auto_save: false` | | Kill switches — hook emits `{}` and touches nothing |

## Failure behaviour (matches the Sagittarius hooks doc)

- **Never block.** Every path exits `0`.
- **Be silent on success, visible on failure.** The only TUI-visible line
  is the one telling you capture has paused.
- **Spool instead of retrying in place.** Failed mines append
  `transcript|wing` to `~/.mempalace/hook_state/unmined.spool`; the next
  `SessionStart` re-mines them in the background.

## Debugging

- `~/.mempalace/hook_state/sagittarius_hook.log` — timestamped mine/skip lines
- `/hooks list`, `/hooks test mempalace-autosave` — inside Sagittarius
- `~/.sagittarius/logs/sagittarius.log` — hook execution failures (e.g. trust skips)
