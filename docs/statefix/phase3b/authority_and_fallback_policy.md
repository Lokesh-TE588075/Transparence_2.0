# Authority and Fallback Policy

## Authority rules

The repository is the single source of truth for:

* whether a conversation exists
* which owner a conversation belongs to
* the stable durable conversation ID
* conversation status
* optimistic version
* `created_at`, `updated_at`, and `last_active_at`
* `genie_conversation_id`
* `last_genie_message_id`

The compatibility cache is never consulted as an authority for any of those values.

## Fallback rules

Degraded reads are allowed only when all of the following are true:

* the current repository read fails with `ConversationRepositoryUnavailableError`
* the adapter previously received a successful authoritative repository record for the same owner-scoped key
* that confirmed snapshot has not been invalidated by delete or missing-record resolution

When those conditions hold, the adapter returns `GenieSessionLookupResult` with:

* `source = CACHE`
* `degraded = True`

## Explicitly disallowed fallbacks

* cache-only discovery for conversations never confirmed by the repository
* cross-owner fallback reuse
* fallback after delete
* fallback after a repository miss cleared the confirmed snapshot
* write operations against fallback state

## Rationale

This keeps degraded behavior available for resilience without weakening ownership boundaries or allowing the cache to resurrect, invent, or leak durable conversation state.
