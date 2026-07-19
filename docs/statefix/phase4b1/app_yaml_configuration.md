# Phase 4B1 — app.yaml Configuration

## Changes Made

Phase 4B1 appends two entries to the `env` section of `app.yaml`. All existing
entries are preserved unchanged.

### New Entry 1 — HMAC Secret Reference

```yaml
- name: CONVERSATION_OWNER_HMAC_SECRET
  valueFrom: conversation-owner-hmac-secret
```

`valueFrom: conversation-owner-hmac-secret` instructs Databricks Apps to
resolve the secret value at deployment time from the app resource named
`conversation-owner-hmac-secret`. The resource maps to scope
`transparence-owner-identity` and key `conversation-owner-hmac-v1`.

The secret value **never appears** in `app.yaml`, in Git, or in any configuration
file. It is injected only into the running container's environment.

### New Entry 2 — Feature Flag

```yaml
- name: ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY
  description: "Enable HMAC-signed request owner identity enforcement (Phase 4B2 — disabled by default)"
  value: "false"
```

This flag must remain `"false"` until Phase 4B2 integration is explicitly
activated after the full test gate passes. Setting it to `true` before Phase 4B2
is complete would have no effect (no request-path code reads it), but it is
strictly controlled to prevent accidental early activation.

## Preserved Entries

All pre-existing entries are unchanged:

| Entry | Expected value | Status |
|---|---|---|
| `LAKEBASE_ENDPOINT_NAME` | `valueFrom: postgres` | Preserved |
| `ENABLE_DURABLE_GENIE_SESSION_ADAPTER` | `"false"` | Preserved |
| `CONVERSATION_REPOSITORY_BACKEND` | `"memory"` | Preserved |
| `ENABLE_LAKEBASE_CONVERSATION_REPOSITORY` | `"false"` | Preserved |

## Security Constraints

- No plaintext HMAC secret value anywhere in `app.yaml`.
- No scope name or key name embedded as the environment variable value.
- No duplicate environment variable names.
- The `valueFrom` reference is a resource key name only; it does not expose
  the secret scope or key name to the application.

## Deployment Note

The new entries take effect only after the next deployment. The current running
deployment (`01f181110277111f8f8d22379e477ecc`) is not restarted or redeployed
as part of Phase 4B1. The `CONVERSATION_OWNER_HMAC_SECRET` environment variable
is **not** active in the currently running container.

Phase 4B2 will deploy the application, at which point the secret is injected and
identity enforcement code is wired into the request path.
