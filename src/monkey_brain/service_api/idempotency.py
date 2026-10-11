"""``@idempotent`` — the HTTP route decorator over the shared idempotency
store.

The store itself (backends, reserve/complete/release, request_fingerprint)
lives in kernel/idempotency_store.py so the Actor Runtime and kernel
effect boundaries can use it without importing the control-plane API
package. This module keeps only the FastAPI-shaped part, and re-exports
the store names so existing ``from src.monkey_brain.service_api.idempotency
import ...`` call sites (and the control plane's api.idempotency alias) keep working.
"""

from __future__ import annotations

from typing import Any

from src.monkey_brain.kernel.idempotency_store import (  # noqa: F401 — re-exported
    _ABANDONED,
    _COMPLETED,
    _IN_PROGRESS,
    DEFAULT_CAPACITY,
    DEFAULT_TTL_SECONDS,
    RESERVATION_TIMEOUT_SECONDS,
    IdempotencyRecord,
    IdempotencyStore,
    _InMemoryIdempotencyBackend,
    _make_backend,
    _RedisIdempotencyBackend,
    _UnavailableIdempotencyBackend,
    get_idempotency_store,
    request_fingerprint,
)


def _find_request_body(kwargs: dict[str, Any]) -> Any:
    """The route's Pydantic request body, regardless of its parameter
    name. Most decorated routes name it `body`, but that's a convention,
    not a guarantee — /prompt's body param is `payload`. Falls back to
    scanning every kwarg for the first Pydantic BaseModel instance
    (duck-typed via model_dump, matching this module's own existing
    convention at the call site below), skipping `request` itself."""
    body_obj = kwargs.get("body")
    if body_obj is not None:
        return body_obj
    for k, v in kwargs.items():
        if k in ("request", "user_id") or v is None:
            continue
        if hasattr(v, "model_dump"):
            return v
    return None


def _to_cacheable(result: Any) -> tuple[bool, Any]:
    """(cacheable, payload). Plain dicts cache as-is, unchanged from
    before. A Pydantic BaseModel instance (e.g. PromptResponse) now also
    caches — tagged with its concrete class so replay reconstructs the
    SAME type rather than returning a bare dict a caller expecting a
    model instance wouldn't get from a real (non-replayed) call. Replay
    bypasses the route function entirely, so FastAPI's own response_model
    coercion never runs on a replayed response — this has to do that
    reconstruction itself."""
    if isinstance(result, dict):
        return True, result
    if hasattr(result, "model_dump") and hasattr(type(result), "model_validate"):
        return True, {
            "__idempotent_pydantic__": True,
            "class": f"{type(result).__module__}:{type(result).__qualname__}",
            "data": result.model_dump(),
        }
    return False, None


def _from_cached(payload: Any) -> Any:
    """Inverse of _to_cacheable's Pydantic branch — only ever reconstructs
    a class this same decorator itself tagged and cached, never arbitrary
    caller input, so the dotted-path import is safe."""
    if isinstance(payload, dict) and payload.get("__idempotent_pydantic__"):
        import importlib

        module_name, qualname = payload["class"].split(":", 1)
        obj: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            obj = getattr(obj, part)
        return obj.model_validate(payload["data"])
    return payload



# ── Route decorator ──────────────────────────────────────────────────────

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


def idempotent(resource: str):
    """Apply to a mutating route, directly above the function definition
    and BELOW @router.post(...):

        @router.post("/orders", ...)
        @idempotent("orders.create")
        async def create_order(body: OrderCreateRequest, request: Request, user_id=Depends(...)):
            ...

    Opt-in in insecure-dev — a request with no Idempotency-Key header
    executes normally. Outside insecure-dev the header is required and
    the request is fail-closed (400) without it.

    Must be the INNERMOST decorator (directly above `async def`, below
    @router.*) — decorators apply bottom-up, so @router.post(...) needs
    to register the ALREADY-wrapped function to see its
    functools.wraps-copied signature and resolve Depends()/body/path
    params correctly; registering the raw function first (idempotent
    below router.post instead of above) would make @router.post capture
    the unwrapped handler and skip idempotency entirely. Verified against
    a live TestClient before this was wired into any real route.

    Relies on every route in this codebase consistently naming its
    Request parameter `request` and its auth dependency `user_id` — true
    of every route this is applied to (see ADR-009).
    """

    def decorator(fn):
        import functools

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            from fastapi import HTTPException, Request

            request: Request | None = kwargs.get("request")
            if request is None:
                return await fn(*args, **kwargs)

            key = request.headers.get(IDEMPOTENCY_KEY_HEADER)
            if not key:
                key = request.headers.get("X-Razorpay-Event-Id") or request.headers.get("X-Razorpay-Signature")
            if not key:
                from src.monkey_brain.kernel.production_gates import insecure_dev_mode

                if not insecure_dev_mode():
                    raise HTTPException(
                        status_code=400,
                        detail="Idempotency-Key header required for this mutating operation",
                    )
                return await fn(*args, **kwargs)

            user_id = kwargs.get("user_id", "")
            body_obj = _find_request_body(kwargs)
            body_dict = body_obj.model_dump() if hasattr(body_obj, "model_dump") else None
            scoped_key = f"{resource}:{user_id}:{key}"
            request_hash = request_fingerprint(request.method, request.url.path, body_dict)

            # Best-effort correlation for the audit trail below — the
            # decorator is generic across every route it's applied to, so
            # the Idempotency-Key itself (stable, caller-supplied) is the
            # one identifier guaranteed available regardless of which
            # route this wraps.
            from src.monkey_brain.kernel.pipeline.audit_trail import (
                record_decision_event,
            )
            from src.monkey_brain.kernel.compile import _obs

            store = get_idempotency_store()
            claimed, existing = store.reserve(scoped_key, request_hash)
            if not claimed:
                if existing is None:
                    from src.monkey_brain.kernel.production_gates import (
                        idempotency_fail_closed,
                    )

                    if idempotency_fail_closed():
                        raise HTTPException(
                            status_code=503,
                            detail="Idempotency store unavailable — request refused (fail-closed)",
                        )
                if existing is not None and existing.state == _ABANDONED:
                    _obs.counter("idempotency.requests.total", result="abandoned")
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Idempotency-Key {key!r} reservation expired after a crash; "
                            "reconciliation required — do not retry the effect"
                        ),
                    )
                if existing is not None and existing.state == _COMPLETED:
                    if existing.request_hash != request_hash:
                        record_decision_event(
                            "idempotency_conflict",
                            actor_id=str(user_id),
                            execution_id=key,
                            reason=f"Idempotency-Key {key!r} reused for a different request ({resource})",
                        )
                        _obs.counter("idempotency.requests.total", result="conflict")
                        raise HTTPException(
                            status_code=409,
                            detail=f"Idempotency-Key {key!r} was already used for a different request",
                        )
                    record_decision_event(
                        "idempotency_replay",
                        actor_id=str(user_id),
                        execution_id=key,
                        reason=f"Idempotency-Key {key!r} replayed cached result ({resource})",
                    )
                    _obs.counter("idempotency.requests.total", result="replay")
                    return _from_cached(existing.response_body)
                # Genuinely concurrent in-progress race — not literally
                # "conflict" (a different payload) or "replay" (nothing
                # completed yet), but the spec's result label set is
                # closed to new/replay/conflict; this is the closest fit
                # (the request could not proceed as a fresh execution).
                _obs.counter("idempotency.requests.total", result="conflict")
                raise HTTPException(
                    status_code=409,
                    detail=f"a request with Idempotency-Key {key!r} is already being processed",
                )

            _obs.counter("idempotency.requests.total", result="new")
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                from src.monkey_brain.kernel.security_operation import (
                    UnknownOutcomeError,
                )

                if isinstance(exc, UnknownOutcomeError):
                    store.complete(
                        scoped_key,
                        request_hash,
                        {
                            "outcome": "unknown",
                            "reconciliation_required": True,
                            "operation_id": getattr(exc, "operation_id", ""),
                        },
                    )
                    raise HTTPException(
                        status_code=409,
                        detail="operation outcome unknown; reconciliation required; do not retry",
                    ) from exc
                store.release(scoped_key)
                raise
            cacheable, payload = _to_cacheable(result)
            if cacheable:
                store.complete(scoped_key, request_hash, payload)
            else:
                # Not a dict or Pydantic model — release rather than cache
                # something we can't safely replay.
                store.release(scoped_key)
            return result

        return wrapper

    return decorator
