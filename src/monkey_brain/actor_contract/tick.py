"""One governed-caller actor tick and its response shape.

Shared by the control plane's POST /actors/{id}/execute (actors resident in
the control-plane process — the monolithic deployment.yaml shape) and the
Actor Runtime's own POST /execute (the per-actor-Pod shape), so a caller sees
an identical response regardless of which path served it. Callers are
responsible for governance (ensure_governed) around run_actor_tick.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import HTTPException


def gate_on_world_validation(pr: Any, actor_id: str | None = None) -> None:
    """Gate 3 (ADR-010) — before execute: block a real action from running
    against a structurally broken world. Off switch (WORLD_VALIDATION_GATE_
    EXECUTE=false) exists for perf-sensitive deployments once scale testing
    (Gate 7) has a real number for the cost of a full world scan per call;
    default is ON because correctness matters more than that cost today.

    actor_id scopes an actor-scoped violation (presence_consistency/
    membership_consistency) belonging to some OTHER actor out of the
    pass/fail decision — same reasoning validate_world()'s own docstring
    already documents, and the same param prompt.py's equivalent gate
    call already passes (validate_world(planetary_runtime, actor_id=
    user_id)). Without this, run_actor_tick() (used by both the control
    plane's POST /actors/{id}/execute and actor_runtime.py's per-Pod POST
    /execute) blocked EVERY actor's execution on ANY unrelated actor's
    pre-existing violation anywhere in the whole shared world, not just
    a violation about the actor actually being executed — confirmed live
    with a second, unrelated debug actor's own presence gap. NOTE: this
    does NOT exempt a violation about actor_id itself (validate_world's
    own filter keeps `v["actor_id"] == actor_id` in `blocking` on
    purpose) — an actor whose OWN presence/membership record is broken
    still correctly blocks on its own execution either way."""
    if os.getenv("WORLD_VALIDATION_GATE_EXECUTE", "true").strip().lower() == "false":
        return
    from src.monkey_brain.kernel.validation.world_validator import validate_world

    report = validate_world(pr, actor_id=actor_id)
    if not report["ok"]:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "world validation failed — refusing to execute against a structurally inconsistent world",
                "violation_count": report["violation_count"],
                "categories": report["categories"],
            },
        )


async def run_actor_tick(actor_id: str, pr: Any, state: Any) -> dict[str, Any]:
    """The actual tick-and-format-response work behind POST /execute."""
    if state.actor_runtime is None:
        return {"actor_id": actor_id, "status": "no_tick_handler"}
    gate_on_world_validation(pr, actor_id=actor_id)
    try:
        # Same 30s cap SocietyRuntime.tick_one_actor() already applies to
        # the identical operation (a single actor's cognitive tick, LLM
        # planner included) — this route called actor_runtime.tick()
        # directly with no timeout at all, so a slow LLM call here could
        # hang indefinitely instead of failing predictably like every
        # other actor-tick entry point does.
        result = await asyncio.wait_for(state.actor_runtime.tick(), timeout=30.0)
        state.cycle_count += 1
        state.last_cycle = time.time()
        pr._save_actors()

        return {
            "actor_id": actor_id,
            "goal": state.profile.goals[0] if state.profile.goals else "",
            "goal_achieved": (
                getattr(result, "outcome", {}).get("goal_achieved", False)
                if isinstance(getattr(result, "outcome", None), dict)
                else False
            ),
            "actions": [
                {
                    "action_id": a.get("action_id", ""),
                    "success": a.get("success", False),
                    "error": a.get("error", ""),
                }
                for a in (getattr(result, "actions", None) or [])
            ],
            "belief_updated": getattr(result, "belief_updated", False),
            "learned": getattr(result, "learned", False),
            "error": getattr(result, "error", None),
            "cycle_count": state.cycle_count,
        }
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Actor {actor_id} tick timed out after 30s (LLM planner latency — see cognitive-os-docs/engineering/adr/016-performance-gate9.md)",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Execute failed: {e}")
