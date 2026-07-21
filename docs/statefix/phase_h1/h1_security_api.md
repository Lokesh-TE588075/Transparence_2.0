# H1: Ownership/Security Model and History API

## Security Model

### Owner Identity

- Trusted identity is resolved from `X-Forwarded-User` (set by Databricks Apps).
- `owner_user_id_hash` = HMAC-SHA256(secret_key, email) — raw email never stored.
- The hash is derived server-side; browser/body/query/cookie cannot supply it.

### Owner Isolation

- Every SQL query (SELECT, INSERT, UPDATE) includes `WHERE owner_user_id_hash = %s`.
- Owner hash is always a parameterized value (never interpolated into SQL).
- Owner A cannot read, write, or deactivate Owner B's messages.

### Forbidden Payload Fields

`response_payload_json` must NOT contain:
`genie_conversation_id`, `genie_message_id`, `download_key`, `export_id`,
`export_status`, `export_mode`, `generated_sql`, `genie_thought_description`,
`fallback_recommended`, `shape_retry_exhausted`, `debug_info`, owner hashes,
process-local keys, raw exception traces.

## History API

### Endpoint

```
GET /api/conversations/{frontend_conversation_id}/messages
```

**Query Parameters:**
- `page` (int, default=1, min=1): 1-based page number.
- `page_size` (int, default=50, min=1, max=100): Records per page.

### Security Checks (fail-closed)

1. Resolve trusted identity — 503 on `ConfigurationError`, 401 on `ResolutionError`.
2. If identity is None — 503.
3. Validate `frontend_conversation_id` (strip, reject empty/@ chars) — 400.
4. Call `get_message_repository()` — 503 if None (disabled/unavailable).
5. Query `list_messages(owner_hash, frontend_id, page, page_size)`.
6. On `MessageRepositoryUnavailableError` — 503.

### Response Format (200 OK)

```json
{
  "conversation_id": "<frontend_conversation_id>",
  "messages": [
    {
      "id": "<opaque-uuid>",
      "role": "user" | "assistant",
      "content": "...",
      "sequence": 1,
      "timestamp": "2026-07-21T10:00:00Z",
      "status": "success",
      "is_table": false,
      "table_data": null,
      "row_count": 0,
      "suggested_questions": null,
      "computed_chart_data": null,
      "computed_metrics": null,
      "query_description": null,
      "has_visualization": false,
      "source": null
    }
  ],
  "page": 1,
  "page_size": 50,
  "total_messages": 1,
  "has_more": false
}
```

**No internal identifiers** (owner hash, genie IDs, process-local keys) appear
in any response body.

### What the API Does NOT Return

- 404 for unknown conversations (returns empty messages to avoid enumeration).
- Owner hash in any field.
- Genie conversation/message IDs.
- Stack traces or raw exception messages.

## Persistence Failure Policy

### User Message Write Fails

The failure is logged as WARNING. Genie processing continues.
The user gets a successful chat response. History for this exchange is lost.
(Intentional: user-visible experience is never blocked by history storage.)

### Genie Succeeds, Assistant Write Fails

Same: logged as WARNING, response is returned normally.
This creates a partial state: user message stored but assistant message missing.
On reload, the user message will appear but the assistant response will not.
This is the known unavoidable partial-state scenario.

### Conversation Reset Deactivation Fails

Logged as ERROR but never propagated. Reset succeeds regardless.
The messages will remain `is_active=TRUE` until manually deactivated or TTL'd.
