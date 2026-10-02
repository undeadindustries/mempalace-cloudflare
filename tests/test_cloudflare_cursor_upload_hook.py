"""Tests for hooks/cloudflare/cursor_upload_hook.py (stdlib-only client hook)."""

import importlib.util
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).resolve().parent.parent / "hooks" / "cloudflare" / "cursor_upload_hook.py"
)


@pytest.fixture
def hook(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("cursor_upload_hook", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(module, "LOG_PATH", tmp_path / "state" / "upload.log")
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    for name in (
        "MEMPAL_DISABLE_HOOK",
        "MEMPALACE_CF_UPLOAD_DISABLED",
        "MEMPAL_CF_WING",
        "MEMPAL_CF_UPLOAD_INTERVAL",
        "MEMPALACE_CLOUDFLARE_URL",
        "MEMPALACE_CLOUDFLARE_TOKEN",
        "MEMPALACE_API_KEY",
        "CF_ACCESS_CLIENT_ID",
        "CF_ACCESS_CLIENT_SECRET",
        "MEMPALACE_CONFIG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    return module


class _Stub:
    """Local HTTP server answering /api/transcripts from a scripted list."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers["Content-Length"])
                stub.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(self.rfile.read(length)),
                    }
                )
                status, payload = stub.replies.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    created = []

    def make(replies):
        item = _Stub(replies)
        created.append(item)
        return item

    yield make
    for item in created:
        item.close()


def _config(url, **extra):
    base = {"url": url, "token": "tok", "access_id": "", "access_secret": ""}
    base.update(extra)
    return base


@pytest.mark.parametrize(
    "workspace,expected",
    [
        ("/Users/robs/src/mempalace-cloudflare", "mempalace_cloudflare"),
        ("/x/My  Project--1/", "my_project_1"),
        ("/", "root"),
        ("", "cursor_session"),
        ("/home/rob/src/___", "cursor_session"),
    ],
)
def test_infer_wing_matches_the_bash_hook(hook, workspace, expected):
    assert hook.infer_wing(workspace) == expected


def test_infer_wing_env_override(hook, monkeypatch):
    monkeypatch.setenv("MEMPAL_CF_WING", "forced")
    assert hook.infer_wing("/a/b") == "forced"


def test_load_config_env_beats_file_and_file_beats_nothing(hook, tmp_path, monkeypatch):
    cfg = tmp_path / "c.json"
    cfg.write_text(
        json.dumps(
            {
                "cloudflare_url": "https://file.example/",
                "cloudflare_token": "file-tok",
                "cloudflare_access_client_id": "fid",
                "cloudflare_access_client_secret": "fsecret",
            }
        )
    )
    from_file = hook.load_config(str(cfg))
    assert from_file["url"] == "https://file.example"
    assert from_file["token"] == "file-tok"
    assert (from_file["access_id"], from_file["access_secret"]) == ("fid", "fsecret")

    monkeypatch.setenv("MEMPALACE_CLOUDFLARE_URL", "https://env.example")
    monkeypatch.setenv("MEMPALACE_CLOUDFLARE_TOKEN", "env-tok")
    from_env = hook.load_config(str(cfg))
    assert (from_env["url"], from_env["token"]) == ("https://env.example", "env-tok")


def test_load_config_honours_config_dir_env(hook, tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        json.dumps({"cloudflare_url": "https://dir.example", "cloudflare_token": "t"})
    )
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path))
    assert hook.load_config()["url"] == "https://dir.example"


def test_config_problem_reports_missing_and_half_access(hook):
    assert hook.config_problem(_config("", token="")) is not None
    assert hook.config_problem(_config("https://x", access_id="only-id")) is not None
    assert hook.config_problem(_config("https://x")) is None


def test_claim_upload_first_event_runs_now_then_coalesces(hook, tmp_path):
    state = tmp_path / "s"
    assert hook.claim_upload(state, "conv", now=1000.0, interval=60) == 0.0
    assert hook.claim_upload(state, "conv", now=1001.0, interval=60) is None


def test_claim_upload_spaces_uploads_by_the_interval(hook, tmp_path):
    state = tmp_path / "s"
    assert hook.claim_upload(state, "conv", now=1000.0, interval=60) == 0.0
    (state / "conv.cf_pending").unlink()  # worker released its marker...
    last = state / "conv.cf_last"
    last.touch()  # ...after stamping "uploaded at 2000"
    os.utime(last, (2000.0, 2000.0))
    assert hook.claim_upload(state, "conv", now=2010.0, interval=60) == pytest.approx(50.0)
    (state / "conv.cf_pending").unlink()
    assert hook.claim_upload(state, "conv", now=2100.0, interval=60) == 0.0


def test_claim_upload_recovers_from_a_dead_worker_marker(hook, tmp_path):
    state = tmp_path / "s"
    assert hook.claim_upload(state, "conv", now=1000.0, interval=60) == 0.0
    marker = state / "conv.cf_pending"
    os.utime(marker, (1000.0, 1000.0))
    later = 1000.0 + 60 + hook.PENDING_GRACE_SECONDS + 1
    assert hook.claim_upload(state, "conv", now=later, interval=60) is not None


def test_conversations_do_not_share_state(hook, tmp_path):
    state = tmp_path / "s"
    assert hook.claim_upload(state, "a", now=1000.0, interval=60) == 0.0
    assert hook.claim_upload(state, "b", now=1000.0, interval=60) == 0.0


def test_upload_repeats_until_nothing_remains_and_sends_auth(hook, stub, tmp_path):
    server = stub(
        [
            (200, {"stored": 100, "already_filed": 0, "parsed_chunks": 250, "remaining": 150}),
            (200, {"stored": 100, "already_filed": 100, "parsed_chunks": 250, "remaining": 50}),
            (200, {"stored": 50, "already_filed": 200, "parsed_chunks": 250, "remaining": 0}),
        ]
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('{"role": "user"}\n', encoding="utf-8")

    totals = hook.upload_transcript(
        _config(server.url, access_id="id", access_secret="sec"), "proj", str(transcript)
    )

    assert totals["stored"] == 250
    assert len(server.requests) == 3
    first = server.requests[0]
    assert first["path"] == "/api/transcripts"
    assert first["headers"]["authorization"] == "Bearer tok"
    assert first["headers"]["cf-access-client-id"] == "id"
    assert first["headers"]["cf-access-client-secret"] == "sec"
    assert first["headers"]["user-agent"].startswith("mempalace-cloudflare-cursor-hook/")
    assert first["body"] == {
        "wing": "proj",
        "source_file": str(transcript),
        "transcript": '{"role": "user"}\n',
    }


def test_upload_does_not_retry_a_client_error(hook, stub, tmp_path):
    server = stub([(400, {"error": "wing bad"})])
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="HTTP 400"):
        hook.upload_transcript(_config(server.url), "w", str(transcript))
    assert len(server.requests) == 1


def test_upload_retries_server_errors_then_succeeds(hook, stub, tmp_path):
    server = stub([(503, {"error": "busy"}), (200, {"stored": 1, "remaining": 0})])
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x", encoding="utf-8")
    totals = hook.upload_transcript(_config(server.url), "w", str(transcript))
    assert totals["stored"] == 1
    assert len(server.requests) == 2


def test_upload_refuses_oversized_transcript(hook, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("0123456789", encoding="utf-8")
    monkeypatch.setattr(hook, "MAX_TRANSCRIPT_BYTES", 5)
    with pytest.raises(RuntimeError, match="over the"):
        hook.upload_transcript(_config("http://127.0.0.1:9"), "w", str(transcript))


def _event(transcript, **extra):
    data = {
        "conversation_id": "c1",
        "transcript_path": str(transcript),
        "workspace_roots": ["/home/u/src/My-Project"],
        "loop_count": 0,
    }
    data.update(extra)
    return json.dumps(data)


@pytest.fixture
def spawned(hook, monkeypatch):
    calls = []
    monkeypatch.setattr(hook, "spawn_worker", lambda *args: calls.append(args))
    return calls


def test_event_spawns_one_worker_with_inferred_wing_and_coalesces(hook, spawned, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x", encoding="utf-8")
    hook.handle_hook_event(_event(transcript), now=1000.0)
    hook.handle_hook_event(_event(transcript), now=1001.0)
    assert spawned == [("c1", str(transcript), "my_project", 0.0)]


def test_event_is_ignored_for_followup_loops_bad_paths_and_kill_switch(
    hook, spawned, tmp_path, monkeypatch
):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x", encoding="utf-8")
    hook.handle_hook_event(_event(transcript, loop_count=1), now=1000.0)
    hook.handle_hook_event(_event(tmp_path / "missing.jsonl"), now=1000.0)
    hook.handle_hook_event(_event(tmp_path / "notes.txt"), now=1000.0)
    monkeypatch.setenv("MEMPAL_DISABLE_HOOK", "1")
    hook.handle_hook_event(_event(transcript), now=1000.0)
    assert spawned == []


def test_main_always_prints_empty_object_even_on_garbage_stdin(hook, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook.main([]) == 0
    assert capsys.readouterr().out.strip() == "{}"
    assert "ERROR hook event failed" in hook.LOG_PATH.read_text()


def test_worker_logs_and_skips_when_config_is_incomplete(hook, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "nothing-here"))
    hook.run_worker("c1", str(tmp_path / "t.jsonl"), "w", 0.0)
    assert "not uploading" in hook.LOG_PATH.read_text()
    assert not (hook.STATE_DIR / "c1.cf_pending").exists()


def test_install_registers_both_events_once_and_keeps_other_hooks(hook, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hooks_json = tmp_path / ".cursor" / "hooks.json"
    hooks_json.parent.mkdir()
    hooks_json.write_text(
        json.dumps({"version": 1, "hooks": {"stop": [{"command": "/other/hook.sh"}]}})
    )

    destination = hook.install(config_file=None)
    hook.install(config_file=str(tmp_path / "cf.json"))

    assert destination.is_file()
    document = json.loads(hooks_json.read_text())
    for event in ("stop", "preCompact"):
        ours = [e for e in document["hooks"][event] if "cursor_upload_hook.py" in e["command"]]
        assert len(ours) == 1
        assert f"--config {tmp_path / 'cf.json'}" in ours[0]["command"]
    assert {"command": "/other/hook.sh"} in document["hooks"]["stop"]
    assert list(hooks_json.parent.glob("hooks.json.bak-*"))
