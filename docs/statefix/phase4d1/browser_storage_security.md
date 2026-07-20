# Phase 4D1 — Browser Storage Security

## Storage Layer

All browser-side persistence uses `localStorage` accessed exclusively through
`globalThis.localStorage` to allow Node.js test injection. All reads and writes
are wrapped in `try/catch` so storage faults are never surfaced to the user.

---

## Permitted Stored Fields

| Field | Type | Purpose |
|---|---|---|
| `version` | `number` | Schema migration guard |
| `activeConversationId` | `string` | Restore active tab on refresh |
| `conversations[].id` | `string` | Sidebar restoration |
| `conversations[].title` | `string` (optional) | Display-only label |
| `conversations[].createdAt` | `number` (optional) | Timestamp (ms, max 9e15) |

---

## Prohibited Stored Fields

The following are **never** written to `localStorage`:

| Category | Examples |
|---|---|
| Trusted user identity | Username, email, display name |
| Owner hash | `owner_hash` / `owner_key` (64-hex opaque value) |
| Session cookie or session ID | HTTP session token, any browser session identifier |
| Process-local key | `plc_v1_*` (opaque 71-char digest) |
| Genie conversation ID | `genie_conversation_id` field from backend responses |
| Genie message ID | `genie_message_id` / `last_genie_message_id` |
| SQL | Any generated or raw SQL query text |
| Credentials | API tokens, passwords, access keys |
| Raw backend response payloads | Full `ChatResponse` JSON |
| Table data | `table_data`, `latest_table_result`, result rows |
| Shipment data | Any LBN record, scorecard fields, shipment attributes |
| Error stack traces | JavaScript or Python stack trace text |

Enforcement: `saveLifecycleState()` constructs a `payload` object containing
only `version`, `activeConversationId`, and `conversations` (each stripped to
`{ id, title?, createdAt? }`). Extra fields supplied by the caller are silently
dropped because they are not copied into `payload`.

---

## Malformed / Invalid Storage Handling

| Error condition | Behaviour |
|---|---|
| Non-JSON or truncated JSON | `_parse()` catches `JSON.parse` exception; returns `null` |
| Not an object (array, primitive) | `_parse()` rejects; returns `null` |
| Unsupported schema version | `_parse()` rejects; returns `null` |
| Missing `activeConversationId` | `_parse()` rejects; returns `null` |
| `activeConversationId` contains `@` | `isValidConversationId()` returns `false`; rejected |
| `activeConversationId` contains control char | Same |
| `activeConversationId` empty string | Same |
| `activeConversationId` longer than 200 chars | Same |
| `conversations` not an array | `_parse()` rejects; returns `null` |
| Conversations list longer than 50 | `_parse()` rejects; `saveLifecycleState()` truncates to 50 |
| Duplicate conversation ID in list | `_parse()` rejects entire payload; `saveLifecycleState()` deduplicates |
| Conversation entry title longer than 100 chars | `_parse()` rejects; `saveLifecycleState()` truncates |
| Conversation entry `createdAt` invalid timestamp | `_parse()` rejects; `saveLifecycleState()` omits field |
| Serialised payload exceeds 16 KiB | Both `_parse()` and `saveLifecycleState()` reject |

---

## Storage Unavailability

| Error | Behaviour |
|---|---|
| `localStorage` property throws | `_getStorage()` returns `null`; all operations are no-ops |
| `localStorage` is `undefined` / `null` | `_getStorage()` returns `null`; same |
| `getItem()` throws (`SecurityError`) | `loadLifecycleState()` catches; returns `null` |
| `setItem()` throws (`QuotaExceededError`, `SecurityError`) | `saveLifecycleState()` catches; returns `false` |
| `removeItem()` throws | `clearLifecycleState()` catches; swallows silently |

In all unavailability cases, in-memory React state remains authoritative.
Chat functionality is unaffected.

---

## Write Timing

| Write site | Condition |
|---|---|
| `useEffect` on `[activeConvId, conversations]` | After every React state change (post-render) |
| Explicit call in `handleNewChat` | Immediately after HTTP 200 success, before React state update |

No `localStorage` writes occur at render time or before an HTTP 200 response.
