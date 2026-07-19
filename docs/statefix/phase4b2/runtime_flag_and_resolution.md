# Phase 4B2 — Runtime Flag and Resolution

## Feature flag

| Variable | Default | Accepted true values | Accepted false values |
|---|---|---|---|
| `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY` | `false` (absent → empty → false) | `true`, `1`, `yes`, `on` | `false`, `0`, `no`, `off`, `` |

Parsing is case-insensitive and whitespace is stripped. Any other value raises
`RequestOwnerIdentityRuntimeConfigurationError`. The default is unconditionally
disabled; no explicit flag is required to run safely.

## Disabled path (ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY = false)

- `resolve_request_owner_identity` returns `None` immediately.
- `CONVERSATION_OWNER_HMAC_SECRET` is **never read**.
- `X-Forwarded-User` header is **never inspected**.
- `RequestOwnerIdentityProvider` is **never constructed**.
- `RequestOwnerIdentitySettings` is **never instantiated**.
- No legacy identity header (`X-Forwarded-Email`, `X-User-Email`) is inspected.
- No fallback identity is generated.
- `chat()` behaviour is **identical to the parent commit** when disabled.

## Enabled path (ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY = true)

1. `RequestOwnerIdentityRuntimeSettings.from_environment` reads the flag (true).
2. `_default_provider_factory` is called with the same environment mapping.
3. `RequestOwnerIdentitySettings.from_environment` reads `CONVERSATION_OWNER_HMAC_SECRET`.
4. A fresh `RequestOwnerIdentityProvider` is constructed (once per request, not cached).
5. `provider.derive_from_headers(headers)` extracts and validates `X-Forwarded-User`.
6. An immutable `RequestOwnerIdentity` is returned and attached to `request.state`.

## No global state

Neither `RequestOwnerIdentityRuntime` nor any provider or identity is stored as a
process-global variable. A fresh runtime object is constructed per
`resolve_request_owner_identity()` call. Providers are constructed at most once
per `resolve_from_headers()` call and discarded afterward.

## Secret access boundary

`CONVERSATION_OWNER_HMAC_SECRET` is read exclusively inside
`RequestOwnerIdentitySettings.from_environment`, which is called only on the
enabled path. The disabled path can never reach that call.

## Module-import safety

The runtime module (`request_owner_identity_runtime.py`) performs no environment
read at import time. All `from app.services.request_owner_identity import ...`
statements are deferred to function bodies on the enabled path.
