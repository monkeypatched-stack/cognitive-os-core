"""The planetary cycle's world dynamics run through the governed-mutation pipeline.

Live 2026-10-15: every auto-tick logged "ungoverned state mutation:
shared_world.perturb" -- PlanetaryRuntime._run_cycle wrote to SharedWorld with no
commitment, so outside insecure-dev mode the whole cycle died at its first line.
The suite's conftest turns insecure-dev mode on, which is why nothing caught it:
these tests turn it off for the cycle itself and stub only the OPA call.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest

from src.monkey_brain.kernel.security_boundary import (
    HumanApprovalRequired,
    SecurityBoundaryDenied,
    commitment_active,
)
from src.monkey_brain.kernel.society.integration import (
    PLANETARY_SCHEDULER_PRINCIPAL,
    WORLD_DYNAMICS_ACTION,
    PlanetaryRuntime,
)
from src.monkey_brain.kernel.society.perturbation_queue import WorldPerturbation
from src.monkey_brain.kernel.trusted_auth import get_trusted_auth

AUTHZ = "src.monkey_brain.kernel.security_boundary._authorize"


def _allow(seen: list):
    async def authorize(action, resource, extra, *, verified_delegation=None):
        ev = get_trusted_auth()
        seen.append({"action": action, "resource": resource, "principal": ev.principal_id,
                     "principal_type": ev.principal_type})
        return {"allowed": True, "reason": "test", "approval_mode": "AUTO_APPROVE",
                "approval_source": "POLICY_AUTOMATIC", "risk_level": "LOW", "policy_rule": "test",
                "requires_hitl": False}
    return authorize


ISOLATED_DB = f"cogos_test_world_dynamics_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def runtime():
    """A runtime on its own empty Mongo database: construction otherwise rehydrates every actor
    persisted in the developer's local database (1,714 of them on 2026-10-15, ~100 s), none of
    which these tests need. The database is this module's own and is dropped afterwards; the
    developer's data is never read or written."""
    from src.monkey_brain.persistence.db_pool import reset_db_pool

    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", f"mongodb://localhost:27017/{ISOLATED_DB}")
    reset_db_pool()
    try:
        yield PlanetaryRuntime()   # built in dev mode (no OPA/Redis here); each cycle runs secure
    finally:
        mp.undo()
        reset_db_pool()
        try:
            from pymongo import MongoClient

            MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=2000).drop_database(ISOLATED_DB)
        except Exception:
            pass


@pytest.fixture
def secure_runtime(runtime, monkeypatch):
    from src.monkey_brain.kernel.approval import reset_approval_store
    from src.monkey_brain.kernel.security_operation import reset_operation_ledger_for_tests

    reset_approval_store()
    reset_operation_ledger_for_tests()
    runtime._perturbation_queue.drain()
    monkeypatch.setenv("COGNITIVEOS_ALLOW_INSECURE_DEV_MODE", "false")  # the cycle itself runs secure
    return runtime


def test_an_ungoverned_world_write_is_still_blocked(secure_runtime):
    with pytest.raises(SecurityBoundaryDenied, match="ungoverned state mutation: shared_world.perturb"):
        secure_runtime._world_model.perturb(magnitude=0.1, event_chance=0.0)


def test_the_cycle_applies_world_dynamics_through_the_governed_path(secure_runtime):
    seen: list = []
    secure_runtime._perturbation_queue.publish(WorldPerturbation(entity_id="e-1", description="road closed"))
    with patch(AUTHZ, side_effect=_allow(seen)):
        result = asyncio.run(secure_runtime._run_cycle())
    assert result.error is None
    # OPA was asked, once, about this action -- by the scheduler's own service principal
    assert seen == [{"action": WORLD_DYNAMICS_ACTION, "resource": "shared_world",
                     "principal": PLANETARY_SCHEDULER_PRINCIPAL, "principal_type": "service"}]
    assert len(secure_runtime._perturbation_queue) == 0                     # the report was reconciled
    assert any(e.entity_id == "e-1" for e in secure_runtime._world._events)


def test_the_scheduler_identity_and_commitment_do_not_leak(secure_runtime):
    async def cycle_then_look():
        with patch(AUTHZ, side_effect=_allow([])):
            await secure_runtime._run_cycle()
        return get_trusted_auth().authenticated, commitment_active()
    assert asyncio.run(cycle_then_look()) == (False, False)
    # and outside the governed task, a direct write is blocked again
    with pytest.raises(SecurityBoundaryDenied):
        secure_runtime._world_model.perturb(magnitude=0.1, event_chance=0.0)


def test_a_policy_denial_skips_world_dynamics_and_keeps_queued_reports(secure_runtime, caplog):
    async def deny(action, resource, extra, *, verified_delegation=None):
        raise SecurityBoundaryDenied("action_blocked", stage="AUTHZ")
    secure_runtime._perturbation_queue.publish(WorldPerturbation(entity_id="e-2", description="flood"))
    with patch(AUTHZ, side_effect=deny), patch.object(secure_runtime, "_apply_world_dynamics") as writes:
        result = asyncio.run(secure_runtime._run_cycle())
    assert result.error is None                                              # the cycle itself continues
    assert len(secure_runtime._perturbation_queue) == 1                      # nothing drained: next cycle
    writes.assert_not_called()                                               # no world dynamics written
    assert any("world dynamics denied at AUTHZ" in r.getMessage() for r in caplog.records)


def test_a_high_risk_listing_waits_for_a_human_instead_of_writing(secure_runtime, caplog):
    async def needs_human(action, resource, extra, *, verified_delegation=None):
        return {"allowed": True, "reason": "high_risk_action", "approval_mode": "HUMAN_APPROVAL_REQUIRED",
                "approval_source": "POLICY_AUTOMATIC", "risk_level": "MEDIUM", "policy_rule": "high_risk_action",
                "requires_hitl": True}
    with patch(AUTHZ, side_effect=needs_human), patch.object(secure_runtime, "_apply_world_dynamics") as writes:
        result = asyncio.run(secure_runtime._run_cycle())
    assert result.error is None
    writes.assert_not_called()
    assert any("await human approval" in r.getMessage() for r in caplog.records)
    assert issubclass(HumanApprovalRequired, Exception)


def test_the_isolated_runtime_loads_no_unrelated_actors(runtime):
    from src.monkey_brain.persistence.db_pool import get_db_pool

    assert get_db_pool().get_db().name == ISOLATED_DB
    assert get_db_pool().get_db()["actor_state"].count_documents({}) == 0      # nothing persisted to rehydrate
