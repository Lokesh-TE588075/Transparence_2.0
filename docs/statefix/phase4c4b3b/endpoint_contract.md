# Endpoint Contract — POST /api/conversations/{frontend_conversation_id}/reset

## Overview

Resets the server-side Genie session and durable conversation state associated
with a specific frontend conversation ID, scoped to the authenticated owner.

## Request

```
POST /api/conversations/{frontend_conversation_id}/reset
```

### Path Parameter

| Parameter | Type | Description |
|-----------|------|-------------|
| `frontend_conversation_id` | `str` | Opaque identifier assigned by the React frontend. Must not contain `@`. Whitespace is stripped. |

### Authentication

The endpoint reads the trusted owner identity from
`request.state` (set by `session_middleware`), which was resolved via
`resolve_request_owner_identity()` using the request headers
(`X-Forwarded-User` or similar). No Bearer token or body field is used for
authentication.

### Body

No request body is expected.

## Responses

| Status | Meaning | Body |
|--------|---------|------|
| 200 | Reset successful (or idempotent) | `{"status": "ok", "message": "conversation reset"}` |
| 400 | Invalid `frontend_conversation_id` | `{"status": "error", "message": "invalid conversation id"}` |
| 401 | Missing or unresolvable trusted identity | `{"status": "error", "message": "identity required"}` |
| 409 | Concurrent reset in progress — client should retry | `{"status": "error", "message": "reset conflict, please retry"}` |
| 503 | Service unavailable (runtime not ready, disabled mode, or internal error) | `{"status": "error", "message": "service unavailable"}` |

## Idempotency

The endpoint is fully idempotent. Calling it on an already-RESET, STALE, or
EXPIRED conversation returns 200 without changing the lifecycle reason.
Calling it on a missing conversation creates a RESET tombstone and returns 200.

## Security Properties

- **Owner isolation**: the owner is derived exclusively from the trusted
  identity header; no request field can override it.
- **Opaque key derivation**: the process-local conversation key passed to the
  coordinator is a SHA-256 domain-separated digest (`plc_v1_<64hex>`); no
  raw identity or session cookie is passed downstream.
- **Fail-closed disabled mode**: when the identity runtime returns `None`
  (feature disabled), the endpoint returns 503 — it does not proceed without
  a trusted owner.
- **Static response bodies**: no identifiers appear in success or error
  responses; logs are also scrubbed.
