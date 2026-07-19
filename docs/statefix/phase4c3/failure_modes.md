# Phase 4C3 Failure Modes

## Fail-Closed Contract

Every failure in the message-persistence layer:
1. Returns an error response with `fallback_recommended=False`.
2. Clears the in-memory Genie conversation mapping.
3. Does NOT delete, touch, set_status, or otherwise mutate the durable record
   beyond what was already committed.

This is intentional: the record already has the correct `genie_conversation_id`
from Phase 4C2B. The failure only means the `last_genie_message_id` could not
be updated. On the next request the conversation can still be recovered normally.

## Failure Catalog

| Trigger | Outcome |
|---|---|
| `genie_message_id` is None or empty | Fail closed — missing message ID is a hard error |
| `genie_conversation_id` missing from result | Persistence skipped (same as pre-4C3 behavior) |
| Adapter unavailable | Fail closed |
| Record not found in repo | Fail closed |
| Conv-ID mismatch (result vs confirmed record) | Fail closed |
| Version conflict, reload succeeds + same msg | Idempotent success |
| Version conflict, reload succeeds + different msg | Fail closed |
| Version conflict, reload fails | Fail closed |
| `shape_retry_exhausted=True` | Persistence skipped (signal forwarded to frontend) |
| Error status result | Persistence skipped |

## fallback_recommended Semantics

`fallback_recommended=False` is always returned on Phase 4C3 errors because:
- The underlying Genie call succeeded.
- The error is in the persistence layer, not in Genie.
- Sending the user to a non-Genie fallback is misleading and unhelpful.
- The correct user-facing message is the generic durable error copy.
