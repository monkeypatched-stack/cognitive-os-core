"""Seed data for the `roles` collection.

The bootstrap admin role carries every permission in INIT_PERMISSIONS —
matches scripts/seed_user_role_permissions.py's own stated intent ("1 admin
user -> 1 admin role -> all permissions"). The learner role carries only the
learning actor's learner-facing permissions: a learner's own progress,
study material and mastery cycles (the actor still limits them to their own
records), and nothing else the control plane serves.
"""

from __future__ import annotations

from src.mock.organizations.permissions import _PERMISSION_IDS

INIT_ROLES: list[dict] = [
    {
        "role_id": "ROLE-001",
        "name": "admin",
        "permissions": list(_PERMISSION_IDS),
        "parent_role_ids": [],
        "is_active": True,
    },
    {
        "role_id": "ROLE-002",
        "name": "learner",
        "permissions": ["perm-view-learning", "perm-execute-learning"],
        "parent_role_ids": [],
        "is_active": True,
    },
]
