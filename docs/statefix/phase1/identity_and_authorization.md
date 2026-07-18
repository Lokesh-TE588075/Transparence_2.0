# Identity and Authorization — Phase 1 Investigation

**Date:** 2026-07-18  
**Branch:** feature/genie-state-persistence  
**Baseline commit:** 7d9977ff5965b8ee015bda1cc1662a317ae8a0d0

---

## 1. Identity Header Matrix

| Identity value | Header / source | Current usage | Persistence usage | Security role |
|---|---|---|---|---|
| `X-Forwarded-Email` | HTTP header (Databricks Apps) | **chat.py line 186**: primary user ID for chat route (current code) | Phase 2: display label and audit field only; **not** the primary ownership key | Human-readable identity; not used as the durable owner hash |
| `X-User-Email` | HTTP header (local dev fallback) | **chat.py line 187**: fallback if X-Forwarded-Email absent; **feedback.py line 28**: only source read (inconsistency) | Same as above | Dev-only; not trusted in production |
| `X-Forwarded-User` | HTTP header | **NOT READ** in current source | Phase 2: **primary ownership key** — HMAC-SHA256(configured server secret, value) stored as `owner_user_id_hash` | Identifies the authenticated Databricks user by opaque subject ID; preferred over email for durable ownership |
| `X-Forwarded-Preferred-Username` | HTTP header | **NOT READ** anywhere in source | N/A | Not used |
| `X-Forwarded-Access-Token` | HTTP header | **NOT READ** — explicitly excluded by comment (chat.py line 233–237) | N/A | Cannot call Genie API due to IAM-only scopes |
| `X-Request-Id` | HTTP header | **NOT READ** | N/A | Not used |
| `transparence_session_id` | httponly cookie (set by session_middleware) | **main.py lines 64–90**: set on every response; read as `cookie_val` to identify server session | `session_id` is the first component of `server_conversation_key` | Distinguishes anonymous browser sessions |
| Browser UUID | React `crypto.randomUUID()` (App.jsx line 13–16) | Sent as `body.conversation_id` in every POST | `frontend_conversation_id` is the second component of `server_conversation_key` | Frontend conversation identity |
| `server_conversation_key` | Composite: `f"{session_id}:{frontend_conversation_id}"` (chat.py line 207) | Used as key into `GenieSessionStore._sessions` and `ConversationManager` | Phase 2: maps to `app_conversation` row | Namespaced backend identity |

## 2. Explicit Header Answers

1. **Is `X-Forwarded-User` currently read?** NO — not in current source. Phase 2 must read it as the **primary ownership identity**. `HMAC-SHA256(configured server secret, X-Forwarded-User)` becomes `owner_user_id_hash`. Fall back to `X-Forwarded-Email` only when `X-Forwarded-User` is unexpectedly absent. Do not hardcode the HMAC secret.
2. **Is `X-Forwarded-Email` currently read?** YES — chat.py line 186, primary.
3. **Is `X-Forwarded-Access-Token` currently read?** NO — explicitly excluded by design comment at chat.py line 232–237.
4. **Is the browser cookie used as the authoritative user/session identity?** YES (partly) — `session_id` from `transparence_session_id` cookie is the first component of `server_conversation_key`. However it is not tied to a Databricks identity; it is a random session token.
5. **Is the React conversation UUID used as part of the backend key?** YES — as the second component of `server_conversation_key`.
6. **Is the authenticated Databricks user part of the authoritative Genie state key?** NO — `X-Forwarded-Email` is logged as `user_id` but is NOT part of the `server_conversation_key`. The key is `{cookie_session_id}:{frontend_uuid}`, which does not include the email.
7. **Does feedback use the same identity-header logic as chat?** NO (inconsistency). Chat reads `X-Forwarded-Email` first; feedback reads only `X-User-Email` (line 28 feedback.py). In production (Databricks Apps), `X-User-Email` is not injected by the platform, so feedback `user_id` will always be `"anonymous"`.
8. **Do export endpoints validate user ownership?** NO — export.py has no identity header reads. Anyone with the `download_key` UUID can download any export.

## 3. Genie Authorization Path (Proven Code Trace)

```
chat.py line 238:
  _genie_pl = _get_genie_pipeline(user_token=None)
            ^———————————————— explicitly None

genie_backend_factory.py line 56:
  if user_token:  → False (user_token is None)
  → returns singleton _genie_pipeline (SP auth)

genie_backend_factory.py line 80–98 (_build_pipeline):
  client = GenieClient(
      host=settings.DATABRICKS_HOST,
      timeout_seconds=...,
      user_token="",  ← always empty
  )

genie_client.py _get_headers() priority order:
  1. self._user_token  → empty string, skipped
  2. DATABRICKS_TOKEN env var  → not set in app.yaml
  3. settings.DATABRICKS_TOKEN  → from SDK credential chain at startup
  4. self._ws.config.authenticate()  → SDK credential chain
     → In Databricks Apps: service principal credentials (auto-injected)

Result: ALL Genie API calls use the app service principal credentials.
```

**File/line references:**
- `app/routes/chat.py` line 238: `_get_genie_pipeline(user_token=None)`
- `app/routes/chat.py` lines 232–237: comment explaining why X-Forwarded-Access-Token is excluded
- `app/services/genie_backend_factory.py` lines 56–58: user_token guard
- `app/services/genie_backend_factory.py` lines 94–98: `GenieClient` construction
- `app/services/genie_client.py` lines 267–296: `_get_headers()` priority chain

## 4. User Permissions vs App SP Permissions

| Operation | Interactive user (`lokesh.choraria@te.com`) | App SP (`app-31pcl9 transparence`, ID 78664835752275) | Confirmed or inferred |
|---|---|---|---|
| View Lakebase project | Yes (API call succeeded) | Not configured | Confirmed (SDK call) / Inferred |
| Manage Lakebase project | Yes (project creator role) | Not configured | Inferred |
| Connect to database | Yes (creator gets superuser) | Not configured | Inferred |
| Create schema | Yes | Not configured | Inferred |
| Create table | Yes | Not configured | Inferred |
| SELECT | Yes | Not configured | Inferred |
| INSERT | Yes | Not configured | Inferred |
| UPDATE | Yes | Not configured | Inferred |
| DELETE | Yes | Not configured | Inferred |

**Note:** When a Lakebase resource is attached to the app with `CAN_CONNECT_AND_CREATE`, the SP automatically receives the Postgres role that allows connecting and creating new objects. The SP will own any schema it creates and will have full DML on objects in that schema.

## 5. Authorization Model Decision Input

### Model A — Shared Data Access (current design)

- `X-Forwarded-User` owns the conversation. `X-Forwarded-Email` is retained for display and audit logging only.
- App service principal calls Genie.
- Every authorised app user sees the same dataset through the same Genie Space permissions.
- Lakebase state is written and read by the app service principal.
- **Benefits:** Simple; no per-user token plumbing; works today with IAM-only scopes.
- **Risks:** No per-user data isolation; a compromised session can access any conversation via `download_key`.
- **Current prerequisites:** None — this is the current operating model.
- **Required app scopes:** No change needed.
- **Impact on code:** Minimal; only the Lakebase adapter is added.
- **Workspace support:** Full.

### Model B — User-Specific Data Access

- `X-Forwarded-User` or `X-Forwarded-Email` owns the conversation.
- `X-Forwarded-Access-Token` is forwarded to Genie and SQL queries.
- Unity Catalog row filters and column masks apply per user.
- Lakebase operational state is still managed by the app SP.
- **Benefits:** True per-user data isolation; UC ABAC enforced.
- **Risks:** Requires adding `databricks-genie` and `sql` scopes to `effective_user_api_scopes`; changes app.yaml authorization config; X-Forwarded-Access-Token is currently IAM-only and cannot call Genie.
- **Current prerequisites:** App scopes must include `databricks-genie` scope (requires app config change and redeployment). UC row filters on `lbn_with_scorecard` must be defined.
- **Required app scopes:** `databricks-genie`, `sql` (or equivalent data-plane scopes).
- **Impact on code:** Significant — `chat.py` must extract and forward `X-Forwarded-Access-Token`; `genie_backend_factory.py` user-token path must be activated; per-request GeniePipeline instances instead of singleton.
- **Workspace support:** Technically feasible but requires policy and config changes.

### Decision Required from Application Owner

> **“Should all authorised TransparencE users see the same shipment data, or must data access vary by user?”**

If all users see the same dataset → **proceed with Model A** (no scope changes required).  
If data must vary by user → **choose Model B** (requires scope change, app redeployment, and UC row-filter design before Phase 2 can begin).

**Recommendation for Phase 2:** Proceed with Model A. Model B can be layered on top later (the factory already has a `user_token` code path stub).

## 6. Recommended Durable User Identifier

For Lakebase persistence the authoritative identifier is:

> **`HMAC-SHA256(configured server secret, X-Forwarded-User)`**

This provides:
- Stable, opaque identity across sessions (`X-Forwarded-User` is a platform-assigned subject ID, not mutable like a display name or email)
- Privacy (HMAC output is not reversible without the server secret; the raw value is never stored)
- Consistent fixed-length hex string suitable as a primary key component
- Binding to a server secret so that hashes cannot be precomputed by an external party

**Fallback:** If `X-Forwarded-User` is unexpectedly absent, fall back to `X-Forwarded-Email` hashed the same way. Log a warning and treat the session as degraded.

`X-Forwarded-Email` is retained for audit logging and display only. It must never replace `X-Forwarded-User` as the primary ownership key.

Do not use the raw cookie `session_id` as the durable owner identifier; cookies are ephemeral and regenerated on new browser sessions.

Do not hardcode the HMAC secret. It must be injected via a secrets resource or environment variable.

---

## 7. Current Authorization Decision

**Recorded decision: Model A — Shared Data Access.**

| Dimension | Decision |
|---|---|
| Conversation ownership | `X-Forwarded-User` (primary); `X-Forwarded-Email` for display/audit |
| Genie API calls | App service principal via SDK credential chain |
| Lakebase operational state | Owned and managed by the app service principal |
| Dataset permissions | All authorised app users share the Genie Space permissions granted to the SP |
| Per-user data isolation | Not implemented. Model B is deferred. |

**This decision must be revisited if future users require business-unit, region, customer, row-level, or column-level data restrictions.** At that point Model B (user-delegated access via `X-Forwarded-Access-Token` and UC row filters / column masks) requires adding data-plane OAuth scopes to the app configuration and redeployment.
