"""LiveKit room-join token minting.

Shared infrastructure: the control plane mints browser tokens
(POST /livekit/token) and the actor's device runtimes mint tokens for the
participants they create (cognitive-os-actor devices/livekit_adapter.py).
Moved from devices/livekit_adapter.py so the control plane needn't import
device code.
"""

from __future__ import annotations

import os
from datetime import timedelta

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")


class LiveKitUnavailableError(RuntimeError):
    """The livekit packages aren't installed (pyproject.toml's 'livekit' extra)."""


def create_livekit_room_token(
    room: str,
    participant_identity: str,
    *,
    can_publish: bool = True,
    can_subscribe: bool = True,
    ttl_seconds: int = 120,
) -> str:
    """Mint a short-TTL, scope-bound LiveKit room-join token.

    Requires LIVEKIT_API_KEY/LIVEKIT_API_SECRET (the LiveKit project's own
    credentials, distinct from AGENTOS_MASTER_KEY — this token is verified
    by the LiveKit server, not by this codebase).
    """
    try:
        from livekit import api
    except ImportError as exc:
        raise LiveKitUnavailableError(
            "create_livekit_room_token requires the livekit-api package (pyproject.toml's 'livekit' extra)"
        ) from exc

    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise RuntimeError("LIVEKIT_API_KEY / LIVEKIT_API_SECRET must be set to mint a LiveKit room token")

    grants = api.VideoGrants(room_join=True, room=room, can_publish=can_publish, can_subscribe=can_subscribe)
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(participant_identity)
        .with_grants(grants)
        .with_ttl(timedelta(seconds=ttl_seconds))
    )
    return token.to_jwt()
