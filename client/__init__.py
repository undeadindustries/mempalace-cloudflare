"""Mempalace Cloudflare Remote Client Package."""

from .mempalace_cloudflare_remote import CloudflareRemoteBackend, CloudflareRemoteCollection

try:
    from mempalace.backends.registry import register

    register("cloudflare-remote", CloudflareRemoteBackend)
except Exception:
    pass

__all__ = ["CloudflareRemoteBackend", "CloudflareRemoteCollection"]
