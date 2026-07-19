# Phase 4C4A — Current Reset Flow

## Document Purpose

This document records the verified current behaviour of the "New Chat"
action across frontend and backend as of the inspection date.  No code was
modified to produce this document.

---

## 1. Frontend New Chat Lifecycle

### 1.1 Event Handler

**File:** `frontend/src/App.jsx`  
**Function:** `handleNewChat` (line 32)  
**Trigger:** `Sidebar` component `onNew` prop → `<button className="new-chat-btn" onClick={onNew}>`

### 1.2 Conversation ID Generation

**Function:** `_newConvId()` (line 12)  
**Primary:** `crypto.randomUUID()` — standard UUID v4, 36-char string, RFC 4122.  
**Fallback:** `${Date.now()}-${Math.random().toString(36).slice(2)}` for browsers without Web Crypto API.

### 1.3 Complete handleNewChat Behaviour

```javascript
const handleNewChat = () => {
  const id = _newConvId();                                    // (a)
  const newConv = { id, title: "New conversation", messages: [] };  // (b)
  setConversations(prev => [newConv, ...prev]);               // (c)
  setActiveConvId(id);                                         // (d)
};
```

1. **(a)** A new UUID v4 is generated.  It is never reused from a prior conversation.
2. **(b)** A new conversation object is constructed with empty messages.
3. **(c)** The new conversation is prepended to the `conversations` array.
4. **(d)** The active conversation switches to the new ID.

### 1.4 Storage and Persistence

| Question | Answer |
|----------|--------|
| Is the ID persisted in localStorage? | **No.** No localStorage or sessionStorage calls exist in App.jsx. |
| Is the ID persisted in sessionStorage? | **No.** |
| Does page refresh retain the ID? | **No.** `_newConvId()` is called inline during component initialization (`const _initialId = _newConvId()` line 20). Every page load generates a fresh ID. |
| Do multiple browser tabs share the ID? | **No.** Each tab has independent React state.  No cross-tab communication (BroadcastChannel, SharedWorker, storage events) exists. |
| Is browser history cleared? | **No.** No `history.pushState` or `history.replaceState` calls. |

### 1.5 Message Clearing

Displayed messages are cleared by construction: the new conversation
object has `messages: []`.  The old conversation remains in the
`conversations` array (sidebar list) with its messages intact and can be
re-selected.

### 1.6 Backend Communication on New Chat

**No HTTP request is sent to the backend when New Chat is clicked.**

The frontend performs only React state mutations.  The backend is
contacted only when the user subsequently sends a message via
`handleSendMessage` → `POST /api/chat`.

### 1.7 In-Flight Request Cancellation

**No cancellation occurs.**  `handleSendMessage` uses a plain `fetch()`
call with no `AbortController`.  If a request is in flight when New Chat
is clicked, that request continues to its natural completion.  Its
response handler updates the old conversation (because it captures
`activeConvId` from closure at send time), not the new one.

However, there is a **race condition**: the `isLoading` state is global
(not per-conversation).  The loading spinner will remain visible on the
new conversation until the old request completes.  No functional harm
occurs because the response updates the old conversation object (matched
by `c.id === activeConvId` captured at send time).

**Phase 4C4B requirement:** After reset succeeds and a new conversation ID
is activated, the `setIsLoading(false)` call from the old response must be
guarded against clearing the new conversation’s loading state.

Exact mechanism: `activeConvIdRef = useRef(activeConvId)` (synchronised via
`useEffect`).  In `handleSendMessage`, capture `requestConversationId` at
send time.  In `finally`/`catch`, guard: `if (activeConvIdRef.current ===
requestConversationId) setIsLoading(false)`.  This prevents an old request’s
resolution from affecting the newly active conversation’s UI state.

See `reset_semantics_decision.md` section 7.10 for full specification.

**Additional ref requirement:** The `activeConvIdRef` must be updated
synchronously (not only via `useEffect`). A helper `activateConversation(id)`
sets `activeConvIdRef.current = id` before calling `setActiveConvId(id)`.
This eliminates the window between state commit and effect execution where
a concurrent `finally` could read the stale ref.

### 1.7.1 Reset-versus-MISS Race Awareness

The current `handleNewChat` (line 32) is purely client-side. Phase 4C4B adds
a backend reset call that creates a durable RESET tombstone before generating
the new ID. This tombstone prevents any in-flight chat request from
creating a new ACTIVE durable record for the old frontend ID.

Without the tombstone, the following race is possible:
1. User clicks New Chat while a chat request is mid-flight.
2. Reset loads no durable record (the request hasn’t written back yet).
3. Reset returns success without occupying the logical key.
4. In-flight request completes and persists a new ACTIVE record.
5. Old ID becomes durable and recoverable after restart.

See `concurrency_and_failure_policy.md` Section 8.1 for the full proof.

### 1.8 Late Response Cross-Contamination

**Cannot occur under current code.**  Line 99:
```javascript
setConversations(prev => prev.map(c => {
  if (c.id !== activeConvId) return c;  // activeConvId captured at send time
  ...
}));
```
The `activeConvId` in the closure is the OLD conversation ID.  Messages
are appended only to the conversation that initiated the request.

### 1.9 Logout Relationship

No logout mechanism exists in the frontend code.  There is no
authentication UI or session-clearing handler.

---

## 2. Backend State on New Chat

### 2.1 No Reset Endpoint

There is no `POST /api/reset`, `POST /api/conversations/.../reset`, or
any equivalent endpoint.  The backend has no awareness that a "New Chat"
event occurred.

### 2.2 GenieSessionStore In-Memory Behaviour

When the first message arrives on the new conversation ID, the
`GenieSessionStore._get_or_create_session()` method (line 135) is called:

```python
def _get_or_create_session(self, app_conversation_id: str) -> GenieSession:
    now = datetime.now(timezone.utc)
    session = self._sessions.get(app_conversation_id)
    if session is None or session.is_expired(now) or not session.is_active:
        session = self._new_session(app_conversation_id, now)
        self._sessions[app_conversation_id] = session
        return session
    session.updated_at = now
    session.expires_at = self._expiry(now)
    return session
```

Since the new UUID has never been seen before, `session` is `None`, and a
fresh `GenieSession` is created.  The old session for the previous
conversation ID remains in memory (unexpired) until TTL cleanup.

### 2.3 Durable State (When Enabled)

The durable adapter's `get_or_create(key)` calls
`repository.create_conversation()`.  Since the frontend always generates
a **new** UUID, the logical key `(owner_hash, new_uuid)` has never been
registered.  A fresh ACTIVE record is created at version 1.

The **old** conversation's durable record remains ACTIVE until explicitly
transitioned.  No automatic cleanup or status transition occurs.

---

## 3. Summary of Current Gaps

| Gap | Impact |
|-----|--------|
| No backend notification on New Chat | Old durable record stays ACTIVE indefinitely |
| No in-flight cancellation | isLoading spinner bleeds to new conversation UI |
| No durable reset of old conversation | Stale ACTIVE records accumulate |
| Old Genie session remains in memory | Memory pressure on long-running processes |
| No multi-tab coordination | Independent conversations per tab (acceptable) |
| No localStorage persistence | Page refresh loses all conversation history (acceptable for MVP) |

---

*Phase 4C4A — inspection only.  No code modified.*
