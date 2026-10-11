"""The device registry contract between Actor Runtimes and the control plane.

The control plane owns the device registry (cognitive-os-control-plane:
src/monkey_brain/device_registry.py, api/routes/devices.py). Actor-side
processes -- an Actor Runtime, a drone's ROS bridge -- register the devices
they host through its HTTP API with the internal service token, and look
them up the same way; they never touch the registry's storage. This module
is both halves of that contract: the record (CameraIdentity) and the client.

The client reaches the control plane at COGNITIVEOS_CONTROL_PLANE_URL
(default http://127.0.0.1:8031 -- agentos on the same host, as on a RunPod
pod; http://agentos:8031 in Docker/Kubernetes; a tunnelled local port for
an edge actor).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass

import httpx

from src.monkey_brain.actor_contract.internal_auth import internal_service_headers

DEVICES_PATH = "/api/v1/agentos/devices"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class DeviceRegistryUnavailable(RuntimeError):
    """The control plane's device registry could not be reached or refused
    the request -- distinct from "no such device"."""


@dataclass(frozen=True)
class CameraIdentity:
    """The explicit actor_id <-> vehicle <-> ROS 2 namespace <-> LiveKit
    mapping for one drone's camera. Constructed once per drone where
    PX4_NAMESPACE/ACTOR_ID are assigned, never inferred from an incoming
    LiveKit event; every lookup is by the authoritative actor_id.

    The four identities a camera observation must never blur together:
        actor_id                      e.g. "drone-a"   (ActorCell.actor_id)
        ROS 2 namespace               e.g. "px4_1"     (PX4_NAMESPACE)
        LiveKit participant identity  e.g. "drone-a-camera"  (the publisher)
        LiveKit video track name      e.g. "drone-a-camera-track"
    """

    actor_id: str
    """CognitiveOS ActorCell.actor_id, e.g. "drone-a" -- the only key an
    identity is ever looked up by."""
    vehicle_id: str
    """PX4 vehicle identity, e.g. "px4/drone-a" -- human/log-facing, not
    used for routing."""
    ros_namespace: str
    """PX4_NAMESPACE, e.g. "px4_1" -- the same value the drone's
    Px4RosExecutionAdapter/ros_bridge_server was started with."""
    livekit_room: str
    """The LiveKit room this drone's camera publishes into."""
    livekit_participant_identity: str
    """The participant identity the ROS-camera bridge publishes under, e.g.
    "drone-a-camera" -- distinct from the operator's own identity."""
    camera_track_name: str
    """The LiveKit video track name, e.g. "drone-a-camera-track" -- how a
    subscriber tells this drone's camera apart in a room with several."""


def control_plane_url() -> str:
    return os.getenv("COGNITIVEOS_CONTROL_PLANE_URL", "http://127.0.0.1:8031").rstrip("/")


def _camera_url(actor_id: str) -> str:
    return f"{control_plane_url()}{DEVICES_PATH}/{actor_id}/camera"


def _headers(mutating: bool = False) -> dict[str, str]:
    headers = internal_service_headers()
    if mutating:
        # The registry's mutating routes are @idempotent; each call here is
        # a fresh intent (a re-register is a new PUT of the full record).
        headers["Idempotency-Key"] = uuid.uuid4().hex
    return headers


def _raise_unavailable(action: str, actor_id: str, exc: Exception | None = None, response=None) -> None:
    detail = f"HTTP {response.status_code}: {response.text[:200]}" if response is not None else str(exc)
    raise DeviceRegistryUnavailable(
        f"device registry {action} for {actor_id!r} failed at {control_plane_url()} ({detail})"
    ) from exc


def register_camera_identity(identity: CameraIdentity) -> None:
    """Register (or replace) this actor's camera with the control plane."""
    try:
        r = httpx.put(_camera_url(identity.actor_id), json=asdict(identity), headers=_headers(True), timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        _raise_unavailable("register", identity.actor_id, exc)
    if r.status_code >= 300:
        _raise_unavailable("register", identity.actor_id, response=r)


def unregister_camera_identity(actor_id: str) -> None:
    """Remove this actor's camera; unknown actors are not an error."""
    try:
        r = httpx.delete(_camera_url(actor_id), headers=_headers(True), timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        _raise_unavailable("unregister", actor_id, exc)
    if r.status_code >= 300 and r.status_code != 404:
        _raise_unavailable("unregister", actor_id, response=r)


def _identity_from(r: httpx.Response, actor_id: str) -> CameraIdentity | None:
    if r.status_code == 404:
        return None
    if r.status_code >= 300:
        _raise_unavailable("lookup", actor_id, response=r)
    return CameraIdentity(**r.json())


def get_camera_identity(actor_id: str) -> CameraIdentity | None:
    """This actor's registered camera, or None if it has none."""
    try:
        r = httpx.get(_camera_url(actor_id), headers=_headers(), timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        _raise_unavailable("lookup", actor_id, exc)
    return _identity_from(r, actor_id)


async def aget_camera_identity(actor_id: str) -> CameraIdentity | None:
    """get_camera_identity for async callers (route handlers)."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(_camera_url(actor_id), headers=_headers())
    except httpx.HTTPError as exc:
        _raise_unavailable("lookup", actor_id, exc)
    return _identity_from(r, actor_id)
