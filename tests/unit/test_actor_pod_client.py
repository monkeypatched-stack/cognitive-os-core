"""kernel/edge/actor_pod_client.py: deciding whether an actor lives on its own
dedicated Actor Runtime, and what address reaches it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.monkey_brain.kernel.edge.actor_pod_client import (
    actor_pod_base_url,
    dedicated_pod_node_id,
    is_dedicated_node,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ACTOR_POD_URL_TEMPLATE", "KUBERNETES_SERVICE_HOST", "POD_NAMESPACE"):
        monkeypatch.delenv(name, raising=False)


def test_is_dedicated_node():
    assert is_dedicated_node("drone-a", "cognitiveos-actor-drone-a")
    assert is_dedicated_node("drone-a", "cognitiveos-actor-drone-a-7f9c-xk2p")
    assert not is_dedicated_node("drone-a", "cognitiveos-actor-drone-b")
    assert not is_dedicated_node("drone-a", "agentos-0")
    assert not is_dedicated_node("drone-a", "")
    assert not is_dedicated_node("drone-a", None)


def test_dedicated_pod_node_id_reads_registry():
    pr = SimpleNamespace(locate_actor=lambda actor_id: SimpleNamespace(node_id=f"cognitiveos-actor-{actor_id}"))
    assert dedicated_pod_node_id(pr, "drone-a") == "cognitiveos-actor-drone-a"

    local = SimpleNamespace(locate_actor=lambda actor_id: SimpleNamespace(node_id="agentos-0"))
    assert dedicated_pod_node_id(local, "drone-a") is None

    unknown = SimpleNamespace(locate_actor=lambda actor_id: None)
    assert dedicated_pod_node_id(unknown, "drone-a") is None
    assert dedicated_pod_node_id(None, "drone-a") is None


def test_dedicated_pod_node_id_prefers_desired_node():
    """A control-plane restart can overwrite the registry entry's node_id with
    its own; the desired-node claim still routes to the dedicated runtime."""
    pr = SimpleNamespace(
        get_actor_desired_node=lambda actor_id: f"cognitiveos-actor-{actor_id}",
        locate_actor=lambda actor_id: SimpleNamespace(node_id="node-508c394e2794"),
    )
    assert dedicated_pod_node_id(pr, "drone-a") == "cognitiveos-actor-drone-a"

    unplaced = SimpleNamespace(
        get_actor_desired_node=lambda actor_id: "",
        locate_actor=lambda actor_id: SimpleNamespace(node_id="node-508c394e2794"),
    )
    assert dedicated_pod_node_id(unplaced, "drone-a") is None


def test_base_url_defaults_to_service_name():
    assert actor_pod_base_url("drone-a") == "http://cognitiveos-actor-drone-a:8051"


def test_base_url_in_kubernetes(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert actor_pod_base_url("drone-a") == "http://cognitiveos-actor-drone-a.monkeybrain.svc.cluster.local:8051"
    monkeypatch.setenv("POD_NAMESPACE", "fleet")
    assert actor_pod_base_url("drone-a") == "http://cognitiveos-actor-drone-a.fleet.svc.cluster.local:8051"


def test_base_url_template_overrides(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("ACTOR_POD_URL_TEMPLATE", "http://{actor_id}.local:9000/")
    assert actor_pod_base_url("drone-a") == "http://drone-a.local:9000"
