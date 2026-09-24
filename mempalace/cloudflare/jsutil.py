"""Convert Python values into JavaScript values for Workers bindings.

Vectorize and Workers AI parse their own request bodies. A bare Python list or
dict is stringified and rejected. CPython unit tests have no Pyodide, so the
original object is returned unchanged.
"""

import json
from typing import Any, Sequence


def bind_params(stmt: Any, params: Sequence[Any]) -> Any:
    """Bind D1 parameters, sending Python ``None`` as SQL NULL.

    Pyodide passes ``None`` to JavaScript as ``undefined``, which D1 rejects
    with D1_TYPE_ERROR. Parsing the list as JSON on the JS side yields a real
    ``null``, and ``Function.apply`` keeps it from turning back into ``None``.
    """
    if None not in params:
        return stmt.bind(*params)
    try:
        import js
    except ImportError:
        return stmt.bind(*params)
    js_params = js.JSON.parse(json.dumps(list(params)))
    return stmt.bind.apply(stmt, js_params)


def as_js(value: Any) -> Any:
    """Return ``value`` as a JS object when running inside the Workers runtime."""
    try:
        from js import Object
        from pyodide.ffi import to_js
    except ImportError:
        return value
    return to_js(value, dict_converter=Object.fromEntries)
