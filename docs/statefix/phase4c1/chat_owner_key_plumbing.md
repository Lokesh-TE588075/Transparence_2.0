# Phase 4C1: Chat Owner-Key Plumbing

## Overview

`app/routes/chat.py` now reads the trusted `owner_user_id_hash` from
the request-scoped identity and passes it to `GeniePipeline.run()`.

## Flow

1. Phase 4B2 resolves `_trusted_identity` via
   `resolve_request_owner_identity(headers=request.headers)`.
2. If identity is `None` (feature disabled), `_owner_key = None`.
3. If identity exists, `_owner_key = _trusted_identity.owner_user_id_hash`.
4. Pipeline is called with `owner_key=_owner_key`.

## Security Properties

- `owner_key` is derived ONLY from the immutable `RequestOwnerIdentity`.
- Cannot be overridden via request body.
- Cannot be overridden via query parameters.
- Cannot be overridden via headers other than the trusted identity.
- Cannot be overridden via conversation_id.
- Cannot be overridden via legacy email headers.
- Cannot be overridden via Authorization header.
- Cannot be overridden via cookies.

## Disabled Path

When `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY` is false:
- `_trusted_identity` is None.
- `owner_key` is None.
- No HMAC secret is read.
- No X-Forwarded-User is inspected.
- Legacy email behaviour is unchanged.
- Anonymous fallback is unchanged.

## What Is NOT Passed

- `audit_principal` — stays in request.state only.
- `source` — stays in request.state only.
- Raw `X-Forwarded-User` value — never enters the pipeline.
- Full `RequestOwnerIdentity` object — only the hash is extracted.
