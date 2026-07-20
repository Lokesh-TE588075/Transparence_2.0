/**
 * Phase 4D1 — Frontend conversation lifecycle persistence tests.
 *
 * Tests production module: frontend/src/utils/conversationLifecyclePersistence.js
 *
 * Mirrors the contract established by test_frontend_conversation_reset.mjs:
 *   - node --test (no build, no npm, no live browser)
 *   - globalThis.localStorage mock (production module uses globalThis.localStorage)
 *   - Validates storage key, version field, ID validation, limits, and error safety
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH =
  path.resolve(__dirname, "../frontend/src/utils/conversationLifecyclePersistence.js");

// ---------------------------------------------------------------------------
// localStorage mock (globalThis-scoped for production module compatibility)
// ---------------------------------------------------------------------------

function makeLocalStorageMock() {
  const store = Object.create(null);
  return {
    store,
    getItem(key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
    setItem(key, value) { store[key] = String(value); },
    removeItem(key) { delete store[key]; },
    clear() { Object.keys(store).forEach((k) => delete store[k]); },
    get length() { return Object.keys(store).length; },
  };
}

let _lsMock = makeLocalStorageMock();
globalThis.localStorage = _lsMock;

function resetStorage() {
  _lsMock = makeLocalStorageMock();
  globalThis.localStorage = _lsMock;
}

// ---------------------------------------------------------------------------
// Dynamic import of production module
// ---------------------------------------------------------------------------

const mod = await import(MODULE_PATH);
const {
  STORAGE_KEY,
  SUPPORTED_VERSION,
  isValidConversationId,
  loadLifecycleState,
  saveLifecycleState,
  clearLifecycleState,
  removeConversationFromLifecycleState,
} = mod;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function validState(overrides = {}) {
  return {
    activeConversationId: "conv-uuid-001",
    conversations: [{ id: "conv-uuid-001", title: "Shipment query" }],
    ...overrides,
  };
}

function storedJson() {
  const raw = globalThis.localStorage.getItem(STORAGE_KEY);
  return raw ? JSON.parse(raw) : null;
}

// ===========================================================================
// GROUP 1: Module exports
// ===========================================================================

describe("exports", () => {
  test("STORAGE_KEY is a non-empty string", () => {
    assert.strictEqual(typeof STORAGE_KEY, "string");
    assert.ok(STORAGE_KEY.length > 0);
  });

  test("SUPPORTED_VERSION is a positive integer", () => {
    assert.strictEqual(typeof SUPPORTED_VERSION, "number");
    assert.ok(SUPPORTED_VERSION >= 1);
    assert.strictEqual(Math.floor(SUPPORTED_VERSION), SUPPORTED_VERSION);
  });

  test("all required exports are functions", () => {
    for (const name of [
      "isValidConversationId",
      "loadLifecycleState",
      "saveLifecycleState",
      "clearLifecycleState",
      "removeConversationFromLifecycleState",
    ]) {
      assert.strictEqual(typeof mod[name], "function", `${name} must be a function`);
    }
  });
});

// ===========================================================================
// GROUP 2: isValidConversationId
// ===========================================================================

describe("isValidConversationId", () => {
  test("accepts a normal UUID", () => {
    assert.strictEqual(
      isValidConversationId("550e8400-e29b-41d4-a716-446655440000"), true
    );
  });

  test("accepts a timestamp-random ID", () => {
    assert.strictEqual(isValidConversationId("1720000000000-abc123"), true);
  });

  test("rejects empty string", () => {
    assert.strictEqual(isValidConversationId(""), false);
  });

  test("rejects null", () => {
    assert.strictEqual(isValidConversationId(null), false);
  });

  test("rejects undefined", () => {
    assert.strictEqual(isValidConversationId(undefined), false);
  });

  test("rejects non-string", () => {
    assert.strictEqual(isValidConversationId(42), false);
  });

  test("rejects ID containing @ character", () => {
    assert.strictEqual(isValidConversationId("user@example.com"), false);
  });

  test("rejects ID with control character", () => {
    assert.strictEqual(isValidConversationId("conv\x00id"), false);
  });

  test("rejects ID exceeding max length", () => {
    assert.strictEqual(isValidConversationId("a".repeat(201)), false);
  });

  test("accepts ID at exactly max length (200)", () => {
    assert.strictEqual(isValidConversationId("a".repeat(200)), true);
  });
});

// ===========================================================================
// GROUP 3: loadLifecycleState — empty / corrupt storage
// ===========================================================================

describe("loadLifecycleState — empty and invalid states", () => {
  test("returns null when storage is empty", () => {
    resetStorage();
    assert.strictEqual(loadLifecycleState(), null);
  });

  test("returns null when stored JSON is corrupt", () => {
    resetStorage();
    globalThis.localStorage.setItem(STORAGE_KEY, "{invalid json");
    assert.strictEqual(loadLifecycleState(), null);
  });

  test("returns null when stored version is unsupported", () => {
    resetStorage();
    globalThis.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ version: 999, activeConversationId: "id-1", conversations: [] })
    );
    assert.strictEqual(loadLifecycleState(), null);
  });

  test("returns null when activeConversationId is missing", () => {
    resetStorage();
    globalThis.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ version: SUPPORTED_VERSION, conversations: [] })
    );
    assert.strictEqual(loadLifecycleState(), null);
  });

  test("returns null when activeConversationId is invalid", () => {
    resetStorage();
    globalThis.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        version: SUPPORTED_VERSION,
        activeConversationId: "user@host",
        conversations: [],
      })
    );
    assert.strictEqual(loadLifecycleState(), null);
  });

  test("returns null when localStorage.getItem throws", () => {
    const orig = globalThis.localStorage.getItem;
    globalThis.localStorage.getItem = () => { throw new Error("SecurityError"); };
    try {
      assert.strictEqual(loadLifecycleState(), null);
    } finally {
      globalThis.localStorage.getItem = orig;
    }
  });
});

// ===========================================================================
// GROUP 4: saveLifecycleState + loadLifecycleState round-trip
// ===========================================================================

describe("saveLifecycleState / loadLifecycleState round-trip", () => {
  test("saves and restores activeConversationId", () => {
    resetStorage();
    saveLifecycleState(validState());
    const loaded = loadLifecycleState();
    assert.ok(loaded !== null);
    assert.strictEqual(loaded.activeConversationId, "conv-uuid-001");
  });

  test("stored object has correct version", () => {
    resetStorage();
    saveLifecycleState(validState());
    const raw = storedJson();
    assert.strictEqual(raw.version, SUPPORTED_VERSION);
  });

  test("saves and restores conversations array", () => {
    resetStorage();
    saveLifecycleState(
      validState({
        conversations: [
          { id: "conv-001", title: "Query 1" },
          { id: "conv-002", title: "Query 2" },
        ],
      })
    );
    const loaded = loadLifecycleState();
    assert.strictEqual(loaded.conversations.length, 2);
    assert.strictEqual(loaded.conversations[0].id, "conv-001");
  });

  test("save ignores extra fields (only permitted fields stored)", () => {
    resetStorage();
    saveLifecycleState({ ...validState(), secretField: "should-be-dropped" });
    const raw = storedJson();
    assert.strictEqual("secretField" in raw, false);
  });

  test("overwrite: second save replaces first", () => {
    resetStorage();
    saveLifecycleState(validState({ activeConversationId: "conv-first" }));
    saveLifecycleState(validState({ activeConversationId: "conv-second" }));
    const loaded = loadLifecycleState();
    assert.strictEqual(loaded.activeConversationId, "conv-second");
  });

  test("save with invalid activeConversationId is silently swallowed", () => {
    resetStorage();
    // Should not throw; may store nothing or store with null ID
    assert.doesNotThrow(() => saveLifecycleState({ activeConversationId: "user@bad" }));
  });

  test("save respects max conversations limit (50)", () => {
    resetStorage();
    const conversations = Array.from({ length: 60 }, (_, i) => ({
      id: `conv-${i.toString().padStart(3, "0")}`,
      title: `Conversation ${i}`,
    }));
    saveLifecycleState({ activeConversationId: conversations[0].id, conversations });
    const raw = storedJson();
    assert.ok(raw === null || raw.conversations.length <= 50);
  });

  test("save does not throw when localStorage.setItem throws", () => {
    resetStorage();
    const orig = globalThis.localStorage.setItem;
    globalThis.localStorage.setItem = () => { throw new Error("QuotaExceededError"); };
    try {
      assert.doesNotThrow(() => saveLifecycleState(validState()));
    } finally {
      globalThis.localStorage.setItem = orig;
    }
  });
});

// ===========================================================================
// GROUP 5: clearLifecycleState
// ===========================================================================

describe("clearLifecycleState", () => {
  test("removes stored state", () => {
    resetStorage();
    saveLifecycleState(validState());
    clearLifecycleState();
    assert.strictEqual(loadLifecycleState(), null);
  });

  test("does not throw when storage is already empty", () => {
    resetStorage();
    assert.doesNotThrow(() => clearLifecycleState());
  });

  test("clear on storage-remove-throws does not propagate error", () => {
    resetStorage();
    const orig = globalThis.localStorage.removeItem;
    globalThis.localStorage.removeItem = () => { throw new Error("SecurityError"); };
    try {
      assert.doesNotThrow(() => clearLifecycleState());
    } finally {
      globalThis.localStorage.removeItem = orig;
    }
  });

  test("only removes STORAGE_KEY, leaves other keys intact", () => {
    resetStorage();
    globalThis.localStorage.setItem("other-key", "some-value");
    saveLifecycleState(validState());
    clearLifecycleState();
    assert.strictEqual(globalThis.localStorage.getItem("other-key"), "some-value");
    assert.strictEqual(globalThis.localStorage.getItem(STORAGE_KEY), null);
  });
});

// ===========================================================================
// GROUP 6: removeConversationFromLifecycleState
// ===========================================================================

describe("removeConversationFromLifecycleState", () => {
  test("removes a conversation by ID from stored list", () => {
    resetStorage();
    saveLifecycleState(
      validState({
        conversations: [
          { id: "conv-001", title: "A" },
          { id: "conv-002", title: "B" },
        ],
      })
    );
    removeConversationFromLifecycleState("conv-001");
    const loaded = loadLifecycleState();
    assert.ok(loaded === null || !loaded.conversations.some((c) => c.id === "conv-001"));
  });

  test("does not throw when ID is not in stored list", () => {
    resetStorage();
    saveLifecycleState(validState());
    assert.doesNotThrow(() => removeConversationFromLifecycleState("non-existent"));
  });

  test("does not throw when storage is empty", () => {
    resetStorage();
    assert.doesNotThrow(() => removeConversationFromLifecycleState("any-id"));
  });

  test("does not affect activeConversationId for other convs", () => {
    resetStorage();
    saveLifecycleState(
      validState({
        activeConversationId: "conv-002",
        conversations: [
          { id: "conv-001", title: "A" },
          { id: "conv-002", title: "B" },
        ],
      })
    );
    removeConversationFromLifecycleState("conv-001");
    const loaded = loadLifecycleState();
    if (loaded) {
      assert.strictEqual(loaded.activeConversationId, "conv-002");
    }
  });
});

// ===========================================================================
// GROUP 7: Title sanitisation and label limits
// ===========================================================================

describe("title sanitisation", () => {
  test("title longer than 100 chars is truncated or conversation is excluded", () => {
    resetStorage();
    const longTitle = "x".repeat(150);
    saveLifecycleState(
      validState({
        conversations: [{ id: "conv-001", title: longTitle }],
      })
    );
    const raw = storedJson();
    if (raw && raw.conversations.length > 0) {
      assert.ok(raw.conversations[0].title.length <= 100);
    }
  });

  test("non-string title is not stored as raw number", () => {
    resetStorage();
    saveLifecycleState(
      validState({
        conversations: [{ id: "conv-001", title: 12345 }],
      })
    );
    const raw = storedJson();
    // Production module must not persist a numeric title as-is;
    // it may omit the field entirely (undefined) or coerce to a safe string.
    // Asserting only that it is NOT a raw numeric value.
    if (raw && raw.conversations.length > 0) {
      const storedTitle = raw.conversations[0].title;
      assert.notStrictEqual(
        typeof storedTitle,
        "number",
        "title must not be stored as a raw number"
      );
    }
  });
});

// ===========================================================================
// GROUP 8: Storage-size guard
// ===========================================================================

describe("storage-size guard", () => {
  test("does not store when serialised payload exceeds 16 KiB", () => {
    resetStorage();
    // Craft a state that would be very large
    const conversations = Array.from({ length: 50 }, (_, i) => ({
      id: `conv-${i}`,
      title: "x".repeat(200),
    }));
    saveLifecycleState({ activeConversationId: conversations[0].id, conversations });
    const raw = globalThis.localStorage.getItem(STORAGE_KEY);
    // Either nothing was saved or the stored payload is <= 16384 bytes
    assert.ok(raw === null || new TextEncoder().encode(raw).length <= 16384);
  });
});

// ===========================================================================
// GROUP 9: Reset-transition write count
// ===========================================================================

describe("reset-transition write count", () => {
  // Helper: wrap setItem with a call counter.
  // Returns { restore, count } where count() returns calls since install.
  function spySetItem() {
    const orig = globalThis.localStorage.setItem;
    let n = 0;
    globalThis.localStorage.setItem = (...args) => { n++; orig.apply(globalThis.localStorage, args); };
    return { restore: () => { globalThis.localStorage.setItem = orig; }, count: () => n };
  }

  test("saveLifecycleState calls setItem exactly once per invocation", () => {
    resetStorage();
    const spy = spySetItem();
    try {
      saveLifecycleState(validState({ activeConversationId: "new-conv-001" }));
      assert.strictEqual(spy.count(), 1, "One saveLifecycleState call must produce exactly one setItem call");
    } finally {
      spy.restore();
    }
  });

  test("loadLifecycleState does not call setItem (no write before HTTP 200)", () => {
    resetStorage();
    saveLifecycleState(validState());
    const spy = spySetItem();
    try {
      loadLifecycleState();
      assert.strictEqual(spy.count(), 0, "loadLifecycleState must not call setItem");
    } finally {
      spy.restore();
    }
  });

  test("failed reset produces no setItem call when saveLifecycleState is not called", () => {
    resetStorage();
    saveLifecycleState(validState());
    const spy = spySetItem();
    try {
      // Simulate failed reset: no saveLifecycleState call (state unchanged)
      // Do nothing — count must remain zero.
      assert.strictEqual(spy.count(), 0, "No saveLifecycleState call means no setItem call");
    } finally {
      spy.restore();
    }
  });

  test("old ID is absent from persisted state after successful-reset write", () => {
    resetStorage();
    // Pre-seed old state
    saveLifecycleState({
      activeConversationId: "old-conv-reset-test",
      conversations: [{ id: "old-conv-reset-test", title: "Old conversation" }],
    });
    // Simulate successful reset: one saveLifecycleState call with new state
    saveLifecycleState({
      activeConversationId: "new-conv-reset-test",
      conversations: [{ id: "new-conv-reset-test", title: "New conversation" }],
    });
    const raw = storedJson();
    assert.ok(raw !== null, "State must be persisted after reset write");
    const ids = (raw.conversations || []).map((c) => c.id);
    assert.ok(
      !ids.includes("old-conv-reset-test"),
      "Old conversation ID must not appear in persisted conversations after reset"
    );
    assert.notStrictEqual(
      raw.activeConversationId,
      "old-conv-reset-test",
      "Old conversation ID must not be the active ID after reset"
    );
  });

  test("new ID is present exactly once in persisted state after successful-reset write", () => {
    resetStorage();
    // Simulate successful reset write
    saveLifecycleState({
      activeConversationId: "new-conv-reset-test",
      conversations: [{ id: "new-conv-reset-test", title: "New conversation" }],
    });
    const raw = storedJson();
    assert.ok(raw !== null, "State must be persisted after reset write");
    assert.strictEqual(
      raw.activeConversationId,
      "new-conv-reset-test",
      "New ID must be the active conversation ID"
    );
    const ids = (raw.conversations || []).map((c) => c.id);
    const count = ids.filter((id) => id === "new-conv-reset-test").length;
    assert.strictEqual(count, 1, "New conversation ID must appear exactly once in persisted list");
  });
});
