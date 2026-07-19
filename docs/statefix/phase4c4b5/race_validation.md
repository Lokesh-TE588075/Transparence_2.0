# Phase 4C4B5 — Race and Concurrency Validation

Date: 2026-07-20
Branch: feature/genie-state-persistence

## A. Reset and Old Message Request Overlap

- Before reset succeeds, old response may update the still-active old chat (frontend `activeConvIdRef` still holds old ID).
- After new activation (`activeConvIdRef.current` updated), `isResponseEligible()` returns false for old ID.
- Old response callbacks are silently rejected — no state mutation on new conversation.
- Verified by frontend tests 37–40 (old conversation cannot reactivate/send/clear/overwrite).

## B. Reset Conflicts with Durable MISS Writeback

- RESET tombstone is written before any pipeline writeback can bind a Genie conversation.
- `get_or_create` on a key with existing RESET status returns the RESET record, not a fresh ACTIVE.
- Pipeline TOCTOU check: if returned record is non-ACTIVE, no bind or last-message update occurs.
- Verified by test_30_reset_tombstone_blocks_writeback_bind.

## C. Two Reset Requests Occur

- Frontend synchronous lock (`acquireResetLock`) prevents duplicate transport.
- If duplicate transport nevertheless occurs (e.g., retry middleware), backend remains idempotent:
  - First request: ACTIVE → RESET (200).
  - Second request: finds RESET → ALREADY_INACTIVE (200).
- Verified by test_31_double_reset_idempotent and test_32_triple_reset_idempotent.

## D. Reset Succeeds, Followed Immediately by Send

- Frontend generates new ID and updates `activeConvIdRef` BEFORE reactive state.
- Send uses only the new frontend ID (captured at submit time from `activeConvIdRef.current`).
- Old ID's durable record is RESET → pipeline blocks.
- New ID has no durable record → normal pipeline path (fresh Genie conversation).
- Verified by test_27 (old ID blocked) and test_28 (new ID independent).

## E. Reset Fails While Old Send Remains Pending

- Reset returns 409/503/network error → frontend retains old conversation as active.
- `activeConvIdRef.current` is NOT updated.
- Old response may complete normally (still eligible via `isResponseEligible`).
- Verified by frontend tests 48 (failure retains old conversation in list).

## F. Component Teardown During Reset

- `isMountedSafe(isMountedRef)` checked before every state setter.
- If component unmounts during in-flight reset, no state update occurs.
- Promise `.finally()` releases lock regardless of mount state.
- No unhandled rejection produced.
- Verified by frontend test 35 (teardown no unhandled rejection).

## Reset/Writeback Race Result

RESET tombstone wins. The CAS transition to RESET is committed before any concurrent pipeline writeback can bind the same durable key. The adapter's `get_or_create` returns the existing RESET record — pipeline recognizes non-ACTIVE status and aborts bind.

## Frontend Late-Response Result

Old-ID responses are rejected by `isResponseEligible()` after `activeConvIdRef` updates. The ref update is synchronous and occurs before any reactive state update, ensuring no window where both old and new responses can mutate the same state slice.
