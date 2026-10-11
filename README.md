# CognitiveOS Core

The shared foundation of CognitiveOS: the kernel (observe → believe → plan →
predict → govern → execute → compare → learn), the society and planetary
runtimes, actor contracts, and the platform services the other repos build on.
It is a library. It runs neither the control plane nor any actor by itself.

CognitiveOS is split across four repositories, checked out side by side:

```text
Code/
├── cognitive-os-core/                    ← this repo: kernel, society, contracts
├── cognitive-os-control-plane/           Control API, scheduler, auth, gateway (Kong), UI
├── cognitive-os-actor/                   Actor Runtime, domains, devices, domain API service
└── coginitive-os-manufacturing/          manufacturing domain microservices
```

The other three depend on this repo. Locally they find it as the sibling
folder `../cognitive-os-core` (their `tests/conftest.py`, `pyproject.toml`
`[tool.uv.sources]`, and Docker `core` build context), or wherever
`COGNITIVEOS_CORE_DIR` points.

## What lives here

| Path | Contents |
|---|---|
| `src/monkey_brain/kernel/` | cognitive pipeline, society/planetary runtime, scheduler, governance, edge clients |
| `src/monkey_brain/actor_contract/` | the contract between the control plane and Actor Runtimes |
| `src/monkey_brain/service_api/` | shared HTTP plumbing (permissions, audit, idempotency) for services built on core |
| `domains/manufacturing/knowledge/services/common/` | `services.common`: config, auth tokens, RBAC, stores shared by every service |
| `packages/` | broca, cerebellum, cortex, soma-cli, … |

`src.monkey_brain` and `services` are namespace packages, so the other repos
add their own modules to them.

## Boundaries

Core must not import the control plane or any domain code. The domain code
plugs in at runtime instead:

- `kernel/extensions.py`: domain plugins (`COGNITIVEOS_DOMAIN_PLUGINS`, or the
  `cognitiveos.domain_plugins` entry point) register verticals, intents,
  planning hooks, confirmation pauses and more through `register(ext)`.
- `kernel/verticals.py`, the intent registry and `pipeline/confirmation_pauses.py`
  are the registries those plugins fill.

`scripts/check_architecture_conformance.py --strict` enforces this. It checks
`core_imports_control_plane` and `core_imports_domain` along with the other
architecture invariants, and CI runs it on every push.

## Development

```bash
uv sync --group dev
python scripts/check_architecture_conformance.py --strict
python -m pytest -q tests/unit/test_cognitive_kernel.py
```
