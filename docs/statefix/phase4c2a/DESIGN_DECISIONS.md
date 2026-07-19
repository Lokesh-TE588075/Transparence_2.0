# Phase 4C2A Design Decisions

## 1. Deferred Import Pattern
The adapter is imported inside `_durable_session_lookup()` rather than at module
top-level. This preserves the Phase 3B constraint that the adapter is not imported
unless durable mode is enabled and the lookup gate is reached.

## 2. _DurableLookupUnavailableError vs _OwnerKeyContractError
Two distinct internal exceptions differentiate the failure modes:
- `_OwnerKeyContractError`: Structural owner-key validation failure.
- `_DurableLookupUnavailableError`: Any durable-specific failure (prerequisites,
  repository unavailable, degraded read, key construction error).
Both result in `fallback_recommended=False`.

## 3. Degraded Reads Rejected
Per "unconfirmed degraded read fails closed" requirement, any result with
`degraded=True` raises `_DurableLookupUnavailableError`. The adapter contract
says degraded=True means "repository unavailable, returning from confirmed
snapshot" — but our Phase 4C2A policy is stricter: we require live repository
confirmation.

## 4. Store Hydration Key
The session store is hydrated using `app_conversation_id` (which is
`session_id:frontend_conversation_id`), NOT the adapter's internal cache key
(SHA-256 digest). This ensures `_run_inner()` finds the mapping using its
standard lookup path.

## 5. No Fallback on Failure
When the durable lookup fails (prerequisites missing, repo unavailable,
degraded read), the pipeline returns immediately with `fallback_recommended=False`.
Neither the custom pipeline nor a new Genie conversation is started.

## 6. ACTIVE-Only Recovery
Only records with `ConversationStatus.ACTIVE` are recovered. Non-active records
(STALE, RESET, EXPIRED) are treated as "not found" — the pipeline continues
with a new-conversation flow.

## 7. Missing genie_conversation_id
If a record is ACTIVE but has `genie_conversation_id=None` (created but never
bound), it's treated as "not yet ready" and the pipeline starts fresh.
