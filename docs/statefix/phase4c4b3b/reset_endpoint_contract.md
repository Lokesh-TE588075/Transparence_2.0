# Phase 4C4B3B — Reset Endpoint Contract

## Endpoint

```
POST /api/conversations/{frontend_conversation_id}/reset
```

Implemented in `app/routes/conversation_reset.py`, registered in `app/main.py`:
```python
app.include_router(conversation_reset.router, prefix="/api", tags=["conversations"])
```

## Identity Source

Trusted owner comes **exclusively** from `resolve_request_owner_identity(headers=request.headers)`,
stored on `request.state.REQUEST_OWNER_IDENTITY_STATE_ATTRIBUTE`. No body, query string,
cookie, or frontend-controlled header is ever consulted for ownership.

## HTTP Status Mappings

| Condition | Status |
|---|---|
| Success (RESET / ALREADY_INACTIVE / TOMBSTONE_CREATED) | **200** |
| Invalid frontend ID (empty, contains `@`) | **400** |
| Trusted identity missing (ResolutionError) | **401** |
| Coordinator CAS conflict | **409** |
| Identity disabled (returns `None`) | **503** |
| Identity runtime misconfigured (ConfigurationError) | **503** |
| Coordinator unavailable / internal error | **503** |
| Unexpected coordinator exception | **503** |
| Reset runtime unavailable | **503** |

## Canonicalization

`_canonicalize_frontend_id(raw)` strips whitespace and rejects empty strings or strings
containing `@`.  The canonical value is used for:
- the durable key (`owner_hash + canonical_frontend_id`)
- the process-local key (`owner_hash + session_id + canonical_frontend_id`)
- the coordinator `reset()` call

## Process-Local Key

Derived from `build_process_local_conversation_key(owner_user_id_hash, session_id, canonical_frontend_id)`.
`session_id` is read from `request.state.session_id` (set by `session_middleware`).

## Response Hygiene

All response bodies are static and sanitized. No owner hash, session ID, process-local key,
Genie ID, record version, or exception text appears in any HTTP response or log message.

## Fail-Closed Guarantee

When trusted identity is disabled (`trusted_identity is None`), the endpoint returns
**503** immediately — anonymous/default resets are never permitted.
