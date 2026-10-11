"""Prompt-style response shapes for an actor execution result, shared by
the Actor Runtime (which runs the execution and returns these) and the
control plane (whose /prompt and approval/negotiation resume routes return
them to clients) -- so a client sees the same shape whichever side built it.
Moved from the control plane's api/routes/prompt.py.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


def result_value(result: Any) -> Any:
    if is_dataclass(result):
        return asdict(result)
    if isinstance(result, dict):
        return result
    return getattr(result, "__dict__", result)


def actor_query_result(question: str, actor_id: str, result: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Adapt the scheduler's actor result to the existing prompt response shape."""
    value = result_value(result)
    actions = value.get("actions", []) if isinstance(value, dict) else []
    outcome = value.get("actual_outcome", {}) if isinstance(value, dict) else {}
    achieved = bool(outcome.get("goal_achieved", False)) if isinstance(outcome, dict) else False
    answer = f"{question} executed through the planetary cycle"
    if achieved:
        answer += " successfully"

    # Real gap this closes: RespondToInquiryCapability (kernel/domains/
    # grocery.py) is Autonomous Dialogue's termination signal — its own
    # docstring says "An orchestrator watches for this action name to know
    # the conversation is over" and parameters["answer"] is the real,
    # planner-written natural-language answer to whoever asked the original
    # question. But this — the actual HTTP-facing orchestrator a caller's
    # answer comes back through — never watched for it at all: `answer`
    # above was always this generic "executed through the planetary cycle"
    # string, silently discarding the real answer every time (confirmed
    # live, repeatedly, across this session's own /prompt calls). plan.steps
    # and actions are parallel, same-order lists (one action per step) —
    # cross-reference by index to find which action, if any, was a
    # successful RespondToInquiry step, and surface ITS real answer instead.
    plan_steps = value.get("plan", {}).get("steps", []) if isinstance(value, dict) else []
    for index, action in enumerate(actions):
        if not isinstance(action, dict) or index >= len(plan_steps):
            continue
        step = plan_steps[index]
        step_action = step.get("action") if isinstance(step, dict) else None
        if step_action == "RespondToInquiry" and action.get("success"):
            respond_answer = (action.get("result") or {}).get("answer")
            if respond_answer:
                answer = respond_answer

    query_result = {
        "question": question,
        "answer": answer,
        "semantic_hits": [],
        "graph_paths": [],
        "citations": [],
        "llm_answered": True,
        "actor_id": actor_id,
        "actor_execution": value,
    }
    business_flow = {
        "question": question,
        "actor": actor_id,
        "flow": [
            {
                "step": index + 1,
                "action": (action.get("action_id", f"action-{index + 1}") if isinstance(action, dict) else str(action)),
                "result": action.get("result") if isinstance(action, dict) else None,
                "success": action.get("success") if isinstance(action, dict) else None,
            }
            for index, action in enumerate(actions)
        ],
        "result": {
            "actions_taken": len(actions),
            "goal_achieved": achieved,
        },
    }
    return query_result, business_flow
