# H1: Conversation History Persistence — Storage Architecture

## Overview

H1 adds secure owner-scoped message persistence so that browser refresh and tab
reopen rehydrate the chat UI from the backend instead of localStorage.

## Storage Backend

**Lakebase PostgreSQL** (`transparence_state.app_conversation_message`).
The table lives in the same Lakebase instance as `app_conversation`.
Connection is managed by `LakebaseConnectionProvider` (injected via the
existing `connections=-c search_path=transparence_state,public` pattern).

## Repository Layer

| File | Purpose |
|---|---|
| `app/services/message_repository.py` | Protocol interface, domain model, InMemory implementation |
| `app/services/lakebase_message_repository.py` | PostgreSQL adapter (parameterized SQL, injected connection) |
| `app/services/message_repository_runtime.py` | Process-level singleton, double-checked locking, feature flag |
| `app/services/message_history_service.py` | Payload builder, persistence orchestration, deactivation |

## Service Layer

`MessageHistoryService` (functions in `message_history_service.py`):
- `build_response_payload(genie_result)` — normalizes Genie result to a
  size-bounded JSON blob safe for storage.
- `parse_response_payload(json_str)` — safe deserializer (returns {} on error).
- `deactivate_conversation_messages(repo, ...)` — non-blocking; logs errors
  without raising so conversation reset is never blocked.

## API Layer

`GET /api/conversations/{frontend_conversation_id}/messages`  
Registered in `app/routes/conversation_history.py`, included in `app/main.py`.

## Frontend Layer

| File | Change |
|---|---|
| `frontend/src/utils/conversationHistoryLoader.js` | NEW: fetch history, map to frontend message schema |
| `frontend/src/App.jsx` | On mount and on select-conversation: load history for active conv |
| `frontend/src/components/ChatWindow.jsx` | Show skeleton spinner while history loading |
| `frontend/src/App.css` | Skeleton animation keyframes and classes |

## Data Flow (Happy Path)

1. User sends message → `chat.py` persists user message via `append_message`.
2. Genie processes → `chat.py` persists assistant response via `append_message`.
3. Browser reloads → `App.jsx` calls `loadConversationHistory(convId)`.
4. `conversationHistoryLoader.js` calls `GET /api/conversations/{id}/messages`.
5. `conversation_history.py` validates identity, checks ownership, queries repo.
6. Messages returned → `App.jsx` writes to conversation's message array.
7. `ChatWindow` renders historical messages (identical component path as live).

## Feature Flags

| Flag | Default | Meaning |
|---|---|---|
| `ENABLE_MESSAGE_HISTORY` | `false` | Master switch; history disabled unless explicitly set |
| `HISTORY_MAX_PAGE_SIZE` | `50` | Max messages per page (capped at 100) |
