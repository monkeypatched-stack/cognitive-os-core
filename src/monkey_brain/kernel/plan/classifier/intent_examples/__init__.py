"""Training examples for the embedding intent classifier
(embed_classifier.py), keyed by intent.

Empty in the core: the examples are domain content (manufacturing work
orders, batch records, SOPs, ...) and live in the actor repo, whose domain
plugin adds them with register_intent_examples() at startup
(kernel/extensions.py). The classifier reads INTENT_EXAMPLES when it is
built, so plugins must load before the first classification.
"""

from __future__ import annotations

from typing import Iterable, Mapping

# Mutated in place (never rebound) -- embed_classifier.py imports this dict.
INTENT_EXAMPLES: dict[str, list[str]] = {}


def register_intent_examples(fragments: Iterable[Mapping[str, list[str]]]) -> None:
    """Merge example fragments, concatenating lists for intents that were
    split across several fragments."""
    for fragment in fragments:
        for intent, examples in fragment.items():
            INTENT_EXAMPLES.setdefault(intent, []).extend(examples)
