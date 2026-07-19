# Phase 4C2A Security Notes

## Owner-Scoped Isolation
- The durable key requires `owner_user_id_hash` (64-char hex SHA-256).
- Repository lookup is scoped: `get_by_frontend_id(owner_hash, frontend_id)`.
- Owner A cannot retrieve Owner B's conversation even with the same
  `frontend_conversation_id`.
- Tests verify this isolation (TestOwnership class).

## No Credential Leakage
- `owner_key` is never included in response payloads.
- `frontend_conversation_id` is never logged from durable lookup code.
- Error responses use a static sanitized message — no internal details exposed.
- Tests verify these constraints.

## Fail-Closed Behavior
- Missing prerequisites (owner_key or frontend_id) when durable is enabled →
  error response, not graceful degradation to unauthenticated flow.
- Repository unavailable → error response, not fallback to custom pipeline.
- This prevents state confusion and potential session hijacking.

## No-Write Enforcement
- Only `adapter.load()` is called — proven by 6 dedicated no-write tests.
- Tests install `side_effect=AssertionError` on all mutating adapter methods.
- A future write phase (4C2B) will be needed for persistence.
