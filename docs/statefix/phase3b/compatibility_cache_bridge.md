# Compatibility Cache Bridge

The Phase 3B adapter treats `GenieSessionStore` as a compatibility cache only.

## Cache bridge responsibilities

* compute an owner-scoped internal cache key
* refresh cache state after durable repository success
* clear cache state after durable delete or reset outcomes
* swallow cache bridge failures after logging a warning so durable success is preserved

## Refresh order

After a successful repository read or write, the adapter:

1. stores the repository-confirmed snapshot internally
2. resets the cache mapping for the owner-scoped key
3. writes `genie_conversation_id` if present
4. writes `last_genie_message_id` if present

## Delete order

After a successful durable delete, the adapter:

1. removes the confirmed snapshot
2. calls cache `reset_session()` best-effort

## Error handling

Cache bridge exceptions are translated to `DurableGenieSessionCacheError` inside the bridge layer, then downgraded to warnings by the adapter. Durable repository success is never reversed because of cache maintenance failure.

## Security

The cache key is a SHA-256 digest over owner hash plus frontend conversation ID. Raw owner identifiers and raw frontend IDs are not emitted in adapter representations or public exception messages.
