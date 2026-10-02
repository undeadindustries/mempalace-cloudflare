"""Tests for the Worker-side Cursor transcript parser and chunker."""

import json

import pytest

from mempalace.cloudflare import transcripts as tr


def _line(role: str, text: str) -> str:
    return json.dumps({"role": role, "message": {"content": [{"type": "text", "text": text}]}})


def _jsonl(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_parse_unwraps_user_query_and_keeps_words_verbatim():
    text = _jsonl(
        _line("user", "<user_query>\nfix the  login bug,  please\n</user_query>"),
        _line("assistant", "Looking at `auth.py` now."),
    )
    assert tr.parse_cursor_jsonl(text) == [
        ("user", "fix the  login bug,  please"),
        ("assistant", "Looking at `auth.py` now."),
    ]


def test_parse_drops_injected_blocks_and_tool_blocks():
    user = (
        "<system_reminder>secret harness text</system_reminder>"
        "<user_query>real question</user_query>"
        "<timestamp>Friday</timestamp>"
    )
    assistant_blocks = {
        "role": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "answer part one"},
                {"type": "tool_use", "name": "Shell", "input": {"command": "ls"}},
                {"type": "text", "text": "answer part two"},
            ]
        },
    }
    text = _jsonl(_line("user", user), json.dumps(assistant_blocks))
    assert tr.parse_cursor_jsonl(text) == [
        ("user", "real question"),
        ("assistant", "answer part one\nanswer part two"),
    ]


def test_parse_merges_consecutive_assistant_messages_and_skips_noise_lines():
    text = _jsonl(
        _line("user", "<user_query>q</user_query>"),
        "not json at all",
        json.dumps({"type": "turn_ended"}),
        _line("assistant", "first"),
        _line("assistant", "second"),
    )
    assert tr.parse_cursor_jsonl(text) == [("user", "q"), ("assistant", "first\nsecond")]


def test_parse_rejects_claude_code_shaped_records():
    text = _jsonl(json.dumps({"type": "user", "message": {"content": "hi"}}))
    assert tr.parse_cursor_jsonl(text) == []


def test_chunks_pair_each_user_turn_with_its_reply():
    chunks = tr.chunk_messages(
        [
            ("user", "first question about the deployment"),
            ("assistant", "first answer with enough words to pass the floor"),
            ("user", "second question about rollback"),
            ("assistant", "second answer, also long enough to be kept"),
        ]
    )
    assert chunks == [
        "> first question about the deployment\nfirst answer with enough words to pass the floor",
        "> second question about rollback\nsecond answer, also long enough to be kept",
    ]


def test_chunks_split_long_reply_without_losing_a_character():
    reply = "x" * (tr.CHUNK_CHARS * 2 + 50)
    chunks = tr.chunk_messages([("user", "a question long enough to count"), ("assistant", reply)])
    assert len(chunks) == 3
    assert all(len(c) <= tr.CHUNK_CHARS for c in chunks)
    assert "".join(chunks) == "> a question long enough to count\n" + reply


def test_chunks_drop_exchanges_at_or_below_the_noise_floor():
    assert tr.chunk_messages([("user", "ok"), ("assistant", "k")]) == []


def test_chunks_keep_assistant_text_before_first_user_turn():
    chunks = tr.chunk_messages([("assistant", "a leading assistant message that is long enough")])
    assert chunks == ["a leading assistant message that is long enough"]


def test_build_drawers_ids_are_stable_and_scoped_to_source_and_position():
    transcript = _jsonl(
        _line("user", "<user_query>a question that is long enough</user_query>"),
        _line("assistant", "an answer that is long enough to keep"),
    )
    first = tr.build_drawers("proj", "/a/t.jsonl", transcript)
    again = tr.build_drawers("proj", "/a/t.jsonl", transcript)
    other = tr.build_drawers("proj", "/b/t.jsonl", transcript)

    assert [d["id"] for d in first] == [d["id"] for d in again]
    assert first[0]["id"] != other[0]["id"]
    assert first[0]["id"].startswith(f"drawer_proj_{tr.TRANSCRIPT_ROOM}_")
    assert first[0]["wing"] == "proj"
    assert first[0]["room"] == tr.TRANSCRIPT_ROOM
    assert first[0]["source_file"] == "/a/t.jsonl"
    assert first[0]["metadata"]["chunk_index"] == 0


def test_build_drawers_earlier_ids_survive_appended_turns():
    base = [
        _line("user", "<user_query>question number one is here</user_query>"),
        _line("assistant", "answer number one is here too"),
    ]
    extended = base + [
        _line("user", "<user_query>question number two is here</user_query>"),
        _line("assistant", "answer number two is here too"),
    ]
    before = tr.build_drawers("p", "s", _jsonl(*base))
    after = tr.build_drawers("p", "s", _jsonl(*extended))
    assert after[0]["id"] == before[0]["id"]
    assert len(after) == 2


@pytest.mark.parametrize("wing", ["", " ", "a" * 65, "bad/wing", "x\ny"])
def test_validate_wing_rejects_unsafe_names(wing):
    assert tr.validate_wing(wing) is not None


def test_validate_wing_accepts_normal_slug():
    assert tr.validate_wing("mempalace_cloudflare") is None
