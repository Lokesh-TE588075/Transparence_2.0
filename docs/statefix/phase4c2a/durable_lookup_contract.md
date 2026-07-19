# Durable Lookup Contract

## Overview

Phase 4C2A adds a **read-only** durable Genie session lookup to
`GeniePipeline.run()`.  When a matching durable record exists, the pipeline
recovers the existing Genie conversation mapping into the in-memory
`GenieSessionStore` so that `send_message()` is used instead of
`start_conversation()`.

## Lookup Outcome

`_durable_session_lookup()` returns a `_DurableLookupOutcome`:

| Outcome | Meaning |
|---------|---------|
| DISABLED | Durable mode off; no adapter access occurred. |
| MISS | Lookup executed; no recoverable record found. |
| RECOVERED | Existing Genie conversation restored in session store. |

## Recovery Guarantee

When the outcome is RECOVERED:

1. The recovered `genie_conversation_id` is set in the session store.
2. `_run_inner()` receives `_durable_recovered=True`.
3. The recovered mapping is **authoritative** — local routing cannot reset it.
4. `send_message()` is called on the recovered Genie conversation.
5. `start_conversation()` is never called for the request.
6. Shape retries continue through `send_message` on the same conversation.
7. No durable write occurs.

## Empty-Store Restart Recovery

After a container restart:
- GenieSessionStore is completely empty (no mappings, no context, no last_intent).
- The durable lookup recovers the Genie conversation from the repository.
- Recovery does NOT depend on local intent classification.
- The route decision may classify the prompt as AGGREGATION, BROAD_LISTING, etc.
  — the `_durable_recovered` flag overrides the `TRUE_FOLLOW_UP` requirement.

## Confirmed Degraded-Read Policy

Per the adapter contract:
- `degraded=True` means the result came from an adapter-local snapshot that was
  **previously confirmed** by the repository.
- `DurableGenieSessionUnavailableError` is raised when **no** confirmed snapshot
  exists — this is the fail-closed path.
- Confirmed degraded reads are valid recovery sources.

## Lookup Miss Behaviour

- `adapter.load()` returns `None`.
- Pipeline proceeds with normal new-conversation flow.
- `start_conversation()` may be called.
- No durable record is created, bound, or mutated.

## Lookup Unavailable Behaviour

- `adapter.load()` raises `DurableGenieSessionUnavailableError` or another exception.
- Pipeline returns a static error response.
- `fallback_recommended=False` — custom pipeline must not execute.
- No Genie calls occur.
