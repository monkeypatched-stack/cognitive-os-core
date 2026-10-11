"""Intent registry: definitions and routes the UNDERSTAND phase resolves
questions to.

The core holds the mechanism and the platform intents (self-healing,
codegen, SittingFace workloads, specification discovery, and the
world-graph fallback). Domain intents -- work orders, batches, SOPs,
approvals, ... -- live in the actor repo and register here from their
domain plugin (kernel/extensions.py) through register_intent_definition(),
register_intent_route() and register_intent_metadata().

Routing order (INTENT_REGISTRY iteration order) is fixed: the platform
head intents first (self_healing_pipeline must be first -- the healing
sentinel takes priority; codegen before any domain intent), then domain
routes in registration order, then the platform tail, with
world_graph_traversal (matches anything) always LAST, then any routes
added at runtime (soma charts, auto-registered domain queries).
"""

from fastapi import logger
from src.monkey_brain.kernel.plan.intents.predicates.codegen import (
    codegen_question_answer,
    is_codegen_question,
)
from src.monkey_brain.kernel.plan.intents.predicates.sittingface_pipeline import (
    sittingface_pipeline_question_answer,
    is_sittingface_pipeline_question,
)
from src.monkey_brain.kernel.plan.intents.predicates.specification_discovery import (
    specification_discovery_question_answer,
    is_specification_discovery_question,
)
from src.monkey_brain.kernel.plan.intents.predicates.self_healing_pipeline import (
    self_healing_pipeline_question_answer,
    is_self_healing_question,
)
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(slots=True, frozen=True)
class IntentDefinition:
    intent: str

    description: str = ""

    required_inputs: list[str] = field(default_factory=list)

    required_outputs: list[str] = field(default_factory=list)

    default_augmentations: list[str] = field(default_factory=list)

    constraints: dict[str, Any] = field(default_factory=dict)

    min_confidence: float = 0.58

    # GoalType value (goal.py's GoalType enum) this intent resolves to.
    # Kept as a plain string here so intent_registry.py doesn't depend on
    # the kernel's goal module — goal.py converts via GoalType(...).
    goal_type: str = "query"


AUDIT_LOG_DEFINITION = IntentDefinition(
    intent="audit_log",
    required_inputs=["audit_id"],
    required_outputs=["audit_log"],
    min_confidence=0.52,
)


COUNT_DEFINITION = IntentDefinition(
    intent="count",
    required_inputs=["entity_type"],
    required_outputs=["count_result"],
    min_confidence=0.50,
)


SIMULATION_DEFINITION = IntentDefinition(
    intent="simulation",
    required_inputs=["scenario"],
    required_outputs=["simulation_result"],
    min_confidence=0.50,
)


SELF_HEALING_PIPELINE_DEFINITION = IntentDefinition(
    intent="self_healing_pipeline",
    required_inputs=["question", "errors"],
    required_outputs=["fixed_files", "heal_status"],
    min_confidence=0.10,
    goal_type="other",
)


SITTINGFACE_PIPELINE_DEFINITION = IntentDefinition(
    intent="sittingface_pipeline",
    required_inputs=["question"],
    required_outputs=["answer", "steps", "status"],
    min_confidence=0.40,
    goal_type="other",
)


KNOWLEDGE_BASE_DEFINITION = IntentDefinition(
    intent="knowledge_base",
    required_inputs=["topic"],
    required_outputs=["knowledge_result"],
    min_confidence=0.50,
)


INTENT_DEFINITIONS: dict[str, IntentDefinition] = {
    definition.intent: definition
    for definition in (
        AUDIT_LOG_DEFINITION,
        COUNT_DEFINITION,
        SIMULATION_DEFINITION,
        KNOWLEDGE_BASE_DEFINITION,
        SITTINGFACE_PIPELINE_DEFINITION,
    )
}


@dataclass(slots=True, frozen=True)
class IntentRoute:
    intent: str
    handler: Callable[..., Awaitable[Any]]
    predicate: Callable[[str], bool]
    min_confidence: float = 0.58
    question_source: str = "payload"
    top_k: bool = False


SELF_HEALING_PIPELINE_INTENT = IntentRoute(
    intent="self_healing_pipeline",
    handler=self_healing_pipeline_question_answer,
    predicate=is_self_healing_question,
    min_confidence=0.90,  # predicate-only intent; classifier should never match it
    question_source="visible",
)


CODEGEN_INTENT = IntentRoute(
    intent="codegen",
    handler=codegen_question_answer,
    predicate=is_codegen_question,
    min_confidence=0.30,
    question_source="visible",
)


SITTINGFACE_PIPELINE_INTENT = IntentRoute(
    intent="sittingface_pipeline",
    handler=sittingface_pipeline_question_answer,
    predicate=is_sittingface_pipeline_question,
    min_confidence=0.40,
    question_source="visible",
)


SPECIFICATION_DISCOVERY_INTENT = IntentRoute(
    intent="specification_discovery",
    handler=specification_discovery_question_answer,
    predicate=is_specification_discovery_question,
    min_confidence=0.30,
    question_source="visible",
)


async def world_graph_traversal_handler(question: str, **kwargs) -> dict:
    """Handle questions by traversing the world graph.

    This is the fallback handler for questions that don't match
    any specific intent. It searches the world graph for matching
    states and executes a cognitive cycle.
    """
    return {
        "question": question,
        "intent": "world_graph_traversal",
        "answer": f"Traversing world graph for: {question}",
        "handler": "world_graph_traversal",
    }



def is_world_graph_traversal_question(question: str) -> bool:
    """Match any question - this is the fallback predicate.

    This predicate always returns True, so it must be LAST in INTENT_REGISTRY.
    It catches all questions that don't match other specific predicates.
    """
    return True



WORLD_GRAPH_TRAVERSAL_INTENT = IntentRoute(
    intent="world_graph_traversal",
    handler=world_graph_traversal_handler,
    predicate=is_world_graph_traversal_question,
    min_confidence=0.0,  # Always matches
    question_source="payload",
)



_HEAD_ROUTES: tuple[IntentRoute, ...] = (
    SELF_HEALING_PIPELINE_INTENT,  # must be first — healing sentinel takes priority
    CODEGEN_INTENT,  # before any domain intent — "generate service" must not fall to LLM
)
_TAIL_ROUTES: tuple[IntentRoute, ...] = (
    SITTINGFACE_PIPELINE_INTENT,  # ETASS-specific keywords
    SPECIFICATION_DISCOVERY_INTENT,
    WORLD_GRAPH_TRAVERSAL_INTENT,  # MUST BE LAST of the static routes — fallback for unmatched questions
)
_domain_routes: dict[str, IntentRoute] = {}
_runtime_routes: dict[str, IntentRoute] = {}

# The live routing table. Mutated in place (never rebound) — intent_router.py
# and others import this dict object directly.
INTENT_REGISTRY: dict[str, IntentRoute] = {}


def _rebuild_intent_registry() -> None:
    ordered: dict[str, IntentRoute] = {}
    for route in _HEAD_ROUTES:
        ordered[route.intent] = route
    for name, route in _domain_routes.items():
        ordered.setdefault(name, route)
    for route in _TAIL_ROUTES:
        ordered.setdefault(route.intent, route)
    for name, route in _runtime_routes.items():
        ordered.setdefault(name, route)
    INTENT_REGISTRY.clear()
    INTENT_REGISTRY.update(ordered)


_rebuild_intent_registry()


def register_intent_definition(definition: IntentDefinition) -> None:
    """Called by a domain plugin to add an IntentDefinition."""
    INTENT_DEFINITIONS[definition.intent] = definition


def register_intent_route(route: IntentRoute) -> None:
    """Called by a domain plugin to add a route. Domain routes keep their
    registration order, between the platform head and tail routes."""
    _domain_routes[route.intent] = route
    _rebuild_intent_registry()


def get_intent_definition(
    intent_name: str | None,
) -> IntentDefinition:
    return INTENT_DEFINITIONS.get(
        intent_name or "",
        IntentDefinition(intent="generic"),
    )


# required_outputs / goal_type for intents that are only in INTENT_REGISTRY
# (routable) but have no IntentDefinition entry — free-text search, a
# handful of registry-only intents, and whatever domains register through
# register_intent_metadata() (e.g. facility-hierarchy sub-lookups).
_EXTRA_REQUIRED_OUTPUTS: dict[str, list[str]] = {
    "document_search": ["documents"],
    "web_search": ["search_results"],
}

_EXTRA_GOAL_TYPES: dict[str, str] = {
    "document_search": "search",
    "web_search": "search",
    "self_healing_pipeline": "other",
    "codegen": "create",
    "specification_discovery": "create",
}


def register_intent_metadata(
    intent_name: str, required_outputs: list[str] | None = None, goal_type: str | None = None
) -> None:
    """Called by a domain plugin for a registry-only intent with no
    IntentDefinition (see resolve_required_outputs / resolve_goal_type)."""
    if required_outputs is not None:
        _EXTRA_REQUIRED_OUTPUTS[intent_name] = list(required_outputs)
    if goal_type is not None:
        _EXTRA_GOAL_TYPES[intent_name] = goal_type


def resolve_required_outputs(intent_name: str) -> list[str]:
    """required_outputs for `intent_name`: its IntentDefinition if registered,
    else the extra table above, else `[intent_name]` as a last resort."""
    intent_def = INTENT_DEFINITIONS.get(intent_name)
    if intent_def:
        return list(intent_def.required_outputs)
    return _EXTRA_REQUIRED_OUTPUTS.get(intent_name, [intent_name])



def resolve_goal_type(intent_name: str) -> str:
    """goal_type string for `intent_name`: its IntentDefinition if registered,
    else the extra table above, defaulting to "query"."""
    intent_def = INTENT_DEFINITIONS.get(intent_name)
    if intent_def:
        return intent_def.goal_type
    return _EXTRA_GOAL_TYPES.get(intent_name, "query")



# ---------------------------------------------------------------------------
# Dynamic intent registration from soma charts
# ---------------------------------------------------------------------------

_somatic_compiler = None



def set_somatic_compiler(compiler) -> None:
    """Wire the SomaticCompiler so the executor can register intents from charts."""
    global _somatic_compiler
    _somatic_compiler = compiler



def get_somatic_compiler():
    """The SomaticCompiler singleton — SittingFace's knowledge-base repo.

    Used by CognitiveRuntime.explore_knowledge_base() to query SittingFace
    as an external repo it reads from, rather than an owned subsystem.
    """
    return _somatic_compiler



def lookup_or_register_from_soma(intent_name: str) -> IntentRoute | None:
    """If intent_name matches a soma chart, register it as a sittingface route.

    Called by the executor when a classified intent has no static handler.
    Returns the newly registered route, or None if no chart matches.
    """
    if _somatic_compiler is None or not intent_name:
        return None

    chart_names = {c.name for c in getattr(_somatic_compiler, "charts", [])}
    if intent_name not in chart_names:
        return None

    from src.monkey_brain.kernel.plan.intents.predicates.sittingface_workload import (
        sittingface_workload_question_answer,
    )

    route = IntentRoute(
        intent=intent_name,
        handler=sittingface_workload_question_answer,
        predicate=lambda q: True,
        min_confidence=0.40,
        question_source="visible",
    )
    _runtime_routes[intent_name] = route
    _rebuild_intent_registry()
    return route



def register_domain_intent(domain: str, entity_name: str = "") -> IntentRoute | None:
    """Auto-register a new intent for an unseen domain.

    When a new domain is encountered (e.g., 'legal', 'entertainment'),
    this function registers a new intent in INTENT_REGISTRY so that
    future questions about this domain can be handled properly.

    Args:
        domain: The domain name (e.g., 'legal', 'entertainment')
        entity_name: Optional entity name (e.g., 'lawyer', 'music')

    Returns:
        The newly registered IntentRoute, or None if registration failed.
    """
    intent_name = f"{domain}_query"

    # Don't register if already exists
    if intent_name in INTENT_REGISTRY:
        return INTENT_REGISTRY[intent_name]

    # Create predicate that matches questions about this domain
    def domain_predicate(question: str) -> bool:
        question_lower = question.lower()
        # Match if domain name or entity name is in the question
        if domain.lower() in question_lower:
            return True
        if entity_name and entity_name.lower() in question_lower:
            return True
        return False

    # Create handler for this domain
    async def domain_handler(question: str, **kwargs) -> dict:
        return {
            "question": question,
            "intent": intent_name,
            "domain": domain,
            "entity": entity_name,
            "answer": f"Processing {domain} request: {question}",
            "handler": "domain_handler",
        }

    # Register the intent
    route = IntentRoute(
        intent=intent_name,
        handler=domain_handler,
        predicate=domain_predicate,
        min_confidence=0.5,
        question_source="payload",
    )

    _runtime_routes[intent_name] = route
    _rebuild_intent_registry()
    logger.info(f"Registered new domain intent: {intent_name}")

    return route
