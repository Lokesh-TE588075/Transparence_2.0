# Phase 4B1 — Scope and Permission Policy

## Secret Scope Policy

### Scope: `transparence-owner-identity`

This scope is **dedicated exclusively** to the TransparencE application's
owner-identity HMAC secret. It contains no unrelated secrets and will not be
shared with other applications.

| Property | Policy |
|---|---|
| Scope name | `transparence-owner-identity` |
| Access model | Databricks workspace ACL via SDK |
| Public READ access | Not granted |
| Unrelated secrets | Prohibited — scope is single-purpose |
| Maximum keys | 1 active key at any time (rotation via new key + delete old) |

### Why a Dedicated Scope

A dedicated scope provides the strongest isolation guarantee:
- If the secret resource grants READ on the full scope, no other secret leaks.
- Future key rotation is safe: write a new key in the same scope, update the
  resource key reference, then delete the old key.
- Audit logs are scoped to this application only.

## App Service Principal Permissions

The TransparencE app SP (`app-31pcl9 transparence`,
platform ID `78664835752275`, client ID `488a0acb-5804-42f0-98b1-a02cc13c4573`)
is granted **READ** permission on the secret scope via the app resource
configuration.

| Grant | Value |
|---|---|
| Permission | `READ` |
| Scope | `transparence-owner-identity` |
| Key | `conversation-owner-hmac-v1` |
| Write granted | No |
| Manage granted | No |

`READ` is the minimum permission required for the app to retrieve the secret
at runtime. `WRITE` and `MANAGE` are intentionally withheld:
- `WRITE` would allow the app to overwrite the secret (unnecessary and a
  security risk if the app process is compromised).
- `MANAGE` would allow the app to change ACLs on the scope.

## Platform Secret Resource Behavior

Databricks Apps secret resources apply at the **scope level** (the platform
policies READ/WRITE/MANAGE for the named scope + key pair). The resource
configuration specifies both `scope` and `key`; however, the effective permission
applies to the scope.

Because the scope contains exactly one key, granting READ on the scope is
equivalent to granting READ on the single key. This equivalence holds as long
as the scope discipline (one key per scope) is maintained.

## Interactive User Permissions

The workspace owner `lokesh.choraria@te.com` has MANAGE access to all secret
scopes they create. This is required to:
- Create the scope.
- Write the initial secret.
- Grant the app SP permission via the resource attachment.

No IT escalation is required for this phase.

## Non-Granted Entities

No other principals (other apps, external users, service accounts) are granted
access to `transparence-owner-identity`. The scope does not appear in any
shared resource configuration.
