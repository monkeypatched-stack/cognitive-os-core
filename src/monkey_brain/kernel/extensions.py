"""Domain extension points.

The core never imports domain code. Domains (grocery, robotics,
manufacturing, ... -- all in the actor repo) plug in here instead: a domain
plugin is a module with a ``register(ext)`` function that appends hooks to
the lists below, and registers verticals (kernel/verticals.py), intents
(plan/intents/intent_registry.py), classifier examples
(plan/classifier/intent_examples) and confirmation pauses
(pipeline/confirmation_pauses.py) through those modules' own register
functions.

Plugins are found two ways, both optional:
  - COGNITIVEOS_DOMAIN_PLUGINS: comma-separated module paths
  - installed packages exposing the ``cognitiveos.domain_plugins`` entry point
load_domain_plugins() is idempotent; the kernel boot and PlanetaryRuntime
call it, so any process that runs actor cognition has its domains loaded.
A process with no plugins (the control plane) gets empty hook lists, and
every call site treats "no hooks" as "nothing domain-specific to add".
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("agentos.extensions")


@dataclass
class Extensions:
    # bootstrap: register a domain's agents with the runtime -- (runtime) -> None
    agent_registrars: list[Callable[[Any], None]] = field(default_factory=list)
    # bootstrap: load a domain package registry -- (app) -> registry | None
    domain_registry_loaders: list[Callable[[Any], Any]] = field(default_factory=list)
    # PlanetaryRuntime.register_actor: (planetary_runtime, actor_id, profile) -> None | awaitable
    actor_registered_hooks: list[Callable[[Any, str, Any], Any]] = field(default_factory=list)
    # PlanetaryRuntime federation create/restore: (planetary_runtime, federation_id, member_society_ids) -> None
    federation_listeners: list[Callable[[Any, str, Any], None]] = field(default_factory=list)
    # planning context: (planetary_runtime, actor_id, goal_text, activated_society_ids) -> dict merged into metadata
    planning_metadata_providers: list[Callable[[Any, str, str, set], dict]] = field(default_factory=list)
    # planning context: (knowledge_graph, goal_text) -> entities to rank first in relevant knowledge
    knowledge_candidate_providers: list[Callable[[Any, str], list]] = field(default_factory=list)
    # planning context: names offered to the planner -> narrowed names (e.g. a robot sees only its vehicle's)
    capability_filters: list[Callable[[list[str]], list[str]]] = field(default_factory=list)
    # prompt compilation (broca PromptCompiler): domain name -> that domain package's policies
    domain_policy_providers: list[Callable[[str], list[dict]]] = field(default_factory=list)
    # embedding registry construction: (EmbeddingRegistry) -> None (extra modalities)
    embedding_registrars: list[Callable[[Any], None]] = field(default_factory=list)
    # CognitiveKernel reporting: () -> report object / knowledge publisher
    kernel_report_factory: Callable[[], Any] | None = None
    knowledge_publisher_factory: Callable[[], Any] | None = None


extensions = Extensions()

_loaded: set[str] = set()
_lock = threading.Lock()


def _plugin_modules() -> list[str]:
    names = [n.strip() for n in os.getenv("COGNITIVEOS_DOMAIN_PLUGINS", "").split(",") if n.strip()]
    try:
        from importlib.metadata import entry_points

        names += [ep.value for ep in entry_points(group="cognitiveos.domain_plugins")]
    except Exception:
        logger.debug("entry point discovery failed", exc_info=True)
    return list(dict.fromkeys(names))


def load_domain_plugins() -> list[str]:
    """Import every configured domain plugin once and call its register().
    Returns the names loaded so far. A plugin that fails to import is
    logged and skipped -- the process keeps running with the domains that
    did load, same posture as the rest of the boot sequence."""
    with _lock:
        for name in _plugin_modules():
            if name in _loaded:
                continue
            _loaded.add(name)
            try:
                module = importlib.import_module(name.split(":")[0])
                register = getattr(module, "register", None)
                if callable(register):
                    register(extensions)
                logger.info("domain plugin loaded: %s", name)
            except Exception:
                logger.exception("domain plugin %s failed to load", name)
        return sorted(_loaded)


def loaded_domain_plugins() -> list[str]:
    return sorted(_loaded)
