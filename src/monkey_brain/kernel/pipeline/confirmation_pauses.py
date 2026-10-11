"""Pluggable pause-until-confirmed kinds for ActionExecutor.

Approval and negotiation pauses are core coordination and stay built into
action_executor.py. Domain pauses (e.g. payments, which wait for a payer to
approve in their own app) register here from their domain plugin instead,
so the executor never imports domain code. The executor:

  - on entry, loads each kind's *resolved* record for the execution
    (load_resolved) and, when the resumed step comes round, merges
    resume_context(record) into that step's context;
  - when a step's result dict sets a kind's result_flag, saves a pending
    record (save_pending, once per kind per tick) and reports that kind's
    waiting_status as the execution's paused status.

Same capability-driven opt-in contract as requires_approval: the executor
never decides what a pause means, the capability asks for it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ConfirmationPause:
    name: str
    result_flag: str
    waiting_status: str
    load_resolved: Callable[[str], Any]
    """execution_id -> a record with .step_index that has been decided, or None."""
    resume_context: Callable[[Any], dict]
    """resolved record -> context keys for the resumed step."""
    save_pending: Callable[..., None]
    """(*, execution_id, actor_id, action, result, context) -> None."""


_pauses: dict[str, ConfirmationPause] = {}
_lock = threading.Lock()


def register_confirmation_pause(pause: ConfirmationPause) -> None:
    with _lock:
        _pauses[pause.name] = pause


def confirmation_pauses() -> tuple[ConfirmationPause, ...]:
    with _lock:
        return tuple(_pauses.values())
