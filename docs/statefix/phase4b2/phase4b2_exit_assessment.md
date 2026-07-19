# Phase 4B2 — Exit Assessment

## Phase name

Trusted Request Identity Extraction Behind Disabled Feature Flag

## Verdict

PASS. All phase goals are met. Phase 4C (owner-key plumbing into the Genie
pipeline) is safe to begin.

## Phase goal checklist

### Disabled path (ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY = false)

- [x] `CONVERSATION_OWNER_HMAC_SECRET` is not read.
- [x] `X-Forwarded-User` is not inspected.
- [x] `RequestOwnerIdentityProvider` is not constructed.
- [x] Current request behaviour is unchanged.
- [x] Legacy email and anonymous fallback continue to work.

### Enabled path (ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY = true)

- [x] `RequestOwnerIdentity` is derived exclusively from `X-Forwarded-User`.
- [x] Missing or invalid trusted identity returns HTTP 401.
- [x] Configuration failure returns HTTP 503.
- [x] The immutable identity object is placed on `request.state.request_owner_identity`.
- [x] No other identity-related attribute is placed on `request.state`.
- [x] The identity is not used to access durable conversations.
- [x] The existing Genie session key is not replaced.
- [x] Lakebase is not mutated.

## Files changed

### New production files
- `app/services/request_owner_identity_runtime.py`

### Modified production files
- `app/routes/chat.py` (HTTPException import, runtime import, identity extraction block)

### New test files
- `tests/test_request_owner_identity_runtime.py` (64 tests)
- `tests/test_chat_trusted_identity_extraction.py` (42 tests)

### Narrowly modified test files
- `tests/test_request_owner_identity.py` (1 assertion narrowed: allow runtime wrapper
  import in chat.py while still prohibiting direct core module import)
- `tests/test_owner_identity_secret_configuration.py` (2 assertions narrowed: Tests 14
  and 17 updated to allow the Phase 4B2 runtime wrapper import; Test 18 received a
  comment-only reformatting — the all-hyphen guard code was unchanged, confirmed by
  GitHub API comparison between a74a021 and 461ad84)

## Files NOT changed

- `app/main.py` — unchanged.
- `app/services/genie_pipeline.py` — unchanged.
- `app/services/genie_session_store.py` — unchanged.
- `app/services/durable_genie_session_adapter.py` — unchanged.
- `app/services/request_owner_identity.py` — unchanged.
- `app/services/conversation_repository*.py` — unchanged.
- `app/services/lakebase_connection_provider.py` — unchanged.
- `app/services/lakebase_conversation_repository.py` — unchanged.
- `app.yaml` — unchanged. Feature flag remains `false`.
- `requirements.txt` — unchanged.
- All frontend files — unchanged.
- All migration files — unchanged.

## No durable-state operations occurred

- No durable conversation was created, read, updated, or deleted.
- No Lakebase connection was opened.
- No database credential was generated.
- No connection pool was opened.
- No SQL was executed.

## Legacy identity is not the ownership boundary

`X-Forwarded-Email` / `X-User-Email` / `"anonymous"` continue to provide the
`user_id` string for the conversation manager and audit log. This is a temporary
legacy mechanism and is explicitly **not** the durable authorization boundary.
The trusted `RequestOwnerIdentity.owner_user_id_hash` (Phase 4B2) is the
intended durable owner key; it will be plumbed into the Genie pipeline in Phase 4C.

## Test results

| Suite | Files | Result |
|---|---|---|
| Focused runtime | 1 | 64 passed |
| Focused chat integration | 1 | 42 passed |
| Identity phase combined | 4 | 214 passed |
| Exact Phase 4B1 combined + 4B2 | 18 | 842 passed |
| Complete non-live (excluding 4 smoke files) | all | 1669 passed |
| Skipped | — | 0 |
| Failures | — | 0 |
| Collection errors | — | 0 |

## No deployment

No deployment was triggered. The running app was not restarted. The feature flag
in the deployed app.yaml remains `false`.

## Phase 4C gate

Phase 4C (plumb `owner_user_id_hash` into `GeniePipeline.run()` as `owner_key`,
updating the session store key to include the owner hash) is safe to begin
because:

1. `request.state.request_owner_identity` is available on the enabled path.
2. The identity is request-scoped and immutable.
3. No existing pipeline arguments or response contracts were changed.
4. The durable adapter and repository modules are untouched.
5. The feature flag mechanism is tested and operational.
