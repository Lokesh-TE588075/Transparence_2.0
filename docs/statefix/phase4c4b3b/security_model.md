# Security Model — Conversation Reset Endpoint

## Threat Model

The reset endpoint must prevent one authenticated user from resetting another
user's conversation, even if they share the same session cookie or supply a
crafted `frontend_conversation_id`.

## Defence in Depth

### Layer 1: Trusted Identity (Proxy Header)

The owner hash is derived only from the `X-Forwarded-User` (or equivalent)
header set by the Databricks Apps proxy. The route calls
`resolve_request_owner_identity(request)` and uses ONLY the returned
`trusted_identity.owner_user_id_hash`.

No override path exists:
- The request body is not read.
- Query parameters are ignored.
- `X-Forwarded-Email` and `Authorization` headers have no effect.

### Layer 2: Fail-Closed Disabled Mode

When `trusted_identity is None` (identity feature disabled, e.g. local dev),
the route returns 503. It does NOT fall through to an insecure unauthenticated
path.

### Layer 3: Owner-Scoped Durable Key

The durable conversation key used by the repository is:
```
(owner_user_id_hash, frontend_conversation_id)
```

Two owners sharing the same `frontend_conversation_id` hold distinct records.
Owner B cannot reset Owner A's record even with the same frontend ID.

### Layer 4: Opaque Process-Local Key

The `process_local_conversation_key` (format `plc_v1_<64hex>`) is a
domain-separated SHA-256 HMAC over `(owner_hash, session_id, frontend_id)`.
It is used only to remove the in-memory `GenieSession` from `GenieSessionStore`.
No raw hash, email, or session ID is logged or returned.

### Layer 5: Static Response Bodies

All response bodies are module-level constants. No user-supplied value is
reflected in the response. Error messages use generic human-readable text.

## What Reset Does NOT Do

- It does NOT call `adapter.delete()` (hard-delete from the repository).
- It does NOT emit a Genie API request.
- It does NOT trigger the custom pipeline fallback.
- It does NOT modify any other user's session or record.

## Audit Trail

The endpoint logs at INFO level when reset is successful (no identifiers).
Unexpected errors are logged at ERROR level (no identifiers in the log message).
