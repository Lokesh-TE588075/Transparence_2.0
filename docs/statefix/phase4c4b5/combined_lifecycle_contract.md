# Phase 4C4B5 — Combined Lifecycle Contract

## Status: NOT CLOSED (build gate blocked)

## Logging Sanitization Policy

No complete identifier may appear in application logs:
- No raw `app_conversation_id` (plc_v1_ keys)
- No raw `owner_user_id_hash`
- No raw `session_id` or `frontend_conversation_id`
- No raw Genie conversation IDs (`genie_conv_id`)
- No raw Genie message IDs (`message_id`)
- No raw statement IDs or export IDs
- No raw exception text containing identifiers

### Accepted Logging Patterns
- Static text messages (e.g., "GenieSessionStore: session created.")
- `_log_ref(identifier)` — 8-char SHA-256 diagnostic fingerprint
- Integer counts, boolean flags, intent strings, header lists

### Production Files Sanitized
- `app/services/genie_session_store.py` — 5 log statements, all static text
- `app/services/genie_pipeline.py` — `_log_ref()` helper + all 24 identifier positions wrapped
- `app/services/conversation_reset_coordinator.py` — 1 log statement, static text
- `app/routes/conversation_reset.py` — 4 log statements, all static text

## Test Results

### Combined Lifecycle (55 tests)
- Tests 1-46: functional lifecycle scenarios
- Tests 47-50: caplog leakage assertions (route paths)
- Tests 50b-50d: caplog leakage assertions (pipeline paths — Genie start, follow-up, race)
- Tests 51-52: same-cookie different-owner derivation

### Logging Validation (6 files, 294 tests)
| File | Count |
| --- | --- |
| test_genie_session_store.py | 52 |
| test_genie_session_store_context.py | 7 |
| test_conversation_reset_combined_lifecycle.py | 55 |
| test_genie_pipeline_inactive_durable_state.py | 73 |
| test_genie_pipeline_durable_writeback.py | 50 |
| test_genie_pipeline_last_message_persistence.py | 57 |

### 31-File Suite: 1431 passed
### Complete Non-Live: 2258 passed, 0 skipped
### Frontend JavaScript: 49 passed

## Frontend Build Gate
**BLOCKED** — npm not available in Databricks serverless compute.

### Local Build Command
```bash
cd frontend && npm ci && npm run build
```

## Commit History
- Parent (first correction): `60ab02299ced63a520b4376dc26b6d1dc96af3dc`
- First logging commit: `65a07818510d74bf9c8645f9250ff2b1a9fa0c8c`
- Final correction commit: TBD (pending push)
