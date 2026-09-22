"""Unit tests for the local client plugin (mempalace_cloudflare_remote).

Tests that CloudflareRemoteBackend and CloudflareRemoteCollection
correctly interact with the Cloudflare Worker API.
"""

import sys
sys.path.insert(0, "./client")

from mempalace.backends.base import PalaceRef
from mempalace_cloudflare_remote import CloudflareRemoteBackend, CloudflareRemoteCollection


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
