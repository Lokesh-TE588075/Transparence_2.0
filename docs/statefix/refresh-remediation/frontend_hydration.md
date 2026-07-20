# Frontend Hydration Contract

## Purpose

`frontend/src/App.jsx` now restores only sidebar-safe lifecycle metadata after browser refresh.

## Hydration behaviour

On startup, App now:

* loads persisted lifecycle state through `loadLifecycleState()`
* restores only valid conversation descriptors from persisted storage
* rejects invalid entries, duplicate IDs, and malformed objects
* keeps the active frontend conversation ID only when it remains valid and present in the restored list
* falls back to the first restored conversation when the stored active ID is absent
* creates a fresh conversation only when no valid persisted identity remains

## Title handling

Titles are restored only when they are valid non-empty strings.

If a persisted title is absent or invalid, hydration uses:

* `"New conversation"`

This matches the browser-storage contract: arbitrary non-string values are not coerced into stored labels, and object-string artifacts such as `[object Object]` are not persisted.

## Security boundary

Hydration restores identity metadata only.

Visible user/assistant message history intentionally initializes empty after refresh:

* `messages: []`
* no prompt text restored
* no assistant text restored
* no tables restored
* no charts restored
* no SQL restored
* no Genie payloads restored
* no shipment result data restored

This is intentional for the current correction and preserves the requirement that browser storage must not persist prompts, responses, tables, charts, or shipment results.

## Current refresh result

The following now survive refresh:

* sidebar conversation IDs
* sidebar conversation titles when valid
* the active frontend conversation ID

The following do not survive refresh by design:

* visible message transcript
* table/chart attachments
* backend payloads

## Restart result

After the controlled profile is active, durable Genie context is expected to survive process restart through the Lakebase-backed repository and durable adapter path. That restart durability is server-side and is separate from browser refresh hydration.

## Recommended future history direction

Do not extend localStorage beyond the current identity metadata.

If full visible history is later approved, use an owner-scoped backend retrieval design with trusted identity validation and sanitized server-side response shaping rather than browser-side transcript persistence.
