"""Change-control records: building a change control from a request
payload (approval chain, impact assessment, decisions, graph relationships)
and creating its proposed-change nodes.

Shared by the auth service's /compliance/change-controls route
(cognitive-os-control-plane) and the PM service's SOP change controls
(cognitive-os-domains), which used to import these from the
auth router directly. Moved verbatim from services/auth/routers/auth.py.
"""

import re
from datetime import datetime
from uuid import uuid4

from bson import ObjectId
from fastapi import HTTPException, status
from fastapi.encoders import jsonable_encoder

from services.common.approval_chains import (
    approval_graph_relationships,
    approval_step_email,
    resolve_named_approval_chain,
    validate_named_approval_chain,
)
from services.common.compliance import (
    CHANGE_CONTROL_COLLECTION,
    RECORD_ID_KEYS,
    compliance_readiness,
    sha256_hex,
    utc_now,
)

PROPOSED_CHANGE_COLLECTION = "gxp_proposed_changes"


def _bson_safe(value):
    return jsonable_encoder(value, custom_encoder={ObjectId: str})


def _record_lookup_query(record_id: str) -> dict:
    query = {"$or": [{key: record_id} for key in RECORD_ID_KEYS]}
    try:
        if ObjectId.is_valid(record_id):
            query["$or"].append({"_id": ObjectId(record_id)})
    except Exception:
        pass
    return query


def _proposed_values_from_payload(payload: dict) -> dict:
    proposed_values = payload.get("proposed_values")
    if isinstance(proposed_values, dict) and proposed_values:
        return dict(proposed_values)
    proposed_update = payload.get("proposed_update")
    if isinstance(proposed_update, dict):
        set_values = proposed_update.get("$set")
        if isinstance(set_values, dict) and set_values:
            return dict(set_values)
        if proposed_update and not any(
            str(key).startswith("$") for key in proposed_update
        ):
            return dict(proposed_update)
    return {}


def _record_key_values(record: dict) -> dict:
    keys = [
        "_id",
        *RECORD_ID_KEYS,
    ]
    return {key: record.get(key) for key in keys if record.get(key) is not None}


async def _create_proposed_change_nodes(
    db, change_control: dict, current_user: dict
) -> list[dict]:
    proposed_nodes: list[dict] = []
    now = utc_now()
    requested_changes = change_control.get("proposed_changes")
    proposed_values = _proposed_values_from_payload(change_control)
    affected_entities = change_control.get("affected_entities") or []
    if isinstance(requested_changes, list) and requested_changes:
        proposed_specs = requested_changes
    else:
        proposed_specs = [
            {
                "collection": entity.get("collection"),
                "entity_id": entity.get("entity_id"),
                "entity_name": entity.get("name"),
                "proposed_values": proposed_values,
            }
            for entity in affected_entities
            if entity.get("collection") and entity.get("entity_id")
        ]

    for index, spec in enumerate(proposed_specs, start=1):
        if not isinstance(spec, dict):
            continue
        collection = spec.get("collection")
        entity_id = spec.get("entity_id") or spec.get("id") or spec.get("record_id")
        if not collection or not entity_id:
            continue
        live_record = await db[str(collection)].find_one(
            _record_lookup_query(str(entity_id)), {"_id": 0, "embedding": 0}
        )
        pending_values = (
            spec.get("proposed_values")
            if isinstance(spec.get("proposed_values"), dict)
            else proposed_values
        )
        node = {
            "proposed_change_id": f"proposed_change-{uuid4().hex}",
            "change_control_id": change_control.get("change_control_id"),
            "change_control_number": change_control.get("change_control_number"),
            "sequence": index,
            "status": "Pending Approval",
            "lifecycle_state": "proposed",
            "source_collection": collection,
            "source_id": str(entity_id),
            "source_name": spec.get("entity_name")
            or (live_record or {}).get("name")
            or (live_record or {}).get("title")
            or str(entity_id),
            "source_key": _record_key_values(live_record or {"id": entity_id}),
            "source_snapshot": live_record or {},
            "pending_properties": dict(pending_values or {}),
            "proposed_values": dict(pending_values or {}),
            "archived": False,
            "rolled_back": False,
            "live_node_touched": False,
            "approval_chain": change_control.get("approval_chain") or [],
            "approval_assignment_basis": change_control.get(
                "approval_assignment_basis"
            ),
            "created_at": now,
            "updated_at": now,
            "created_by": _current_user_id(current_user),
            "updated_by": _current_user_id(current_user),
            "effective_at": None,
        }
        node["record_hash"] = sha256_hex(node)
        await db[PROPOSED_CHANGE_COLLECTION].insert_one(node)
        proposed_nodes.append(_bson_safe(node))
    return proposed_nodes


def _current_user_id(current_user: dict) -> str:
    return current_user.get("user_id") or current_user.get("sub")


OPEN_CHANGE_STATUSES = {
    "Open",
    "Draft",
    "Submitted",
    "Under Review",
    "Approved",
    "Implementation",
    "Verification",
}


def _change_control_number(now: datetime, sequence: int) -> str:
    return f"SX-CHG-{now:%Y}-{sequence:04d}"


def _entity_collection(entity: dict) -> str | None:
    collection = entity.get("collection") or entity.get("entity_collection")
    if collection:
        collection_name = str(collection)
        return {
            "stages": "industrial_stages",
            "stage": "industrial_stages",
            "lines": "industrial_lines",
            "line": "industrial_lines",
            "plants": "industrial_plants",
            "plant": "industrial_plants",
            "machines": "pharmaceutical_machines",
            "equipment": "pharmaceutical_equipment",
        }.get(collection_name, collection_name)
    entity_type = (
        str(entity.get("type") or entity.get("entity_type") or "").strip().lower()
    )
    return {
        "machine": "pharmaceutical_machines",
        "equipment": "pharmaceutical_equipment",
        "workstation": "workstations",
        "sop": "sops",
        "process": "industrial_stages",
        "stage": "industrial_stages",
        "line": "industrial_lines",
        "plant": "industrial_plants",
        "material": "inventory_items",
        "recipe": "recipe_routes",
    }.get(entity_type)


def _entity_id(entity: dict) -> str | None:
    for key in (
        "entity_id",
        "id",
        "machine_id",
        "equipment_id",
        "workstation_id",
        "sop_id",
        "stage_id",
        "line_id",
        "plant_id",
        "recipe_id",
    ):
        value = entity.get(key)
        if value:
            return str(value)
    return None


async def _open_change_for_affected_entities(
    db, affected_entities: list[dict]
) -> dict | None:
    entity_ids = [_entity_id(entity) for entity in affected_entities]
    entity_ids = [entity_id for entity_id in entity_ids if entity_id]
    if not entity_ids:
        return None
    return await db[CHANGE_CONTROL_COLLECTION].find_one(
        {
            "status": {"$in": list(OPEN_CHANGE_STATUSES)},
            "affected_entities.entity_id": {"$in": entity_ids},
        },
        {"_id": 0},
    )


async def _resolve_affected_entities(db, affected_entities: list[dict]) -> list[dict]:
    resolved: list[dict] = []
    for entity in affected_entities:
        entity_id = _entity_id(entity)
        collection = _entity_collection(entity)
        record = None
        if collection and entity_id:
            entity_name = str(entity.get("name") or "").strip()
            name_query = []
            if entity_name:
                name_query = [
                    {
                        "name": {
                            "$regex": f"^{re.escape(entity_name)}$",
                            "$options": "i",
                        }
                    },
                    {
                        "title": {
                            "$regex": f"^{re.escape(entity_name)}$",
                            "$options": "i",
                        }
                    },
                ]
            record = await db[collection].find_one(
                {
                    "$or": [
                        {"id": entity_id},
                        {"entity_id": entity_id},
                        {"machine_id": entity_id},
                        {"equipment_id": entity_id},
                        {"workstation_id": entity_id},
                        {"sop_id": entity_id},
                        {"stage_id": entity_id},
                        {"line_id": entity_id},
                        {"plant_id": entity_id},
                    ]
                    + name_query
                },
                {"_id": 0, "embedding": 0},
            )
            if record:
                entity_id = (
                    record.get("id")
                    or record.get("entity_id")
                    or record.get("machine_id")
                    or record.get("equipment_id")
                    or record.get("workstation_id")
                    or record.get("sop_id")
                    or record.get("stage_id")
                    or record.get("line_id")
                    or record.get("plant_id")
                    or entity_id
                )
        resolved.append(
            {
                **entity,
                "entity_id": entity_id,
                "collection": collection,
                "name": entity.get("name")
                or (record or {}).get("name")
                or (record or {}).get("title")
                or entity_id,
                "status": entity.get("status") or (record or {}).get("status"),
                "resolved": bool(record),
            }
        )
    return resolved


async def _change_impact_assessment(db, affected_entities: list[dict]) -> dict:
    entity_ids = [
        str(entity.get("entity_id"))
        for entity in affected_entities
        if entity.get("entity_id")
    ]
    machine_ids = [
        entity["entity_id"]
        for entity in affected_entities
        if entity.get("collection") == "pharmaceutical_machines"
        and entity.get("entity_id")
    ]
    equipment_ids = [
        entity["entity_id"]
        for entity in affected_entities
        if entity.get("collection") == "pharmaceutical_equipment"
        and entity.get("entity_id")
    ]
    workstation_ids = [
        entity["entity_id"]
        for entity in affected_entities
        if entity.get("collection") == "workstations" and entity.get("entity_id")
    ]
    stage_ids = [
        entity["entity_id"]
        for entity in affected_entities
        if entity.get("collection") == "industrial_stages" and entity.get("entity_id")
    ]

    sops = (
        await db["sops"]
        .find(
            {
                "$or": [
                    {"entity_id": {"$in": entity_ids}},
                    {"machine_id": {"$in": machine_ids}},
                    {"equipment_id": {"$in": equipment_ids}},
                    {"workstation_id": {"$in": workstation_ids}},
                    {"stage_id": {"$in": stage_ids}},
                ]
            },
            {
                "_id": 0,
                "id": 1,
                "sop_id": 1,
                "title": 1,
                "name": 1,
                "entity_name": 1,
                "workstation_id": 1,
                "stage_id": 1,
                "line_id": 1,
                "plant_id": 1,
            },
        )
        .to_list(length=100)
    )
    work_orders = (
        await db["work_orders"]
        .find(
            {
                "$or": [
                    {"machine_id": {"$in": machine_ids}},
                    {"equipment_id": {"$in": equipment_ids}},
                    {"workstation_id": {"$in": workstation_ids}},
                    {"status": {"$in": ["Open", "In Progress", "Scheduled"]}},
                ]
            },
            {"_id": 0, "work_order_id": 1, "title": 1, "status": 1},
        )
        .to_list(length=100)
    )
    operators = (
        await db["users"]
        .find(
            {
                "$or": [
                    {"workstation_id": {"$in": workstation_ids}},
                    {"stage_id": {"$in": stage_ids}},
                ],
                "title": "Production Worker",
            },
            {"_id": 0, "user_id": 1, "name": 1, "workstation_id": 1, "stage_id": 1},
        )
        .to_list(length=100)
    )
    open_batches = (
        await db["batch_records"]
        .find(
            {"status": {"$in": ["Open", "In Progress", "Released"]}},
            {"_id": 0, "batch_id": 1, "status": 1},
        )
        .to_list(length=100)
    )

    return {
        "directly_affected_entities": affected_entities,
        "connected_sops": sops,
        "linked_work_orders": work_orders,
        "qualified_operators": operators,
        "open_batches": open_batches,
        "downstream_compliance_records": [],
        "flags": {
            "validated_system_review_required": any(
                entity.get("collection")
                in {"pharmaceutical_machines", "pharmaceutical_equipment", "sops"}
                for entity in affected_entities
            ),
            "quality_manager_signoff_required": True,
            "asset_hold_required": bool(machine_ids or equipment_ids),
        },
    }


def _change_decisions(payload: dict, impact: dict) -> dict:
    change_type = payload.get("change_type")
    risk_score = payload.get("risk_score") or 0
    major_or_risky = change_type in {"Major", "Emergency"} or risk_score >= 40
    sop_related = any(
        entity.get("collection") == "sops"
        for entity in payload.get("affected_entities") or []
    )
    process_related = any(
        entity.get("collection") in {"industrial_stages", "recipe_routes"}
        for entity in payload.get("affected_entities") or []
    )
    return {
        "revalidation_required": (
            payload.get("revalidation_required")
            if payload.get("revalidation_required") is not None
            else major_or_risky
        ),
        "requalification_required": (
            payload.get("requalification_required")
            if payload.get("requalification_required") is not None
            else impact["flags"]["asset_hold_required"]
        ),
        "sop_revision_required": (
            payload.get("sop_revision_required")
            if payload.get("sop_revision_required") is not None
            else sop_related or process_related
        ),
        "retraining_required": (
            payload.get("retraining_required")
            if payload.get("retraining_required") is not None
            else sop_related or process_related
        ),
        "regulatory_notification_required": (
            payload.get("regulatory_notification_required")
            if payload.get("regulatory_notification_required") is not None
            else change_type == "Major" and risk_score >= 60
        ),
        "stability_study_required": (
            payload.get("stability_study_required")
            if payload.get("stability_study_required") is not None
            else False
        ),
    }


def _implementation_tasks(payload: dict, impact: dict, decisions: dict) -> list[dict]:
    tasks = [
        {
            "task_id": f"task-{uuid4().hex}",
            "title": "Review graph-generated impact assessment",
            "owner_role": "Reviewer",
            "status": "Pending",
            "evidence_required": False,
        },
        {
            "task_id": f"task-{uuid4().hex}",
            "title": "Implement approved change",
            "owner_role": "Implementer",
            "status": "Pending",
            "evidence_required": True,
        },
        {
            "task_id": f"task-{uuid4().hex}",
            "title": "Verify implementation effectiveness",
            "owner_role": "Verifier",
            "status": "Pending",
            "evidence_required": True,
        },
    ]
    if decisions.get("sop_revision_required"):
        tasks.append(
            {
                "task_id": f"task-{uuid4().hex}",
                "title": "Revise affected SOP/document",
                "owner_role": "Document Owner",
                "status": "Pending",
                "evidence_required": True,
            }
        )
    if decisions.get("retraining_required"):
        tasks.append(
            {
                "task_id": f"task-{uuid4().hex}",
                "title": "Complete affected-operator retraining",
                "owner_role": "Training Coordinator",
                "status": "Pending",
                "evidence_required": True,
            }
        )
    if decisions.get("requalification_required"):
        tasks.append(
            {
                "task_id": f"task-{uuid4().hex}",
                "title": "Complete equipment requalification or calibration evidence",
                "owner_role": "Engineering",
                "status": "Pending",
                "evidence_required": True,
            }
        )
    return tasks


def _approval_user_summary(user: dict | None, fallback_role: str) -> dict:
    if not user:
        return {
            "user_id": None,
            "name": None,
            "title": fallback_role,
            "role": fallback_role,
            "email": None,
        }
    fallback_email = (
        f"{str(user.get('user_id')).lower()}@internal.example"
        if user.get("user_id")
        else None
    )
    return {
        "user_id": user.get("user_id"),
        "name": user.get("name") or user.get("email") or user.get("user_id"),
        "title": user.get("title") or fallback_role,
        "role": user.get("role") or fallback_role,
        "email": user.get("email") or fallback_email,
    }


async def _approval_user_by_id(db, user_id: str | None) -> dict | None:
    if not user_id:
        return None
    return await db["users"].find_one({"user_id": user_id}, {"_id": 0, "embedding": 0})


async def _approval_user_by_dialog_step(db, step: dict) -> dict | None:
    assigned_user = (
        step.get("assigned_user") if isinstance(step.get("assigned_user"), dict) else {}
    )
    user_id = (
        step.get("assigned_user_id")
        or step.get("user_id")
        or step.get("approver_id")
        or assigned_user.get("user_id")
    )
    user = await _approval_user_by_id(db, str(user_id) if user_id else None)
    if user:
        return user
    email = (
        step.get("assigned_user_email")
        or step.get("email")
        or assigned_user.get("email")
    )
    name = (
        step.get("assigned_user_name")
        or step.get("name")
        or step.get("approver_name")
        or assigned_user.get("name")
    )
    clauses = []
    if email:
        clauses.append(
            {"email": {"$regex": f"^{re.escape(str(email).strip())}$", "$options": "i"}}
        )
    if name:
        clauses.append(
            {"name": {"$regex": f"^{re.escape(str(name).strip())}$", "$options": "i"}}
        )
    if not clauses:
        return None
    return await db["users"].find_one({"$or": clauses}, {"_id": 0, "embedding": 0})


def _manual_chain_from_payload(payload: dict) -> list[dict]:
    chain = (
        payload.get("approval_chain")
        if isinstance(payload.get("approval_chain"), list)
        else []
    )
    if not chain:
        chain = (
            payload.get("manual_approval_chain")
            if isinstance(payload.get("manual_approval_chain"), list)
            else []
        )
    return chain


async def _resolve_manual_approval_chain(
    db, chain: list[dict], basis: dict
) -> list[dict]:
    normalized: list[dict] = []
    for index, step in enumerate(chain, start=1):
        if not isinstance(step, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Approval step {index} must be an object.",
            )
        user = await _approval_user_by_dialog_step(db, step)
        if not user:
            label = step.get("stage") or step.get("step") or step.get("role") or index
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Approval step '{label}' must resolve to an existing named worker node.",
            )
        role = (
            step.get("role")
            or step.get("required_role")
            or step.get("assigned_user_title")
            or user.get("title")
            or "Approver"
        )
        stage = step.get("stage") or step.get("step") or f"{role} Approval"
        assigned_user = _approval_user_summary(user, str(role))
        normalized.append(
            {
                **step,
                "sequence": int(step.get("sequence") or index),
                "stage": stage,
                "step": stage,
                "status": step.get("status") or "Pending",
                "role": role,
                "required_role": step.get("required_role") or role,
                "required_roles": step.get("required_roles") or [role],
                "assigned_user": assigned_user,
                "assigned_user_id": assigned_user.get("user_id"),
                "assigned_user_name": assigned_user.get("name"),
                "assigned_user_title": assigned_user.get("title"),
                "assigned_user_email": assigned_user.get("email")
                or step.get("assigned_user_email"),
                "minimum_approvals": step.get("minimum_approvals") or 1,
                "routing": step.get("routing") or "sequential",
                "source": step.get("source") or "manual_dialog",
                "context": step.get("context") or basis,
            }
        )
    normalized.sort(key=lambda item: int(item.get("sequence") or 0))
    for index, step in enumerate(normalized, start=1):
        step["sequence"] = index
    validate_named_approval_chain(normalized)
    missing_email = [
        step.get("assigned_user_name") or step.get("assigned_user_id")
        for step in normalized
        if not approval_step_email(step)
    ]
    if missing_email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Approval step approvers must have email addresses: {', '.join(str(item) for item in missing_email)}",
        )
    return normalized


def _change_control_graph_relationships(
    resolved_entities: list[dict], workflow: list[dict], basis: dict
) -> list[dict]:
    relationships = [
        {
            "type": "AFFECTS",
            "target_collection": entity.get("collection"),
            "target_id": entity.get("entity_id"),
        }
        for entity in resolved_entities
        if entity.get("entity_id")
    ]
    relationships.extend(approval_graph_relationships(workflow, basis))
    return relationships


async def _build_change_control_record(db, payload: dict, current_user: dict) -> dict:
    now = utc_now()
    manual_chain = _manual_chain_from_payload(payload)
    existing = await _open_change_for_affected_entities(
        db, payload.get("affected_entities") or []
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Open change already exists for an affected entity: {existing.get('change_control_number') or existing.get('change_control_id')}",
        )
    resolved_entities = await _resolve_affected_entities(
        db, payload.get("affected_entities") or []
    )
    payload["affected_entities"] = resolved_entities
    impact = await _change_impact_assessment(db, resolved_entities)
    decisions = _change_decisions(payload, impact)
    sequence = await db[CHANGE_CONTROL_COLLECTION].count_documents({}) + 1
    record_id = f"change_control-{uuid4().hex}"
    approval_workflow, approval_assignment_basis = await resolve_named_approval_chain(
        db,
        source_collection=CHANGE_CONTROL_COLLECTION,
        source_id=record_id,
        source_document={**payload, "change_control_id": record_id},
        affected_entities=resolved_entities,
        current_user_id=_current_user_id(current_user),
    )
    if manual_chain:
        approval_workflow = await _resolve_manual_approval_chain(
            db, manual_chain, approval_assignment_basis
        )
        approval_assignment_basis = {
            **approval_assignment_basis,
            "mode": "manual_dialog_assignment",
            "manual_dialog_assignment": True,
            "auto_chain_available": True,
            "manual_chain_count": len(approval_workflow),
        }
    record = {
        "change_control_id": record_id,
        "change_control_number": _change_control_number(now, sequence),
        "standard": "GxP",
        "standards": compliance_readiness()["standards"],
        "standard_id": "ich-q10",
        **payload,
        **decisions,
        "initiating_date": now,
        "initiating_user": _current_user_id(current_user),
        "impact_assessment_generated": impact,
        "implementation_tasks": _implementation_tasks(payload, impact, decisions),
        "training_records": [
            {
                "training_record_id": f"training-{uuid4().hex}",
                "worker_id": operator.get("user_id"),
                "worker_name": operator.get("name"),
                "status": "Pending",
            }
            for operator in impact.get("qualified_operators", [])
        ],
        "approval_chain": approval_workflow,
        "approval_workflow": approval_workflow,
        "approval_process_definition": approval_workflow,
        "approval_assignment_basis": approval_assignment_basis,
        "approval_chain_resolved": True,
        "approval_chain_resolved_at": now,
        "electronic_signatures": [],
        "effectiveness_check": {
            "required": True,
            "window_days": payload.get("effectiveness_check_days")
            or (30 if payload.get("change_type") == "Emergency" else 14),
            "status": "Pending",
        },
        "graph_relationships": _change_control_graph_relationships(
            resolved_entities, approval_workflow, approval_assignment_basis
        ),
        "status": payload.get("status") or "Open",
        "created_at": now,
        "updated_at": now,
        "created_by": _current_user_id(current_user),
        "updated_by": _current_user_id(current_user),
    }
    record["record_hash"] = sha256_hex(record)
    return record


# The entry points other services use.
build_change_control_record = _build_change_control_record
create_proposed_change_nodes = _create_proposed_change_nodes
