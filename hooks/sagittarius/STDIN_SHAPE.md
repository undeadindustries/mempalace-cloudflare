# Sagittarius hook stdin shape

Verified against the Sagittarius source (`internal/hooks/types.go`
`HookInput`, `internal/agent/hooks.go` `FireHookEvent`, `docs/hooks.md`).

## Common fields (every event)

```json
{
  "session_id": "sagittarius-11974",
  "transcript_path": "/Users/robs/.sagittarius/tmp/aes-demo1/chats/session-2026-08-16T05-48-cb4710e8.jsonl",
  "cwd": "/Users/robs/src/aes-demo1",
  "hook_event_name": "AfterAgent",
  "timestamp": "2026-09-27T01:00:00Z",
  "turn_index": 15
}
```

- All keys are **snake_case** (`session_id`, `transcript_path`, `turn_index`).
- `transcript_path` is the live session file (`Runner.SessionFilePath()`),
  empty string when no session is recording.
- `turn_index` counts genuine user turns (tool results recorded with the
  user role are excluded) and continues across `--resume` / `/chat resume`.
- Env vars set for every hook process: `SAGITTARIUS_PROJECT_DIR`,
  `SAGITTARIUS_CWD`, `SAGITTARIUS_SESSION_ID` (plus `GEMINI_*` aliases).

## Event-specific fields

| Event | Extra fields |
|---|---|
| `AfterAgent` | `prompt`, `prompt_response` (this turn only, not the session) |
| `FirstTurn` | `prompt`, `prompt_response` (fires once per session) |
| `SessionStart` | `source`: `startup` \| `resume` \| `clear` |
| `SessionEnd` | `reason`: `exit` (synchronous, 5s timeout in `Runner.Close`) |
| `PreCompress` | `trigger`: `auto` \| `manual` (fire-and-forget; results discarded) |
| `BeforeAgent` | `prompt` (can block / append `additionalContext`) |
| `BeforeTool` | `tool_name`, `tool_input` (can block / merge `tool_input`) |
| `AfterTool` | `tool_name`, `tool_input`, `tool_response` |

## Stdout contract

- Parsed as JSON `HookOutput`. `{}` = silence (what the MemPalace hooks
  emit on every success path).
- `{"systemMessage": "..."}` surfaces one line in the TUI (used only for
  the "MemPalace unavailable / catching up" failure lines).
- Non-JSON stdout degrades to a plain-text system message — every hook
  path must therefore print valid JSON.
- Exit `2` hard-blocks execution (its `stderr` becomes the denial
  reason); the MemPalace hooks never use it and always exit `0`.
- Each hook command runs in its own process group (`Setpgid`); on hook
  **timeout/cancel** the runner SIGKILLs the group. A hook that returns
  promptly is never killed, so backgrounded mines (`& disown`) survive —
  but keep foreground work in the low milliseconds on `AfterAgent`
  (5s configured timeout) and `SessionEnd` (5s harness timeout).
