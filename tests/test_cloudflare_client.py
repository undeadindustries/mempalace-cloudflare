"""Unit tests for the local client plugin (mempalace_cloudflare_remote).

Tests that CloudflareRemoteBackend and CloudflareRemoteCollection
correctly interact with the Cloudflare Worker API.
"""

import io
import sys

import pytest

sys.path.insert(0, "./client")

from mempalace.backends.base import PalaceRef
from mempalace_cloudflare_remote import CloudflareRemoteBackend, CloudflareRemoteCollection

ACCESS_ENV = ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_urlopen(monkeypatch, body=b"{}"):
    """Replace urlopen and return the list of Request objects it receives."""
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return _FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


@pytest.fixture(autouse=True)
def no_access_env(monkeypatch):
    for name in ACCESS_ENV:
        monkeypatch.delenv(name, raising=False)


def test_client_sends_access_headers_from_env(monkeypatch, no_access_env):
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "abc.access")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "s3cret")
    backend = CloudflareRemoteBackend(options={"url": "http://mock-worker", "token": "tok"})
    col = backend.get_collection(palace=PalaceRef(id="p"), collection_name="drawers")
    captured = _capture_urlopen(monkeypatch, b'{"total_drawers": 3}')

    assert col.count() == 3
    req = captured[0]
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_header("Cf-access-client-id") == "abc.access"
    assert req.get_header("Cf-access-client-secret") == "s3cret"


def test_client_access_options_override_env(monkeypatch, no_access_env):
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "env.access")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "env-secret")
    backend = CloudflareRemoteBackend(
        options={
            "url": "http://mock-worker",
            "token": "tok",
            "access_client_id": "opt.access",
            "access_client_secret": "opt-secret",
        }
    )
    col = backend.get_collection(palace=PalaceRef(id="p"), collection_name="drawers")
    captured = _capture_urlopen(monkeypatch)

    col.count()
    assert captured[0].get_header("Cf-access-client-id") == "opt.access"
    assert captured[0].get_header("Cf-access-client-secret") == "opt-secret"


def test_client_rejects_half_configured_access(monkeypatch, no_access_env):
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "abc.access")
    backend = CloudflareRemoteBackend(options={"url": "http://mock-worker", "token": "tok"})

    with pytest.raises(ValueError, match="CF_ACCESS_CLIENT_SECRET"):
        backend.get_collection(palace=PalaceRef(id="p"), collection_name="drawers")


def test_client_without_access_sends_no_access_headers(monkeypatch, no_access_env):
    backend = CloudflareRemoteBackend(options={"url": "http://mock-worker", "token": "tok"})
    col = backend.get_collection(palace=PalaceRef(id="p"), collection_name="drawers")
    captured = _capture_urlopen(monkeypatch)

    col.count()
    assert captured[0].get_header("Cf-access-client-id") is None


def test_client_health_sends_access_headers(monkeypatch, no_access_env):
    monkeypatch.setenv("MEMPALACE_CLOUDFLARE_URL", "http://mock-worker")
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "abc.access")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "s3cret")
    captured = _capture_urlopen(monkeypatch, b'{"status": "ok"}')

    status = CloudflareRemoteBackend().health()
    assert status.ok
    assert captured[0].full_url == "http://mock-worker/healthz"
    assert captured[0].get_header("Cf-access-client-id") == "abc.access"
    # Cloudflare's bot protection can 403 urllib's default "Python-urllib" agent.
    assert captured[0].get_header("User-agent").startswith("mempalace-cloudflare-remote/")


def test_client_collection_construction():
    backend = CloudflareRemoteBackend(
        options={
            "url": "https://mempalace-cf.example.workers.dev",
            "token": "test-secret-token",
        }
    )
    palace = PalaceRef(id="test-palace", namespace="tenant-1")
    col = backend.get_collection(palace=palace, collection_name="drawers")
    assert isinstance(col, CloudflareRemoteCollection)
    assert col.base_url == "https://mempalace-cf.example.workers.dev"
    assert col.token == "test-secret-token"
    assert col.namespace == "tenant-1"


def test_client_upsert_preserves_caller_ids(monkeypatch):
    backend = CloudflareRemoteBackend(options={"url": "http://mock-worker", "token": "test-token"})
    palace = PalaceRef(id="test-palace")
    col = backend.get_collection(palace=palace, collection_name="drawers")

    captured_payloads = []

    def mock_request(method, path, data=None, params=None):
        captured_payloads.append((method, path, data, params))
        return {"result": {"content": [{"text": '{"checkpoint": "saved"}'}]}}

    monkeypatch.setattr(col, "_request", mock_request)

    col.upsert(
        documents=["Doc 1", "Doc 2"],
        ids=["custom-id-1", "custom-id-2"],
        metadatas=[{"wing": "w1", "room": "r1"}, {"wing": "w2", "room": "r2"}],
    )

    assert len(captured_payloads) == 1
    method, path, data, _ = captured_payloads[0]
    assert method == "POST"
    assert path == "/mcp"
    drawers = data["params"]["arguments"]["drawers"]
    assert drawers[0]["id"] == "custom-id-1"
    assert drawers[1]["id"] == "custom-id-2"
    assert data["jsonrpc"] == "2.0"
    assert data["id"] is not None
    assert data["method"] == "tools/call"


def test_client_mcp_calls_include_jsonrpc_id(monkeypatch):
    backend = CloudflareRemoteBackend(options={"url": "http://mock-worker", "token": "test-token"})
    palace = PalaceRef(id="test-palace")
    col = backend.get_collection(palace=palace, collection_name="drawers")

    captured_payloads = []

    def mock_request(method, path, data=None, params=None):
        captured_payloads.append(data)
        return {"result": {"content": [{"text": "[]"}]}}

    monkeypatch.setattr(col, "_request", mock_request)

    col.get(ids=["drawer-1"])
    col.delete(ids=["drawer-1"])

    assert len(captured_payloads) == 2
    ids = []
    for payload in captured_payloads:
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] is not None
        assert payload["method"] == "tools/call"
        ids.append(payload["id"])
    assert ids[0] != ids[1]


def test_client_upsert_forwards_source_file(monkeypatch):
    backend = CloudflareRemoteBackend(options={"url": "http://mock-worker", "token": "test-token"})
    palace = PalaceRef(id="test-palace")
    col = backend.get_collection(palace=palace, collection_name="drawers")

    captured_payloads = []

    def mock_request(method, path, data=None, params=None):
        captured_payloads.append(data)
        return {"result": {"content": [{"text": '{"checkpoint": "saved"}'}]}}

    monkeypatch.setattr(col, "_request", mock_request)

    col.upsert(
        documents=["Hook transcript line"],
        ids=["hook-id-1"],
        metadatas=[{"wing": "w1", "room": "r1", "source_file": "sessions/cursor.jsonl"}],
    )

    drawers = captured_payloads[0]["params"]["arguments"]["drawers"]
    assert drawers[0]["source_file"] == "sessions/cursor.jsonl"


def test_client_get_where_hydrates_content(monkeypatch):
    backend = CloudflareRemoteBackend(options={"url": "http://mock-worker", "token": "test-token"})
    palace = PalaceRef(id="test-palace")
    col = backend.get_collection(palace=palace, collection_name="drawers")

    captured_requests = []

    def mock_request(method, path, data=None, params=None):
        captured_requests.append((method, path, data, params))
        return {
            "drawers": [
                {
                    "id": "drawer-1",
                    "wing": "projects",
                    "room": "code",
                    "content": "Verbatim Code Content in R2",
                    "metadata": {"wing": "projects", "room": "code"},
                }
            ]
        }

    monkeypatch.setattr(col, "_request", mock_request)

    res = col.get(where={"wing": "projects"})
    assert len(captured_requests) == 1
    method, path, _, params = captured_requests[0]
    assert method == "GET"
    assert path == "/api/drawers"
    assert params["content"] == "true"
    assert params["wing"] == "projects"
    assert res.ids == ["drawer-1"]
    assert res.documents == ["Verbatim Code Content in R2"]
