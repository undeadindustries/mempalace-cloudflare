"""End-to-end shell tests for the Sagittarius hook scripts.

Invokes the bash scripts directly via subprocess with synthetic stdin
JSON and asserts on their stdout / exit code / state-dir side effects.

Scripts under test (hooks/sagittarius/):

* `mempal_save_hook_sagittarius.sh`        — AfterAgent event
* `mempal_drain_hook_sagittarius.sh`       — SessionStart event
* `mempal_precompress_hook_sagittarius.sh` — PreCompress event
* `mempal_session_end_hook_sagittarius.sh` — SessionEnd event

Test isolation:

* Each test runs in its own temp dir.
* `MEMPAL_STATE_DIR` and `HOME` point at the temp dir, so no test ever
  touches the real `~/.mempalace/hook_state/`.
* `MEMPAL_PYTHON` points at `/usr/bin/python3` with the subprocess cwd
  set to the temp dir, so `import mempalace` deterministically fails
  (for `python -c`, `sys.path[0]` is the cwd) — exercising the
  spool/systemMessage unhappy paths without a real install.
* Happy-path mine tests use a stub interpreter (a bash script) that
  answers the `import mempalace` probe with exit 0 and records
  `mempalace mine` invocations instead of running them.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks" / "sagittarius"
SAVE_HOOK = HOOKS_DIR / "mempal_save_hook_sagittarius.sh"
DRAIN_HOOK = HOOKS_DIR / "mempal_drain_hook_sagittarius.sh"
PRECOMPRESS_HOOK = HOOKS_DIR / "mempal_precompress_hook_sagittarius.sh"
SESSIONEND_HOOK = HOOKS_DIR / "mempal_session_end_hook_sagittarius.sh"
COMMON_LIB = HOOKS_DIR / "lib" / "common.sh"

# Skip the entire module on Windows — bash is required.
pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="Sagittarius shell hooks require bash; Windows uses a separate code path.",
)


def _payload(**overrides):
    base = {
        "session_id": "sagittarius-test-1",
        "transcript_path": "/tmp/sag-test-session.jsonl",
        "cwd": "/Users/robs/src/neo_geo_learn",
        "hook_event_name": "AfterAgent",
        "timestamp": "2026-09-27T01:00:00Z",
        "turn_index": 15,
    }
    base.update(overrides)
    return base


def _run_hook(
    script: Path,
    stdin_json: dict | str,
    state_dir: Path,
    home: Path,
    extra_env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess:
    """Run a hook script with isolated env and synthetic stdin."""
    if isinstance(stdin_json, dict):
        stdin = json.dumps(stdin_json)
    else:
        stdin = stdin_json
    env = os.environ.copy()
    # Hermetic env: HOME, state dir, and interpreter point at the test
    # temp. Assign unconditionally (not setdefault): the ambient
    # environment may carry MEMPAL_PYTHON from the developer's shell,
    # and hermetic isolation must win.
    home.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["MEMPAL_STATE_DIR"] = str(state_dir)
    # Deterministically unrunnable mempalace: /usr/bin/python3 cannot
    # import it when the subprocess cwd is the temp dir.
    env["MEMPAL_PYTHON"] = "/usr/bin/python3"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(home),
        timeout=timeout,
    )


def _write_stub_python(
    path: Path,
    log: Path,
    *,
    session: str = "sagittarius-test-1",
    turn: str = "15",
    transcript: str = "",
    cwd: str = "/Users/robs/src/neo_geo_learn",
    event: str = "AfterAgent",
) -> Path:
    """Write a stub MEMPAL_PYTHON: probes pass, mines are recorded.

    The stub answers the common.sh stdin parser (`-c` programs
    containing the __MEMPAL_PARSE_OK__ sentinel) with canned fields so
    hook gating/wing inference run deterministically, and records
    `mempalace mine` invocations instead of running them.
    """
    stub = path / "stub-python.sh"
    stub.write_text(
        "#!/bin/bash\n"
        f'STUB_LOG="{log}"\n'
        f'STUB_SESSION="{session}"\n'
        f'STUB_TURN="{turn}"\n'
        f'STUB_TRANSCRIPT="{transcript}"\n'
        f'STUB_CWD="{cwd}"\n'
        f'STUB_EVENT="{event}"\n'
        'if [ "$1" = "-c" ]; then\n'
        '  case "$2" in\n'
        "    *__MEMPAL_PARSE_OK__*)\n"
        "      printf '__MEMPAL_PARSE_OK__\\n%s\\n%s\\n%s\\n%s\\n%s\\n'"
        ' "$STUB_SESSION" "$STUB_TURN" "$STUB_TRANSCRIPT" "$STUB_CWD" "$STUB_EVENT"\n'
        "      ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf \'STUB_MINE %s\\n\' "$*" >> "$STUB_LOG"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub


def _wait_for(path: Path, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return path.exists()


# ── AfterAgent gating ─────────────────────────────────────────────────


def test_non_save_turn_is_silent(tmp_path: Path) -> None:
    """turn_index not on an interval boundary → '{}', no spool."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    proc = _run_hook(SAVE_HOOK, _payload(turn_index=14), state, home)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
    assert not (state / "unmined.spool").exists()


def test_turn_zero_is_silent(tmp_path: Path) -> None:
    proc = _run_hook(SAVE_HOOK, _payload(turn_index=0), tmp_path / "s", tmp_path / "h")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"


def test_save_turn_without_transcript_is_silent(tmp_path: Path) -> None:
    """Save turn but transcript missing → '{}' plus a log line, no spool."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    proc = _run_hook(
        SAVE_HOOK,
        _payload(turn_index=15, transcript_path="/tmp/does-not-exist-xyz.jsonl"),
        state,
        home,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
    assert not (state / "unmined.spool").exists()
    log = (state / "sagittarius_hook.log").read_text()
    assert "nothing to mine" in log


def test_save_turn_with_unrunnable_mempalace_spools_and_warns(
    tmp_path: Path,
) -> None:
    """Broken install → transcript spooled, one systemMessage line."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"sessionId": "s", "kind": "main"}\n')
    proc = _run_hook(
        SAVE_HOOK,
        _payload(turn_index=30, transcript_path=str(transcript)),
        state,
        home,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout.strip())
    assert "systemMessage" in out
    assert "MemPalace unavailable" in out["systemMessage"]
    spool = (state / "unmined.spool").read_text()
    assert str(transcript) in spool
    assert "neo_geo_learn" in spool  # wing recorded alongside the path


def test_save_turn_happy_path_mines_file_with_wing(tmp_path: Path) -> None:
    """Stub interpreter: mine invoked on the FILE with --wing, no spool."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"sessionId": "s", "kind": "main"}\n')
    stub_log = tmp_path / "stub.log"
    stub = _write_stub_python(tmp_path, stub_log, transcript=str(transcript))
    proc = _run_hook(
        SAVE_HOOK,
        _payload(turn_index=15, transcript_path=str(transcript)),
        state,
        home,
        extra_env={"MEMPAL_PYTHON": str(stub)},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
    assert _wait_for(stub_log)
    time.sleep(0.5)  # let the background subshell finish writing
    mine_calls = stub_log.read_text()
    assert str(transcript) in mine_calls
    assert "--wing" in mine_calls and "neo_geo_learn" in mine_calls
    assert "--mode" in mine_calls and "convos" in mine_calls
    assert not (state / "unmined.spool").exists()


def test_kill_switch_short_circuits(tmp_path: Path) -> None:
    proc = _run_hook(
        SAVE_HOOK,
        _payload(turn_index=30),
        tmp_path / "s",
        tmp_path / "h",
        extra_env={"MEMPAL_DISABLE_HOOK": "1"},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"


def test_malformed_stdin_fails_open(tmp_path: Path) -> None:
    proc = _run_hook(SAVE_HOOK, "not json at all", tmp_path / "s", tmp_path / "h")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"


def test_pending_marker_skips_overlapping_mine(tmp_path: Path) -> None:
    state = tmp_path / "state"
    home = tmp_path / "home"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"sessionId": "s", "kind": "main"}\n')
    state.mkdir(parents=True, exist_ok=True)
    (state / "sagittarius_pending_sagittarius-test-1").write_text("")
    proc = _run_hook(
        SAVE_HOOK,
        _payload(turn_index=15, transcript_path=str(transcript)),
        state,
        home,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
    log = (state / "sagittarius_hook.log").read_text()
    assert "still in flight" in log


def test_save_interval_zero_floors_to_default(tmp_path: Path) -> None:
    """MEMPAL_SAVE_INTERVAL=0 must not crash on modulo-by-zero."""
    proc = _run_hook(
        SAVE_HOOK,
        _payload(turn_index=14),
        tmp_path / "s",
        tmp_path / "h",
        extra_env={"MEMPAL_SAVE_INTERVAL": "0"},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"


def test_save_interval_leading_zero_not_parsed_as_octal(tmp_path: Path) -> None:
    """MEMPAL_SAVE_INTERVAL=08 must not crash bash arithmetic on octal evaluation."""
    proc = _run_hook(
        SAVE_HOOK,
        _payload(turn_index=7),
        tmp_path / "s",
        tmp_path / "h",
        extra_env={"MEMPAL_SAVE_INTERVAL": "08"},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"


# ── SessionStart drain ────────────────────────────────────────────────


def test_drain_with_empty_spool_is_silent(tmp_path: Path) -> None:
    proc = _run_hook(DRAIN_HOOK, {"source": "startup"}, tmp_path / "s", tmp_path / "h")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"


def test_drain_with_unrunnable_mempalace_keeps_spool(tmp_path: Path) -> None:
    state = tmp_path / "state"
    home = tmp_path / "home"
    state.mkdir(parents=True, exist_ok=True)
    (state / "unmined.spool").write_text("/tmp/gone.jsonl|some_wing\n")
    proc = _run_hook(DRAIN_HOOK, {"source": "resume"}, state, home)
    assert proc.returncode == 0
    out = json.loads(proc.stdout.strip())
    assert "systemMessage" in out
    assert "still unavailable" in out["systemMessage"]
    # Spool left intact for the next launch.
    assert (state / "unmined.spool").exists()


def test_drain_mines_spooled_files_with_wings(tmp_path: Path) -> None:
    state = tmp_path / "state"
    home = tmp_path / "home"
    t1 = tmp_path / "a.jsonl"
    t1.write_text('{"sessionId": "s", "kind": "main"}\n')
    state.mkdir(parents=True, exist_ok=True)
    (state / "unmined.spool").write_text(f"{t1}|neo_geo_learn\n/tmp/does-not-exist.jsonl|gone\n")
    stub_log = tmp_path / "stub.log"
    stub = _write_stub_python(tmp_path, stub_log)
    proc = _run_hook(
        DRAIN_HOOK,
        {"source": "startup"},
        state,
        home,
        extra_env={"MEMPAL_PYTHON": str(stub)},
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout.strip())
    assert "catching up" in out["systemMessage"]
    assert _wait_for(stub_log)
    time.sleep(0.5)
    mine_calls = stub_log.read_text()
    assert str(t1) in mine_calls
    assert "neo_geo_learn" in mine_calls
    assert "does-not-exist" not in mine_calls


# ── PreCompress / SessionEnd ──────────────────────────────────────────


def test_precompress_spawns_mine_and_returns_silently(tmp_path: Path) -> None:
    state = tmp_path / "state"
    home = tmp_path / "home"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"sessionId": "s", "kind": "main"}\n')
    stub_log = tmp_path / "stub.log"
    stub = _write_stub_python(
        tmp_path, stub_log, transcript=str(transcript), cwd="/Users/robs/src/aes-demo1"
    )
    proc = _run_hook(
        PRECOMPRESS_HOOK,
        {
            "trigger": "auto",
            "session_id": "s-1",
            "transcript_path": str(transcript),
            "cwd": "/Users/robs/src/aes-demo1",
        },
        state,
        home,
        extra_env={"MEMPAL_PYTHON": str(stub)},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
    assert _wait_for(stub_log)
    time.sleep(0.3)
    assert "aes_demo1" in stub_log.read_text()


def test_session_end_spawns_final_mine_and_returns_silently(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    home = tmp_path / "home"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"sessionId": "s", "kind": "main"}\n')
    stub_log = tmp_path / "stub.log"
    stub = _write_stub_python(
        tmp_path, stub_log, transcript=str(transcript), cwd="/Users/robs/src/aes-demo1"
    )
    proc = _run_hook(
        SESSIONEND_HOOK,
        {
            "reason": "exit",
            "session_id": "s-1",
            "transcript_path": str(transcript),
            "cwd": "/Users/robs/src/aes-demo1",
        },
        state,
        home,
        extra_env={"MEMPAL_PYTHON": str(stub)},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
    assert _wait_for(stub_log)
    time.sleep(0.3)
    assert "aes_demo1" in stub_log.read_text()


# ── common.sh unit surface ────────────────────────────────────────────


def _bash_common(expr: str, tmp_path: Path) -> str:
    """Evaluate a bash expression with common.sh sourced, hermetic HOME."""
    home = tmp_path / "chome"
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["MEMPAL_STATE_DIR"] = str(tmp_path / "cstate")
    proc = subprocess.run(
        ["bash", "-c", f'source "{COMMON_LIB}"; {expr}'],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=10.0,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_infer_wing_cases(tmp_path: Path) -> None:
    out = _bash_common(
        'mempal_infer_wing "/Users/robs/src/neo_geo_learn"; echo;'
        'mempal_infer_wing "/My-Cool-App/"; echo;'
        'mempal_infer_wing ""; echo;'
        'mempal_infer_wing "/"; echo',
        tmp_path,
    ).splitlines()
    assert out[0] == "neo_geo_learn"
    assert out[1] == "my_cool_app"
    assert out[2] == "sagittarius_session"
    assert out[3] == "root"


def test_transcript_validation(tmp_path: Path) -> None:
    out = _bash_common(
        'mempal_is_valid_transcript "/a/b.jsonl" && echo ok1;'
        'mempal_is_valid_transcript "/a/../b.jsonl" || echo bad1;'
        'mempal_is_valid_transcript "/a/b.txt" || echo bad2;'
        'mempal_is_valid_transcript "" || echo bad3',
        tmp_path,
    ).splitlines()
    assert out == ["ok1", "bad1", "bad2", "bad3"]


def test_save_interval_unit_cases(tmp_path: Path) -> None:
    out = _bash_common(
        'MEMPAL_SAVE_INTERVAL="" mempal_save_interval; echo;'
        'MEMPAL_SAVE_INTERVAL="0" mempal_save_interval; echo;'
        'MEMPAL_SAVE_INTERVAL="08" mempal_save_interval; echo;'
        'MEMPAL_SAVE_INTERVAL="09" mempal_save_interval; echo;'
        'MEMPAL_SAVE_INTERVAL="015" mempal_save_interval; echo;'
        'MEMPAL_SAVE_INTERVAL="00" mempal_save_interval; echo;'
        'MEMPAL_SAVE_INTERVAL="notanumber" mempal_save_interval; echo;'
        'MEMPAL_SAVE_INTERVAL="20" mempal_save_interval; echo',
        tmp_path,
    ).splitlines()
    assert out == ["15", "15", "8", "9", "15", "15", "15", "20"]
