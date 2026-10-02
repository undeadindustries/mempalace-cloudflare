"""Tests for POST /api/transcripts: idempotent, resumable, verbatim transcript ingest."""

import asyncio
import json

from mempalace.cloudflare import transcripts as tr
from mempalace.cloudflare.entrypoint import CloudflareMemPalaceApp
from tests.test_cloudflare_entrypoint import create_test_env, send_asgi_request

AUTH = {b"authorization": b"Bearer secret"}
SOURCE = "/home/u/.cursor/projects/p/agent-transcripts/c1/c1.jsonl"


def _exchange(n: int) -> list:
    user = f"<user_query>question number {n} about the deployment pipeline</user_query>"
    return [
        json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": user}]}}),
        json.dumps(
            {
                "role": "assistant",
                "message": {"content": [{"type": "text", "text": f"answer number {n}, in words"}]},
            }
        ),
    ]


def _transcript(count: int) -> str:
    lines: list = []
    for n in range(count):
        lines.extend(_exchange(n))
    return "\n".join(lines) + "\n"


def _post(app, env, **body):
    payload = {"wing": "proj", "source_file": SOURCE, "transcript": _transcript(2)}
    payload.update(body)
    return send_asgi_request(app, "POST", "/api/transcripts", body=payload, headers=AUTH, env=env)


def _count_embeds(env) -> dict:
    calls = {"texts": 0}
    original = env.AI.run

    async def counting_run(model, payload):
        calls["texts"] += len(payload["text"])
        return await original(model, payload)

    env.AI.run = counting_run
    return calls


def test_ingest_files_exchanges_verbatim_and_second_upload_is_a_no_op():
    async def _test():
        env = create_test_env(api_key="secret")
        app = CloudflareMemPalaceApp(env=env)
        embeds = _count_embeds(env)

        first = await _post(app, env)
        assert first["status"] == 200
        assert first["json"]["parsed_chunks"] == 2
        assert first["json"]["stored"] == 2
        assert first["json"]["remaining"] == 0
        assert embeds["texts"] == 2

        again = await _post(app, env)
        assert again["json"]["stored"] == 0
        assert again["json"]["already_filed"] == 2
        assert embeds["texts"] == 2  # no new embedding spend

        tools = app._get_tools(env)
        drawer_id = first["json"]["drawer_ids"][0]
        got = await tools.tool_get_drawer(drawer_id)
        assert got["content"] == (
            "> question number 0 about the deployment pipeline\nanswer number 0, in words"
        )
        assert got["metadata"]["source_file"] == SOURCE
        assert got["metadata"]["wing"] == "proj"
        assert got["metadata"]["room"] == tr.TRANSCRIPT_ROOM

    asyncio.run(_test())


def test_ingest_only_stores_turns_added_since_the_last_upload():
    async def _test():
        env = create_test_env(api_key="secret")
        app = CloudflareMemPalaceApp(env=env)
        await _post(app, env, transcript=_transcript(2))
        grown = await _post(app, env, transcript=_transcript(5))
        assert grown["json"]["parsed_chunks"] == 5
        assert grown["json"]["already_filed"] == 2
        assert grown["json"]["stored"] == 3

    asyncio.run(_test())


def test_ingest_caps_each_request_and_resumes_on_the_next(monkeypatch):
    async def _test():
        monkeypatch.setattr("mempalace.cloudflare.tools.MAX_NEW_CHUNKS_PER_REQUEST", 2)
        env = create_test_env(api_key="secret")
        app = CloudflareMemPalaceApp(env=env)
        body = {"transcript": _transcript(5)}

        first = await _post(app, env, **body)
        assert (first["json"]["stored"], first["json"]["remaining"]) == (2, 3)
        second = await _post(app, env, **body)
        assert (second["json"]["stored"], second["json"]["remaining"]) == (2, 1)
        third = await _post(app, env, **body)
        assert (third["json"]["stored"], third["json"]["remaining"]) == (1, 0)

        count = await app._get_tools(env).col.a_count()
        assert count == 5

    asyncio.run(_test())


def test_ingest_rejects_bad_requests_before_writing_anything():
    async def _test():
        env = create_test_env(api_key="secret")
        app = CloudflareMemPalaceApp(env=env)
        for bad in (
            {"wing": "bad/wing"},
            {"wing": ""},
            {"source_file": ""},
            {"transcript": ""},
            {"transcript": 5},
        ):
            res = await _post(app, env, **bad)
            assert res["status"] == 400, bad
        assert await app._get_tools(env).col.a_count() == 0

    asyncio.run(_test())


def test_ingest_rejects_oversized_transcript(monkeypatch):
    async def _test():
        monkeypatch.setattr("mempalace.cloudflare.tools.MAX_TRANSCRIPT_BYTES", 50)
        env = create_test_env(api_key="secret")
        app = CloudflareMemPalaceApp(env=env)
        res = await _post(app, env, transcript=_transcript(3))
        assert res["status"] == 413
        assert await app._get_tools(env).col.a_count() == 0

    asyncio.run(_test())


def test_ingest_reports_zero_chunks_for_a_non_cursor_file():
    async def _test():
        env = create_test_env(api_key="secret")
        app = CloudflareMemPalaceApp(env=env)
        res = await _post(app, env, transcript="just some text\nnot json\n")
        assert res["status"] == 200
        assert res["json"]["parsed_chunks"] == 0
        assert res["json"]["stored"] == 0

    asyncio.run(_test())


def test_ingest_requires_bearer_token():
    async def _test():
        env = create_test_env(api_key="secret")
        app = CloudflareMemPalaceApp(env=env)
        res = await send_asgi_request(
            app,
            "POST",
            "/api/transcripts",
            body={"wing": "w", "source_file": "s", "transcript": "t"},
            env=env,
        )
        assert res["status"] == 401

    asyncio.run(_test())
