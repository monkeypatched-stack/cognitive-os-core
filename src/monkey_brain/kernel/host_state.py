"""The hosting process's shared state object, when it has one.

The control plane (src/monkey_brain/api/main.py) registers its FastAPI
``app.state`` here at import time; kernel code that needs a component the
host wired up (cognitive_runtime, policy, ...) reads it via
get_host_state() instead of importing the control plane's HTTP app.
Importing ``api.main`` from the kernel constructed the whole control-plane
app as a side effect -- inside an Actor Runtime process, too. A process
that registers nothing (an Actor Runtime) gets None, which callers treat
the same as "component not available".
"""

from __future__ import annotations

from typing import Any

_host_state: Any = None


def set_host_state(state: Any) -> None:
    global _host_state
    _host_state = state


def get_host_state() -> Any:
    return _host_state
