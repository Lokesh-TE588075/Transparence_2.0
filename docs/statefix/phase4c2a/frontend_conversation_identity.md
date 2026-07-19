# Frontend Conversation Identity

## Architecture

```
one UI chat = one durable application conversation = one Genie conversation
```

## Identity Components

| Component | Source | Format |
|-----------|--------|--------|
| `frontend_conversation_id` | `body.conversation_id` or generated | Raw browser-supplied UUID |
| `owner_key` | `_trusted_identity.owner_user_id_hash` | 64-char lowercase hex (SHA-256) |
| `app_conversation_id` | `f"{session_id}:{frontend_conversation_id}"` | Session-scoped composite key |
| `DurableGenieSessionKey` | `(owner_key, frontend_conversation_id)` | Frozen dataclass |

## Key Separation

- `app_conversation_id` is the GenieSessionStore lookup key.
- `frontend_conversation_id` is passed **separately** to `GeniePipeline.run()`.
- The durable key is built inside `_durable_session_lookup()` from raw components.
- `frontend_conversation_id` is **never** parsed from `app_conversation_id`.

## Chat.py Plumbing

```python
frontend_conversation_id = body.conversation_id
if not frontend_conversation_id:
    frontend_conversation_id = conversations.create_conversation(user_id=user_id)

server_conversation_key = f"{session_id}:{frontend_conversation_id}"

genie_result = _genie_pl.run(
    ...,
    owner_key=_owner_key,
    frontend_conversation_id=frontend_conversation_id,
)
```

## Security Constraints

- `owner_key` comes only from `_trusted_identity` (X-Forwarded-Access-Token → SP introspection).
- Request body, legacy email header, Authorization header, and cookies cannot override it.
- `chat.py` does not import or access the durable adapter or repository.
