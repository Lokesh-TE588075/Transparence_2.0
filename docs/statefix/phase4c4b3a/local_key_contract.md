# Phase 4C4B3A — Process-Local Conversation Key Contract

## Overview

A *process-local conversation key* (`plc_v1_` prefix) is a 71-character opaque
identifier derived from three inputs:

| Input | Source |
|---|---|
| `owner_user_id_hash` | Trusted identity (64 lowercase hex chars) |
| `session_id` | Server-side session cookie |
| `frontend_conversation_id` | Client-supplied conversation identifier |

The key is produced by `app/services/process_local_conversation_key.py` using
a SHA-256 domain-separated digest:

```
domain_sep = b"transparence-process-local-conversation:v1\x00"
canonical_frontend = frontend_conversation_id.strip()   # canonicalization
digest = sha256(domain_sep + owner_ascii + b"\x00" + session_utf8 + b"\x00" + canonical_frontend_utf8).hexdigest()
plc_key = "plc_v1_" + digest   # 7 + 64 = 71 chars
```

## Canonicalization

`frontend_conversation_id` is canonicalized with `strip()` before hashing,
matching the `DurableGenieSessionKey` contract that strips and stores the
canonical value.  This ensures:

- `build(..., "conv-1")` == `build(..., "  conv-1  ")` == `build(..., "conv-1\t")`
- Whitespace variants of the same frontend conversation ID cannot create
  separate process-local sessions for the same durable logical conversation.
- The canonical value used in the digest equals the value stored by
  `DurableGenieSessionKey` — the two keys are logically aligned.

Existing test vectors use plain IDs (no surrounding whitespace), so `strip()`
is a no-op for them and the documented digest values are unchanged.

## Purpose

Before Phase 4C4B3A, `GenieSessionStore` keyed sessions by the raw
`session:frontend_conversation_id` composite string.  Two users sharing the
same session cookie (e.g. a shared browser profile) but holding different
trusted identities could collide in the store.

The opaque key binds the store entry to the authenticated owner so two users
sharing a cookie and conversation ID receive strictly disjoint in-process
state.

## Key Separation

| Concern | Key used |
|---|---|
| Durable repository (Lakebase) | `owner_user_id_hash` + `frontend_conversation_id` |
| Process-local `GenieSessionStore` | `plc_v1_<digest>` (opaque, owner-scoped) |
| Pipeline `app_conversation_id` | `plc_v1_<digest>` (enabled path) |
| Pipeline `frontend_conversation_id` | Raw `frontend_conversation_id` |
| Disabled (identity off) legacy path | `session:frontend_conversation_id` |

## Known Test Vectors

| (owner_hash, session_id, frontend_id) | plc_v1_ key |
|---|---|
| (`"a"*64`, `"test-session-001"`, `"frontend-conv-001"`) | `plc_v1_8555bda75eb3fb6a048b82a7218843f01e00c444dca06fa2d9222b3e9c0b4566` |
| (`"b"*64`, `"test-session-001"`, `"frontend-conv-001"`) | `plc_v1_25d237104a3862a471728cff3bdf01fd634ed082672bf6576541f7b3c76d31e8` |
| (`"a"*64`, `"test-session-002"`, `"frontend-conv-001"`) | `plc_v1_5dfebb2a16a7cbe2b8a1cc77214487648689dbad67e4e5f55fed49661422ad64` |
| (`"a"*64`, `"test-session-001"`, `"frontend-conv-002"`) | `plc_v1_9f0b0ade3b583032a1a2812e839d85c7fe8c429cf1ec39e9c6877e5cd2f56c8f` |

## Validation Rules

- `owner_user_id_hash`: exactly 64 lowercase hex characters
- `session_id`: non-empty, no control characters `[\x00-\x1f\x7f]`, max 512 UTF-8 bytes
- `frontend_conversation_id`: non-empty after stripping, no `@` character
- Output: `plc_v1_` prefix + 64 hex chars = exactly 71 characters

## Error Handling

`ProcessLocalConversationKeyError` is opaque — its `__repr__` and `__str__`
never expose raw input values.  In `chat.py`, this exception is caught with a
dedicated handler that sets `fallback_recommended=False` (key validation
failures are hard errors, not transient pipeline failures).
