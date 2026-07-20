/**
 * conversationLifecyclePersistence.js
 *
 * Browser-storage lifecycle persistence for conversation identity.
 *
 * Storage key:  transparence.conversation-lifecycle.v1
 * Schema version: 1
 *
 * PERMITTED stored fields:
 *   - schema version (number)
 *   - active frontend conversation ID (string)
 *   - minimal list of conversation descriptors: { id, title? }
 *
 * PROHIBITED stored fields:
 *   owner identity, owner hash, session ID/cookie,
 *   plc_v1_ process-local key, Genie conversation ID,
 *   Genie message ID, SQL, credentials, raw backend response payloads,
 *   raw table data, sensitive shipment information, error stack traces.
 *
 * All localStorage failures are caught and swallowed — storage unavailability
 * must never break chat operation.  In-memory state is always authoritative.
 *
 * Designed as a pure ES module with no React or DOM import dependencies so
 * that Node.js test runners can import it directly with a globalThis.localStorage
 * mock.
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const STORAGE_KEY = "transparence.conversation-lifecycle.v1";
export const SUPPORTED_VERSION = 1;

// Hard limits to prevent abuse or corruption
const _MAX_CONVERSATIONS      = 50;
const _MAX_ID_LENGTH          = 200;
const _MAX_LABEL_LENGTH       = 100;
const _MAX_STORAGE_BYTES      = 16384;   // 16 KiB safety cap
const _MAX_TIMESTAMP_VALUE    = 9e15;    // ~ year 2254 in milliseconds

// IDs must not contain ASCII control characters or the @ sign.
const _CONTROL_CHAR_RE = /[\x00-\x1f\x7f]/;
const _AT_CHAR_RE      = /@/;

// ---------------------------------------------------------------------------
// Storage accessor — isolates all localStorage access behind try/catch
// ---------------------------------------------------------------------------

/**
 * Return the active localStorage instance, or null when unavailable.
 *
 * Reads from globalThis.localStorage so Node.js tests can inject a mock
 * via `globalThis.localStorage = { ... }` before importing this module.
 */
function _getStorage() {
  try {
    // globalThis is defined in all modern environments (browser + Node 12+).
    const store =
      typeof globalThis !== "undefined" ? globalThis.localStorage : undefined;
    if (store == null) return null;
    return store;
  } catch (_) {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

/**
 * Return true when `id` is a valid, safe frontend conversation identifier.
 * A valid ID is a non-empty string containing no control characters or @.
 *
 * @param {unknown} id
 * @returns {boolean}
 */
export function isValidConversationId(id) {
  if (typeof id !== "string" || !id) return false;
  if (id.length > _MAX_ID_LENGTH) return false;
  if (_CONTROL_CHAR_RE.test(id)) return false;
  if (_AT_CHAR_RE.test(id)) return false;
  return true;
}

/**
 * Return true when `ts` is an acceptable creation timestamp.
 * Accepts null / undefined (field is optional) and finite positive numbers
 * up to _MAX_TIMESTAMP_VALUE.
 *
 * @param {unknown} ts
 * @returns {boolean}
 */
function _isValidTimestamp(ts) {
  if (ts === null || ts === undefined) return true;
  if (typeof ts !== "number") return false;
  return isFinite(ts) && ts > 0 && ts <= _MAX_TIMESTAMP_VALUE;
}

// ---------------------------------------------------------------------------
// Parse and validate raw storage content
// ---------------------------------------------------------------------------

/**
 * Parse and strictly validate the raw string from storage.
 *
 * Rejected when:
 *   - not valid JSON, or not an object
 *   - version !== SUPPORTED_VERSION
 *   - activeConversationId is missing or invalid
 *   - conversations is present but not an array, too large, has duplicates,
 *     has invalid entries, or has entries with invalid field types
 *   - the serialized size of the parsed object exceeds _MAX_STORAGE_BYTES
 *
 * @param {string|null} raw
 * @returns {{ version: number, activeConversationId: string, conversations: Array } | null}
 */
function _parse(raw) {
  if (!raw) return null;

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (_) {
    return null; // malformed JSON
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  if (parsed.version !== SUPPORTED_VERSION) return null;
  if (!isValidConversationId(parsed.activeConversationId)) return null;

  const convs = parsed.conversations;
  if (convs !== undefined) {
    if (!Array.isArray(convs)) return null;
    if (convs.length > _MAX_CONVERSATIONS) return null;

    const seen = new Set();
    for (const c of convs) {
      if (!c || typeof c !== "object" || Array.isArray(c)) return null;
      if (!isValidConversationId(c.id)) return null;
      if (seen.has(c.id)) return null; // duplicate
      seen.add(c.id);
      if (c.title !== undefined && c.title !== null) {
        if (typeof c.title !== "string") return null;
        if (c.title.length > _MAX_LABEL_LENGTH) return null;
      }
      if (!_isValidTimestamp(c.createdAt)) return null;
    }
  }

  // Re-serialise the clean structure to check size before returning
  const clean = {
    version: SUPPORTED_VERSION,
    activeConversationId: parsed.activeConversationId,
    conversations: (convs || []).map((c) => ({
      id: c.id,
      ...(c.title   != null ? { title:     String(c.title).slice(0, _MAX_LABEL_LENGTH) } : {}),
      ...(c.createdAt != null ? { createdAt: c.createdAt } : {}),
    })),
  };

  let sizeCheck;
  try {
    sizeCheck = JSON.stringify(clean);
  } catch (_) {
    return null;
  }
  if (sizeCheck.length > _MAX_STORAGE_BYTES) return null;

  return clean;
}

// ---------------------------------------------------------------------------
// Public read
// ---------------------------------------------------------------------------

/**
 * Load and validate persisted lifecycle state from storage.
 *
 * Returns the validated state object on success, or null when:
 *   - localStorage is unavailable
 *   - storage contains no entry for STORAGE_KEY
 *   - the stored value fails any validation check
 *
 * Never throws.
 *
 * @returns {{ version: number, activeConversationId: string, conversations: Array } | null}
 */
export function loadLifecycleState() {
  const store = _getStorage();
  if (!store) return null;

  let raw = null;
  try {
    raw = store.getItem(STORAGE_KEY);
  } catch (_) {
    return null;
  }

  return _parse(raw);
}

// ---------------------------------------------------------------------------
// Public write
// ---------------------------------------------------------------------------

/**
 * Serialise and persist lifecycle state to storage.
 *
 * Strips conversations to only permitted fields (id, title, createdAt).
 * Silently drops entries with invalid IDs.
 * Caps conversations list at _MAX_CONVERSATIONS.
 * Rejects states whose serialised size exceeds _MAX_STORAGE_BYTES.
 *
 * Returns true when the write succeeded, false on any failure.
 * Never throws.
 *
 * @param {{ activeConversationId: string, conversations?: Array }} state
 * @returns {boolean}
 */
export function saveLifecycleState(state) {
  if (!state || typeof state !== "object" || Array.isArray(state)) return false;
  if (!isValidConversationId(state.activeConversationId)) return false;

  const rawConvs = Array.isArray(state.conversations) ? state.conversations : [];
  const cleanConvs = rawConvs
    .filter((c) => c && isValidConversationId(c.id))
    .slice(0, _MAX_CONVERSATIONS)
    .map((c) => {
      const entry = { id: c.id };
      if (c.title != null && typeof c.title === "string" && c.title.length > 0) {
        entry.title = c.title.slice(0, _MAX_LABEL_LENGTH);
      }
      if (_isValidTimestamp(c.createdAt) && c.createdAt != null) {
        entry.createdAt = c.createdAt;
      }
      return entry;
    });

  // Deduplicate (preserve first occurrence)
  const seenIds = new Set();
  const dedupedConvs = cleanConvs.filter((c) => {
    if (seenIds.has(c.id)) return false;
    seenIds.add(c.id);
    return true;
  });

  const payload = {
    version: SUPPORTED_VERSION,
    activeConversationId: state.activeConversationId,
    conversations: dedupedConvs,
  };

  let json;
  try {
    json = JSON.stringify(payload);
  } catch (_) {
    return false;
  }

  if (json.length > _MAX_STORAGE_BYTES) return false;

  const store = _getStorage();
  if (!store) return false;

  try {
    store.setItem(STORAGE_KEY, json);
    return true;
  } catch (_) {
    // QuotaExceededError, SecurityError, or any other storage fault
    return false;
  }
}

// ---------------------------------------------------------------------------
// Public clear
// ---------------------------------------------------------------------------

/**
 * Remove the lifecycle entry from storage.
 * Safe no-op when storage is unavailable or the key does not exist.
 * Never throws.
 */
export function clearLifecycleState() {
  const store = _getStorage();
  if (!store) return;
  try {
    store.removeItem(STORAGE_KEY);
  } catch (_) {
    // Silent — storage unavailability must not break any caller
  }
}

// ---------------------------------------------------------------------------
// Convenience: remove one conversation from the persisted list
// ---------------------------------------------------------------------------

/**
 * Load the current state, remove `conversationId` from the list and (when
 * it is the active ID) clear the active pointer, then save.
 *
 * Used after a successful reset to ensure the tombstoned old ID is never
 * the active ID in persisted state.
 *
 * Returns true when the updated state was successfully saved.
 * Returns false when storage is unavailable or the ID is invalid.
 * Never throws.
 *
 * @param {string} conversationId
 * @returns {boolean}
 */
export function removeConversationFromLifecycleState(conversationId) {
  if (!isValidConversationId(conversationId)) return false;

  const current = loadLifecycleState();
  if (!current) return false;

  const filteredConvs = (current.conversations || []).filter(
    (c) => c.id !== conversationId
  );

  // If the removed ID was the active one, promote the first remaining entry.
  // This handles edge-cases where storage is stale; App.jsx has already
  // activated the new ID in memory so this is defensive only.
  const newActiveId =
    current.activeConversationId === conversationId
      ? filteredConvs.length > 0
        ? filteredConvs[0].id
        : null
      : current.activeConversationId;

  if (!newActiveId || !isValidConversationId(newActiveId)) {
    // Cannot persist a null or invalid active ID — clear instead.
    clearLifecycleState();
    return false;
  }

  return saveLifecycleState({
    activeConversationId: newActiveId,
    conversations: filteredConvs,
  });
}
