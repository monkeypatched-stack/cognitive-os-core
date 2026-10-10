"""Belief serialization shared by the control plane's GET /actors/{id}/beliefs
and the Actor Runtime's own GET /beliefs, so both return the same shape."""

from __future__ import annotations

from typing import Any


def serialize_beliefs(belief_state: Any) -> dict[str, Any]:
    """Flatten a BeliefState's entries into {subject: {confidence, predicate}}."""
    beliefs: dict[str, Any] = {}
    if belief_state is not None and hasattr(belief_state, "beliefs"):
        for entry in belief_state.beliefs:
            best = entry.best_hypothesis
            beliefs[entry.subject] = {
                "confidence": round(best.confidence, 3) if best else 0,
                "predicate": best.predicate if best else "",
            }
    return beliefs
