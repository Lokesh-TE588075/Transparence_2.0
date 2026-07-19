# Failure and No-Write Policy

## No-Write Enforcement

Phase 4C2A is strictly **read-only**. The following adapter methods are
never called in production code:

- `get_or_create()`
- `bind_genie_conversation()`
- `update_last_genie_message()`
- `touch()`
- `set_status()`
- `delete()`

Only `adapter.load(key)` is called — exactly once per request.

## Failure Modes

### DurableGenieSessionUnavailableError

Raised by the adapter when the repository is unavailable and no confirmed
snapshot exists.

Pipeline behaviour:
- Returns `_build_durable_lookup_error_response()`.
- `status="error"`, `fallback_recommended=False`.
- No Genie calls (`start_conversation` / `send_message`) occur.
- Static sanitized message — no internal details leaked.

### Unexpected Exception in load()

Any exception other than `DurableGenieSessionUnavailableError` is caught
and converted to `_DurableLookupUnavailableError` internally.

Same pipeline behaviour as above.

### Missing Prerequisites (enabled mode)

- Missing `owner_key` → `_OwnerKeyContractError` → owner-key error response.
- Missing/empty `frontend_conversation_id` → `_DurableLookupUnavailableError`.
- Invalid `owner_key` format → `_OwnerKeyContractError`.

### Non-ACTIVE Status

Records with status STALE, RESET, or EXPIRED are treated as MISS.
Pipeline proceeds with new-conversation flow.

### No genie_conversation_id

ACTIVE record without a bound Genie conversation → treated as MISS.
Pipeline starts a new Genie conversation.

## Confirmed Degraded vs. Unconfirmed Unavailable

| Situation | Adapter Behaviour | Pipeline Behaviour |
|-----------|-------------------|-------------------|
| Repo available | Returns result (degraded=False) | Normal recovery |
| Repo unavailable, confirmed snapshot exists | Returns result (degraded=True) | Recovery accepted |
| Repo unavailable, no snapshot | Raises DurableGenieSessionUnavailableError | Fail closed |

## Fallback Policy

`fallback_recommended=False` on ALL durable error paths.
The custom pipeline must not execute when durable state is unavailable.
