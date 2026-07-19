# Phase 4C4B4 — Frontend Reset Contract

## Overview

This document specifies the frontend conversation reset contract
implemented in Phase 4C4B4.

## Frontend Files Changed

| File | Change |
|------|--------|
| `frontend/src/App.jsx` | Added `useRef`, `useCallback`; `activeConvIdRef`; `isResetting`/`resetError` state; `activateConversation()`; `resetConversation()` API client; rewrote `handleNewChat` with reset-first flow; added response guard to `handleSendMessage` |
| `frontend/src/components/Sidebar.jsx` | Added `isResetting` prop; disabled New Chat button while resetting |
| `frontend/src/App.css` | Added `.reset-error-banner`, `.resetting-indicator`, `.new-chat-btn:disabled` styles |

## New Chat Ordering (Required)

```
1. User clicks New Chat
2. Guard: if isResetting → return (prevent duplicate)
3. Capture oldConversationId = activeConvIdRef.current
4. Set isResetting = true
5. POST /api/conversations/{encoded_old_id}/reset
6. If network error → set resetError, return (old conversation retained)
7. If !response.ok → set resetError from RESET_ERROR_MESSAGES map, return
8. On HTTP 200:
   a. Generate newId = _newConvId()
   b. Prepend new conversation to conversations state
   c. activateConversation(newId) — ref THEN state
   d. resetError = null
9. finally: isResetting = false
```

## Prohibited Ordering

- Generate new ID before backend confirms reset
- Clear UI before backend confirms reset
- Optimistically reset or clear state before HTTP 200

## Active-Conversation Ref Contract

- `activeConvIdRef` (React useRef): immediately mutable, synchronous
- `activeConvId` (React useState): reactive, for rendering
- `activateConversation(nextId)` updates ref FIRST, then state
- All async callbacks capture `requestConversationId = activeConvIdRef.current`
  at request initiation time
- Response processing guarded by:
  `activeConvIdRef.current === requestConversationId`

## Request-Conversation Capture

In `handleSendMessage`:
```javascript
const requestConversationId = activeConvIdRef.current;
// ... async fetch ...
if (activeConvIdRef.current !== requestConversationId) return; // discard
```

## Dedicated Resetting State

- `isResetting` is distinct from `isLoading`
- While true: New Chat disabled, message send blocked, UI preserved
- Always cleared in `finally` block

## HTTP Error Mapping

| Status | Frontend Message |
|--------|------------------|
| 400 | The current conversation could not be reset. |
| 401 | Your session could not be verified. Please refresh and try again. |
| 409 | The conversation could not be reset because it changed. Please try again. |
| 503 | Conversation reset is temporarily unavailable. Please try again. |
| Network/other | Conversation reset is temporarily unavailable. Please try again. |

## Success/Failure State Transitions

### Success
- Old conversation remains in sidebar (read-only history)
- New conversation created with empty messages
- activeConvIdRef.current = newId
- Input focused on new empty chat
- resetError cleared

### Failure
- Old conversation remains active
- Old messages remain visible
- No new ID generated
- resetError banner displayed (dismissible)
- User can retry or continue chatting
