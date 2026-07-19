# Phase 4B1 — Exit Assessment

## Phase Verdict: COMPLETE

All Phase 4B1 acceptance criteria are met.

## Completed Actions

### 1. Secret Scope Created

Dedicated Databricks secret scope `transparence-owner-identity` created.
- Single-purpose: contains exactly one key.
- No public access.
- No unrelated secrets.

### 2. HMAC Secret Written

Secret key `conversation-owner-hmac-v1` written to scope `transparence-owner-identity`.
- Generated via `secrets.token_urlsafe(48)` (Python standard library).
- ≥48 bytes of cryptographically secure entropy.
- Value was never printed, returned, logged, or stored outside the secret backend.
- Value is absent from Git, app.yaml, documentation, and notebook output.

### 3. App Secret Resource Attachment

**Status: COMPLETE — VERIFIED**

The app resource `conversation-owner-hmac-secret` was attached to the
`transparence` app via AppsAgent with `READ` permission. Verified by
`apps get transparence` — the `resources` array contains:

```json
{
  "name": "conversation-owner-hmac-secret",
  "secret": {
    "scope": "transparence-owner-identity",
    "key": "conversation-owner-hmac-v1",
    "permission": "READ"
  }
}
```

No deployment or restart was triggered. The active deployment
`01f181110277111f8f8d22379e477ecc` and app state RUNNING are unchanged.

### 4. app.yaml Updated

- `CONVERSATION_OWNER_HMAC_SECRET` added with `valueFrom: conversation-owner-hmac-secret`.
- `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY` added with `value: "false"`.
- All existing entries preserved.
- No plaintext secret, no duplicate names.

### 5. Tests

- New file: `tests/test_owner_identity_secret_configuration.py` (24 tests).
- Phase 4A gate `test_app_yaml_unchanged` updated to reflect Phase 4B1 state.
- Focused: 24/24 passed.
- Identity + configuration combined: 108/108 passed.
- Complete non-live suite: 1563/1563 passed, 0 skipped, 0 errors.

### 6. Documentation

Five documents created under `docs/statefix/phase4b1/`:
- `secret_resource_design.md`
- `scope_and_permission_policy.md`
- `app_yaml_configuration.md`
- `test_report.md`
- `phase4b1_exit_assessment.md`

## Security Confirmations

- Secret value never printed or returned in any tool output.
- Secret value absent from Git, app.yaml, documentation, and notebook output.
- App has READ-only permission on the scope.
- Dedicated scope contains no unrelated secret.
- No command-line argument contained the secret.
- `CONVERSATION_OWNER_HMAC_SECRET` env var is NOT active in the currently
  running deployment (requires the next deployment to take effect).
- `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY` is `false`.
- No request-path Python file changed.
- No deployment occurred.
- No app restart occurred.
- No Lakebase connection, database credential, or SQL execution occurred.

## Active Deployment State

| Property | Value |
|---|---|
| Deployment ID | `01f181110277111f8f8d22379e477ecc` |
| Status | SUCCEEDED |
| App state | RUNNING |
| Deploy triggered | No |
| Restart triggered | No |

## Phase 4B2 Gate

Phase 4B2 (request-header extraction behind the disabled flag) is **safe to begin**.
All pre-conditions are confirmed.

### Phase 4B2 Pre-conditions

- [x] Secret scope `transparence-owner-identity` exists.
- [x] Secret key `conversation-owner-hmac-v1` exists in scope.
- [x] App resource `conversation-owner-hmac-secret` attached to `transparence` with READ — **VERIFIED**.
- [x] `CONVERSATION_OWNER_HMAC_SECRET` declared via `valueFrom` in `app.yaml`.
- [x] `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=false` declared in `app.yaml`.
- [x] `RequestOwnerIdentityProvider` module complete and tested.
- [x] No request-path code imports or uses identity provider.
- [x] Full non-live test suite passes (1563/1563).

Phase 4B2 scope: wire `RequestOwnerIdentityProvider` into the Genie pipeline
behind the `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY` flag, add header extraction
from `X-Forwarded-User`, and write integration tests demonstrating that identity
is dormant when the flag is `false` and active when it is `true`.
