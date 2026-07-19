# Phase 4C4B3B — Reset HTTP Route and Trusted-Identity Integration

## Status

COMPLETE — 2190 non-live tests pass (2149 baseline + 41 new).

## Objective

Expose the completed `ConversationResetCoordinator` through a secure,
owner-scoped HTTP endpoint so that the React frontend can trigger a
server-side session reset without holding any PII or internal session keys.

## What Changed

| File | Type | Description |
|------|------|-------------|
| `app/services/conversation_reset_runtime.py` | NEW | Deferred-import bridge that extracts the shared pipeline objects and constructs a `ConversationResetCoordinator` for use by the route |
| `app/routes/conversation_reset.py` | NEW | `POST /api/conversations/{frontend_conversation_id}/reset` endpoint |
| `app/main.py` | MODIFIED | Added `conversation_reset` router include with `/api` prefix |
| `tests/test_conversation_reset_route.py` | NEW | 31 behavioural and security tests for the HTTP endpoint |
| `tests/test_conversation_reset_runtime_wiring.py` | NEW | 10 runtime wiring tests for the bridge module |

## Architecture Summary

```
Frontend                Backend
  │                       │
  │  POST /api/conversations/{id}/reset
  │──────────────────────►│
  │                       ├── resolve_request_owner_identity()
  │                       │     → trusted_identity (or 401/503)
  │                       ├── _canonicalize_frontend_id(id)
  │                       │     → canonical_id (or 400)
  │                       ├── build_process_local_conversation_key(
  │                       │     owner_hash, session_id, canonical_id)
  │                       ├── get_conversation_reset_coordinator()
  │                       │     → extracts pipeline._store + adapter
  │                       └── coordinator.reset(
  │                             owner_hash, canonical_id, local_key)
  │                               → 200 {"status": "ok", ...}
  │◄──────────────────────┤
```

## Baseline Deltas

| Metric | Before | After |
|--------|--------|-------|
| Non-live tests | 2149 | 2190 |
| New route test file | — | 31 tests |
| New wiring test file | — | 10 tests |
| New production files | — | 2 |
| Modified production files | — | 1 (main.py) |

## Branch

`feature/genie-state-persistence`
