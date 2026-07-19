# Phase 4B2 — Request State Contract

## Attribute name

```
REQUEST_OWNER_IDENTITY_STATE_ATTRIBUTE = "request_owner_identity"
```

Defined in `app/services/request_owner_identity_runtime.py` and imported by
`app/routes/chat.py`.

## When the attribute is set

`request.state.request_owner_identity` is set if and only if:

1. `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY` is `true`, **and**
2. `resolve_request_owner_identity` returns a non-`None` value.

## When the attribute is absent

If the feature flag is disabled, `request.state.request_owner_identity` is
**never set**. The attribute does not exist on the state object. Downstream code
must use `getattr(request.state, "request_owner_identity", None)` if it needs
to be tolerant of both states.

## Type and mutability

The value is always an instance of `RequestOwnerIdentity` (a frozen dataclass).
It is immutable; no field can be overwritten after construction.

## What is NOT attached

Only the single `RequestOwnerIdentity` object is attached. The following are
**never** placed on `request.state` by Phase 4B2:

- `owner_user_id_hash` (raw hash string)
- `audit_principal` (raw principal string)
- `source` (source label string)

These are accessible via `request.state.request_owner_identity.owner_user_id_hash`
etc. after Phase 4C plumbs the identity into the pipeline.

## Existing state attributes are unaffected

`request.state.session_id` (set by `session_middleware`) is unchanged.
No other existing state attribute is modified.

## Relationship to legacy identity

`request.headers.get("X-Forwarded-Email")` continues to provide the legacy
`user_id` string used by the conversation manager and audit log. This is a
**separate, temporary** identity source and is **not** the durable authorization
boundary. The trusted `RequestOwnerIdentity` (Phase 4B2) will replace it as the
durable owner key in Phase 4C.
