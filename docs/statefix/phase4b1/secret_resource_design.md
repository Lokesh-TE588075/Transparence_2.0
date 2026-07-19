# Phase 4B1 — Secret Resource Design

## Purpose

Phase 4B1 creates and attaches the server-side HMAC secret required by
`RequestOwnerIdentityProvider`. The secret is stored in a dedicated Databricks
secret scope and injected into the application at runtime via a Databricks Apps
secret resource. No code changes enable identity enforcement; the feature flag
remains `false`.

## Secret Scope

| Property | Value |
|---|---|
| Scope name | `transparence-owner-identity` |
| Purpose | Exclusive holder for the TransparencE owner-identity HMAC key |
| Public access | None |
| Unrelated secrets | None — single-purpose scope |
| Created | Phase 4B1 |

The scope is dedicated to this application and contains exactly one secret key.
No other services or applications are granted access to this scope.

## Secret Key

| Property | Value |
|---|---|
| Key name | `conversation-owner-hmac-v1` |
| Algorithm use | HMAC-SHA256 (in `RequestOwnerIdentityProvider`) |
| Generation method | `secrets.token_urlsafe(48)` — 48 cryptographically secure bytes |
| Entropy | ≥48 bytes (384 bits) — well above the 32-byte minimum enforced by `RequestOwnerIdentitySettings` |
| Stored in Git | No |
| Stored in app.yaml | No (only the resource key reference is stored) |
| Versioning scheme | `-v1` suffix allows future key rotation via new key name |

## App Resource Key

| Property | Value |
|---|---|
| Resource name | `conversation-owner-hmac-secret` |
| Attached to app | `transparence` |
| Permission | `READ` (read-only) |
| Injected env var | `CONVERSATION_OWNER_HMAC_SECRET` |
| Injection mechanism | `valueFrom: conversation-owner-hmac-secret` in `app.yaml` |

The app service principal (SP ID `488a0acb-5804-42f0-98b1-a02cc13c4573`) is
granted `READ` permission on the scope. Write and manage permissions are not
granted.

## Runtime Injection

When the application is next deployed, Databricks Apps reads the secret resource
configuration and injects the resolved value as the environment variable
`CONVERSATION_OWNER_HMAC_SECRET`. The value is never written to disk, logs, or
configuration files.

The secret is consumed by `RequestOwnerIdentitySettings.from_environment()`,
which reads `os.environ["CONVERSATION_OWNER_HMAC_SECRET"]` and validates minimum
length. If the variable is absent or too short, the module raises
`RequestOwnerIdentityConfigurationError` rather than silently falling back to a
weak or absent secret.

## What Phase 4B1 Does NOT Do

- Does not read `X-Forwarded-User` in any request handler.
- Does not call `RequestOwnerIdentityProvider` in any request path.
- Does not create or persist durable conversations.
- Does not connect to Lakebase.
- Does not trigger a deployment or restart.
- Does not enable `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY`.

## Secret Rotation Implications

To rotate the HMAC secret:
1. Write a new secret under a new key name (e.g., `conversation-owner-hmac-v2`).
2. Update the app resource to point to the new key.
3. Deploy the app to pick up the new secret.
4. After confirming the new secret is active, delete the old key.

Rotation requires a deployment. All active Genie sessions will use a new HMAC
digest on the next request after restart; there is no backward compatibility
concern because Phase 4B2 has not yet enforced identity validation.
