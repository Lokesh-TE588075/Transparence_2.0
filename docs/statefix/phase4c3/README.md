# Phase 4C3 — Persist Final Genie Message ID

## Summary

Phase 4C3 adds persistence of the final Genie message ID to the durable
conversation record after every successful Genie call. This closes the
last gap in the cold-restart recovery contract: previously, after container
restart the app could recover the Genie conversation ID but not know which
message was last completed, forcing it to re-run the message from scratch.

## Problem

The `ConversationRecord.last_genie_message_id` column (added to the schema
in Phase 2B2B2) was never written to. The Phase 4C2B write-back step
created and bound the record, but the `update_last_genie_message` call was
omitted. This meant recovered conversations had `last_genie_message_id=None`
and could not distinguish a prior result from a new one.

## Solution

Two new persistence paths, both calling `adapter.update_last_genie_message`:

* **MISS path** (`_maybe_persist_durable_writeback`): after `bind_genie_conversation`
  succeeds, persist the `genie_message_id` from the Genie response using
  `expected_version=confirmed_record.version` (post-bind version).

* **RECOVERED path** (`_maybe_persist_recovered_message`): after `_run_inner`
  returns a success result (non-retry-exhausted, non-error, with
  `genie_conversation_id` and `genie_message_id` present), persist the
  message ID using the version from the confirmed lookup record.

Both paths fail closed on any error: the in-memory mapping is cleared,
an error response is returned, and `fallback_recommended=False`.

## Files Changed

| File | Change |
|---|---|
| `app/services/genie_pipeline.py` | 4 new methods, 3 modified methods, 3 new types |
| `tests/test_genie_pipeline_last_message_persistence.py` | New, 57 tests |
| `tests/test_genie_pipeline_durable_lookup.py` | Boundary updates, 5 sites |
| `tests/test_genie_pipeline_durable_writeback.py` | Boundary updates, 3 sites |

## Test Results

| Suite | Count | Result |
|---|---|---|
| Phase 4C3 focused | 57 | All pass |
| 4C2A + 4C2B + 4C3 combined (36+25+50+57) | 168 | All pass |
| Complete non-live | 1928 | All pass, 0 failures |
