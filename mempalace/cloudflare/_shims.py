"""Cloudflare Workers Pyodide runtime shims.

Must be imported BEFORE any mempalace modules on Cloudflare Workers.
Stubs out heavyweight/native C-extensions (like chromadb) that are not needed
on the serverless Cloudflare Workers runtime, allowing upstream pure-Python
components (BaseBackend, QueryResult, searcher ranking, dialect) to import cleanly.
"""

import sys
import types
from unittest.mock import MagicMock


def install_shims() -> None:
    """Install sys.modules stubs for native dependencies."""
    stubs = [
        "chromadb",
        "chromadb.config",
        "chromadb.api",
        "chromadb.api.models",
        "chromadb.api.models.Collection",
        "chromadb.errors",
        "chromadb.telemetry",
        "chromadb.telemetry.product",
        "chromadb.telemetry.product.posthog",
        "onnxruntime",
        "tokenizers",
    ]
    for module_name in stubs:
        if module_name not in sys.modules:
            mock = MagicMock()
            mock.__spec__ = types.SimpleNamespace(name=module_name)
            mock.__file__ = f"<{module_name}_shim>"
            sys.modules[module_name] = mock


# Auto-install when this module is imported
install_shims()
