# Phase 4C4B4 — Race Safety Contract

## Overview

This document specifies the race conditions covered by the frontend
conversation reset implementation and how each is mitigated.

## Core Mechanism: activeConvIdRef

A React `useRef` holds the authoritative active conversation ID.
Unlike `useState`, it is synchronously readable by any async callback
without waiting for a render cycle.

Every async operation captures `requestConversationId` at initiation
and guards its completion with:
```javascript
if (activeConvIdRef.current !== requestConversationId) return;
```

## Race Scenarios

### A. Old chat request finishes while reset is pending

- While reset is in-flight, old request may still update the old
  conversation display (because activeConvIdRef.current still equals
  the old ID until reset succeeds).
- Once reset succeeds and `activateConversation(newId)` fires,
  the ref changes immediately. Any still-pending old response
  will fail the guard and be discarded.

### B. Old request finishes after reset succeeds

- `activeConvIdRef.current` is now `newId`.
- The old response's `requestConversationId` is `oldId`.
- Guard fails → response discarded silently.
- Applies to: messages, tables, charts, suggestions, loading state.

### C. User double-clicks New Chat

- First click sets `isResetting = true`.
- Second click hits `if (isResetting) return;` guard immediately.
- Exactly one reset request is sent.

### D. User presses Enter while reset is pending

- `handleSendMessage` guard: `if (!text.trim() || isLoading || isResetting) return;`
- No message request is sent.
- Additionally, ChatWindow receives `isLoading={isLoading || isResetting}`,
  which disables the textarea and send button.

### E. Reset fails while an old message request is still active

- Reset failure does NOT change `activeConvIdRef.current`.
- Old conversation remains active.
- The in-flight old response's `requestConversationId` still matches
  the ref → it updates normally.

### F. Request submitted immediately after successful reset

- After `activateConversation(newId)`, `activeConvIdRef.current = newId`.
- `handleSendMessage` captures `requestConversationId = newId`.
- Uses the new conversation ID correctly.

### G. Reset response arrives after component teardown/navigation

- React state setters (`setIsResetting`, `setResetError`) are safe
  to call on unmounted components (React 18 suppresses the warning).
- No external side effects occur in the `finally` block.
- No unhandled promise rejection.

## Duplicate-Click Prevention

- `isResetting` state checked at start of `handleNewChat`.
- Button HTML: `<button disabled={isResetting}>` in Sidebar.
- Both mechanisms are defence-in-depth.

## Late-Response Handling Summary

| Response Type | Guard | Result When Stale |
|---------------|-------|-------------------|
| Assistant text | activeConvIdRef check | Discarded |
| Table data | Same check (same code path) | Discarded |
| Chart/viz | Same check | Discarded |
| Suggestions | Same check | Discarded |
| Generated SQL | Same check | Discarded |
| Loading state | Same check in finally | Not cleared |
| Error message | Same check in catch | Discarded |

## Old Conversation Inactivity (Step 10)

- After successful reset, `activeConvIdRef.current` is permanently changed.
- No function sends messages using the old ID.
- Old conversation data remains in `conversations` state (read-only sidebar).
- No automatic reactivation mechanism exists.
