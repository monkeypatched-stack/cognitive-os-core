"""How the control plane reaches an actor whose cognition runs in its own
Actor Runtime process (actor_runtime.py, drone-actor-deployment.yaml)
instead of in the control plane's own PlanetaryRuntime.

Such an actor is registered in the shared Actor Registry (Redis/Mongo) but
is never resident in the control plane's in-memory societies, so every
route that only searches pr.all_societies() 404s on it. This module is the
one place that decides (a) whether an actor lives on its own dedicated
process and (b) what address reaches it, so prompt forwarding, execute
proxying and the read routes (beliefs, goals) all agree.

Address resolution, in order:
  ACTOR_POD_URL_TEMPLATE  explicit override, "{actor_id}" substituted
                          (e.g. "http://cognitiveos-actor-{actor_id}:8051")
  in Kubernetes           the actor's Service FQDN (actor-deployment.yaml)
  otherwise               the bare Service/container name, which Docker
                          Compose networks resolve the same way
"""

from __future__ import annotations

import os
from typing import Any

ACTOR_POD_PORT = 8051


def is_dedicated_node(actor_id: str, node_id: str | None) -> bool:
    """True when a registry node_id names actor_id's own dedicated process
    (ACTOR_NODE_ID / COGNITIVEOS_NODE_ID = "cognitiveos-actor-<actor_id>...")."""
    return bool(node_id) and node_id.startswith(f"cognitiveos-actor-{actor_id}")


def dedicated_pod_node_id(planetary_runtime: Any, actor_id: str) -> str | None:
    """The dedicated node actor_id is placed on, else None. Only a yes/no
    signal -- the node_id itself (a Pod name that changes every restart) is
    never dialed.

    Checks the desired node first (ACTOR_CLAIM_PLACEMENT's deliberate
    claim), then the registry entry's node_id. The registry node_id alone is
    "whichever process last wrote this record" -- a control-plane restart
    could overwrite it and silently stop routing to the actor's runtime."""
    if planetary_runtime is None:
        return None
    get_desired = getattr(planetary_runtime, "get_actor_desired_node", None)
    desired = (get_desired(actor_id) or "") if callable(get_desired) else ""
    if is_dedicated_node(actor_id, desired):
        return desired
    locate_actor = getattr(planetary_runtime, "locate_actor", None)
    entry = locate_actor(actor_id) if callable(locate_actor) else None
    node_id = getattr(entry, "node_id", "") or ""
    return node_id if is_dedicated_node(actor_id, node_id) else None


def actor_pod_base_url(actor_id: str) -> str:
    template = os.getenv("ACTOR_POD_URL_TEMPLATE", "").strip()
    if template:
        return template.format(actor_id=actor_id).rstrip("/")
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        namespace = os.getenv("POD_NAMESPACE", "monkeybrain")
        return f"http://cognitiveos-actor-{actor_id}.{namespace}.svc.cluster.local:{ACTOR_POD_PORT}"
    return f"http://cognitiveos-actor-{actor_id}:{ACTOR_POD_PORT}"


class ActorPodRequestError(RuntimeError):
    """The actor's own process could not be reached, or answered non-2xx.
    status_code is the upstream status, or None when it was unreachable."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def actor_pod_request(
    actor_id: str,
    method: str,
    path: str,
    *,
    json: Any = None,
    timeout: float = 10.0,
) -> Any:
    """One authenticated call to actor_id's own Actor Runtime. Sends the
    X-Internal-Service-Token header its require_internal_service_token()
    checks. Raises ActorPodRequestError on any failure, never a fake result."""
    import httpx

    from src.monkey_brain.api.internal_auth import internal_service_headers

    url = f"{actor_pod_base_url(actor_id)}{path}"
    headers = internal_service_headers()
    if json is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, headers=headers, json=json)
    except httpx.RequestError as exc:
        raise ActorPodRequestError(f"could not reach {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise ActorPodRequestError(f"{method} {url} -> {resp.status_code}: {resp.text}", resp.status_code)
    return resp.json()
