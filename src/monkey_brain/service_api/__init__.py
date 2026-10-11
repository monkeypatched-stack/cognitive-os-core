"""Shared HTTP plumbing for services built on the core: auth/permission
dependencies (dependencies.py), Idempotency-Key handling (idempotency.py)
and route audit (audit.py). Used by the control plane's API and the actor
repo's domain API service alike. Moved from the control plane's api/
package, which keeps aliases at the old module paths."""
