"""The contract between the control plane and an Actor Runtime.

    Control plane  (src/monkey_brain/api — gateway routes, auth, OPA,
                    registry/placement, proxying to actor pods)
          │  HTTP: X-Internal-Service-Token, /execute, /prompt, /beliefs
          ▼
    actor_contract (this package — shared, transport-level, no API deps)
          ▲
          │
    Actor          (src/monkey_brain/actor_runtime.py — hosts exactly one
                    actor's cognition on a cloud/edge/device/robot node)

Both sides import from here; neither imports the other. In particular the
Actor Runtime must never import ``src.monkey_brain.api`` — that package is
the control plane (its router, OPA wiring, gateway models) and drags the
whole control plane into every actor process. Enforced by
scripts/check_architecture_conformance.py (``actor_imports_control_plane``).
"""
