# Phase 4C4B5 — Race Validation

## Status: CLOSED

## Race-Path Logging Sanitization

The reset-versus-writeback race (tests 45-46) exercises the full pipeline path
where a coordinator reset occurs mid-Genie-conversation. This path previously
logged raw `genie_conv_id` and `message_id` values.

### Corrections Applied
- `genie_conv_id` → `_log_ref(genie_conv_id)` at pipeline start log (L707)
- `message_id` → `_log_ref(message_id)` at follow-up send log (L725)
- `genie_conv_id` → `_log_ref(genie_conv_id)` at follow-up send log (L725)
- `stmt_ids[0]` → `_log_ref(str(stmt_ids[0]))` at query fetch warning (L780)
- `export_id` → `_log_ref(export_id)` at export success/failure logs (L1320, L1338)

### Test Coverage
- `test_50d_pipeline_race_path_no_genie_id_leakage`: exercises durable RESET
  tombstone + pipeline run with caplog; asserts no raw Genie conv/msg IDs,
  no owner hash, no plc_v1_ key, no frontend ID in logs.

### Race Invariants Preserved
All 10 race invariants from tests 45-46 continue to pass:
1. start_conversation called exactly once
2. send_message never called
3. coordinator.reset() called exactly once
4. No durable writeback (bind_genie_conversation not called)
5. No message persistence (update_last_genie_message not called)
6. Pipeline returns `status=inactive`
7. `fallback_recommended=False`
8. Final durable status is RESET
9. Local GenieSessionStore entry absent
10. No durable reactivation

## Validation
- Combined lifecycle: 55 passed
- Complete non-live: 2258 passed, 0 skipped
- Frontend production build: PASS (Vite 6.4.3, exit 0, 817 modules)
