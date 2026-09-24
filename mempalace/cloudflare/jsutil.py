"""Convert Python values into JavaScript values for Workers bindings.

Vectorize and Workers AI parse their own request bodies. A bare Python list or
dict is stringified and rejected. CPython unit tests have no Pyodide, so the
original object is returned unchanged.
"""

from typing import Any


def as_js(value: Any) -> Any:
    """Return ``value`` as a JS object when running inside the Workers runtime."""
    try:
        from js import Object
        from pyodide.ffi import to_js
    except ImportError:
        return value
    return to_js(value, dict_converter=Object.fromEntries)
