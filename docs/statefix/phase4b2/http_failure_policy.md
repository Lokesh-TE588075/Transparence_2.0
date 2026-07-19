# Phase 4B2 — HTTP Failure Policy

The failure policy applies **only when `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY` is
`true`**. When the flag is disabled (the default), no failure mode is reachable
and the response contract is unchanged.

## HTTP 401 — missing or invalid trusted identity

**Trigger**: `resolve_request_owner_identity` raises
`RequestOwnerIdentityRuntimeResolutionError`.

**Causes**:
- `X-Forwarded-User` header is absent from the request.
- `X-Forwarded-User` header value fails validation (empty, too long, contains
  control characters, contains a comma).

**Response**:
```
HTTP 401
{"detail": "Trusted request identity is required."}
```

**Guarantees**:
- The raw header value is never echoed.
- The principal is never included in the response body or logs.
- Legacy email headers (`X-Forwarded-Email`, `X-User-Email`) cannot satisfy this
  requirement.
- The pipeline is **not executed** when this response is returned.

## HTTP 503 — identity runtime or configuration unavailable

**Trigger**: `resolve_request_owner_identity` raises
`RequestOwnerIdentityRuntimeConfigurationError`.

**Causes**:
- `CONVERSATION_OWNER_HMAC_SECRET` is absent, blank, or too short.
- `CONVERSATION_OWNER_HMAC_SECRET` contains surrounding whitespace.
- The feature flag value is invalid (neither a recognized true nor false string).

**Response**:
```
HTTP 503
{"detail": "Trusted request identity is unavailable."}
```

**Guarantees**:
- The secret name, secret value, and raw environment contents are never disclosed.
- No traceback is included.
- The pipeline is **not executed** when this response is returned.

## Exception handling placement

The identity extraction `try/except` block is placed at the **top of `chat()`**,
before the outer `try/except Exception as e` catch-all. This ensures that
`HTTPException` (raised for 401 and 503) propagates cleanly to FastAPI's
exception handler rather than being caught and converted to a generic
`ChatResponse(status="error")` response.

## Disabled behaviour

When the flag is disabled, no exception can be raised from the identity
extraction block (resolver returns `None` immediately). The 401 and 503
paths are completely unreachable.
