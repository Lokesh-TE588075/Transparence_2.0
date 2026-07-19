# Test Report

## Test Execution Summary

| Suite | Collected | Passed | Failed | Skipped |
|-------|-----------|--------|--------|---------|
| test_genie_pipeline_durable_lookup.py (focused) | 36 | 36 | 0 | 0 |
| test_chat_durable_lookup_key_plumbing.py (focused) | 25 | 25 | 0 | 0 |
| Phase 4C1 + 4C2A focused (7 files) | 313 | 313 | 0 | 0 |
| Exact 22-file combined suite | 994 | 994 | 0 | 0 |
| Complete non-live suite (43 files) | 1821 | 1821 | 0 | 0 |

## Suite Definitions

### Focused Pipeline Suite
- tests/test_genie_pipeline_durable_lookup.py

### Focused Chat Suite
- tests/test_chat_durable_lookup_key_plumbing.py

### Selected Focused Phase 4C1 + 4C2A Suite (7 files)
- tests/test_genie_pipeline_owner_key_plumbing.py: 52
- tests/test_chat_owner_key_plumbing.py: 39
- tests/test_chat_trusted_identity_extraction.py: 42
- tests/test_durable_genie_session_runtime_factory.py: 99
- tests/test_genie_backend_durable_runtime_wiring.py: 20
- tests/test_genie_pipeline_durable_lookup.py: 36
- tests/test_chat_durable_lookup_key_plumbing.py: 25

### Exact 22-File Combined Suite
Previous 20 persistence-related files: 933
Phase 4C2A pipeline: 36
Phase 4C2A chat: 25
Total: 994

Per-file arithmetic:
- test_lakebase_conversation_repository.py: 79
- test_lakebase_connection_provider.py: 59
- test_durable_genie_session_adapter.py: 75
- test_durable_genie_session_runtime_factory.py: 99
- test_genie_backend_durable_runtime_wiring.py: 20
- test_main_durable_runtime_lifecycle.py: 58
- test_conversation_repository.py: 87
- test_conversation_repository_factory.py: 67
- test_conversation_state_cleanup.py: 8
- test_conversation_state_factory.py: 6
- test_delta_conversation_state.py: 10
- test_genie_session_store.py: 37
- test_genie_session_store_context.py: 7
- test_multi_user_session_isolation.py: 16
- test_request_owner_identity.py: 84
- test_owner_identity_secret_configuration.py: 24
- test_request_owner_identity_runtime.py: 64
- test_chat_trusted_identity_extraction.py: 42
- test_genie_pipeline_owner_key_plumbing.py: 52
- test_chat_owner_key_plumbing.py: 39
- test_genie_pipeline_durable_lookup.py: 36
- test_chat_durable_lookup_key_plumbing.py: 25

## Excluded Live Suites (exactly 4)

- tests/test_genie_live_smoke.py
- tests/test_genie_integration_smoke.py
- tests/test_delta_state_live_smoke.py
- tests/test_new_pipeline_live_smoke.py

## Chat Argument Contract — Behavioural Testing

The chat plumbing tests execute `chat()` directly using `asyncio.run()` with:
- A fake Request with `request.state.session_id`
- A mock body matching ChatRequest interface
- Mocked `resolve_request_owner_identity`
- Mocked `_get_services` returning stub conversation/audit services
- A fake genie_backend_factory module with `_CapturingGenie`

This proves runtime argument values by capturing actual `pipeline.run()` kwargs
during execution — NOT through source inspection alone.

### Behavioural Tests Proving:
- Explicit frontend_conversation_id passed through to pipeline
- Generated frontend_conversation_id passed when body has none
- app_conversation_id = session_id:frontend_conversation_id (verified at runtime)
- owner_key comes only from trusted identity hash
- audit_principal, source, identity object not passed to pipeline
- Override attempts (body attrs, headers, cookies) cannot replace owner_key
- Disabled identity path passes None owner_key but correct conversation IDs
- HTTP 401/503 prevent pipeline execution
- Response conversation_id matches the frontend ID

### Boundary Tests (limited source inspection):
- chat.py does not import DurableGenieSessionAdapter
- chat.py does not import conversation_repository
- chat.py does not import lakebase

## Phase 4C2A Boundary-Test Scope

The following test files were NOT modified in Phase 4C2A — they test
infrastructure created in Phase 3B/3C and remain unchanged:
- tests/test_durable_genie_session_adapter.py (75 tests, Phase 3B)
- tests/test_genie_backend_durable_runtime_wiring.py (20 tests, Phase 3C)

These files verify that:
- Runtime bundle attaches correctly to pipeline
- adapter.load() is the only read method
- get_or_create, bind, update, touch, set_status, delete are prohibited
- No direct repository access from pipeline
- No live Lakebase access

The Phase 4C2A work relies on this infrastructure but did not modify it.

## No Deployment

No application deployment or restart was performed.
