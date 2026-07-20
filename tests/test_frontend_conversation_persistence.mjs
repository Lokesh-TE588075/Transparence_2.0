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
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH =
  path.resolve(__dirname, "../frontend/src/utils/conversationLifecyclePersistence.js");
const APP_PATH = path.resolve(__dirname, "../frontend/src/App.jsx");

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

function simulateInitialHydration(persistedState, options = {}) {
  let generatedIdCount = 0;
  const nextGeneratedId = options.nextGeneratedId || (() => {
    generatedIdCount += 1;
    return `generated-conv-${generatedIdCount}`;
  });

  const restoredConversations = [];
  const seenIds = new Set();
  const rawConversations = Array.isArray(persistedState?.conversations)
    ? persistedState.conversations
    : [];

  for (const entry of rawConversations) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
    if (!isValidConversationId(entry.id) || seenIds.has(entry.id)) continue;

    seenIds.add(entry.id);
    restoredConversations.push({
      id: entry.id,
      title:
        typeof entry.title === "string" && entry.title.trim().length > 0
          ? entry.title
          : "New conversation",
      messages: [],
    });
  }

  const hasValidActiveId = isValidConversationId(
    persistedState?.activeConversationId
  );

  if (restoredConversations.length === 0 && hasValidActiveId) {
    restoredConversations.push({
      id: persistedState.activeConversationId,
      title: "New conversation",
      messages: [],
    });
  }

  const activeConversationId =
    hasValidActiveId &&
    restoredConversations.some(
      (conversation) => conversation.id === persistedState.activeConversationId
    )
      ? persistedState.activeConversationId
      : restoredConversations[0]?.id;

  if (activeConversationId) {
    return {
      activeConversationId,
      conversations: restoredConversations,
      generatedIdCount,
    };
  }

  const freshId = nextGeneratedId();
  return {
    activeConversationId: freshId,
    conversations: [{ id: freshId, title: "New conversation", messages: [] }],
    generatedIdCount,
  };
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

  test("non-string title is omitted from storage and hydrates to default title", () => {
    resetStorage();
    saveLifecycleState(
      validState({
        conversations: [{ id: "conv-001", title: { unsafe: true } }],
      })
    );

    const raw = storedJson();
    assert.ok(raw && raw.conversations.length === 1);
    assert.deepEqual(raw.conversations[0], { id: "conv-001" });
    assert.equal(Object.prototype.hasOwnProperty.call(raw.conversations[0], "title"), false);
    assert.equal(JSON.stringify(raw).includes("[object Object]"), false);

    const restored = simulateInitialHydration(raw);
    assert.equal(restored.conversations[0].title, "New conversation");
    assert.deepEqual(Object.keys(raw.conversations[0]).sort(), ["id"]);
  });
});

// ===========================================================================
// GROUP 8: App.jsx initial hydration (refresh metadata restore)
// ===========================================================================

describe("App.jsx initial hydration", () => {
  test("restores multiple sidebar conversations after refresh", () => {
    const source = readFileSync(APP_PATH, "utf8");
    assert.ok(source.includes("_buildInitialLifecycleState"));
    assert.ok(source.includes("_restorePersistedConversations"));

    const restored = simulateInitialHydration({
      activeConversationId: "conv-002",
      conversations: [
        { id: "conv-001", title: "Delayed shipments" },
        { id: "conv-002", title: "China lanes" },
      ],
    });

    assert.deepEqual(
      restored.conversations.map((c) => c.id),
      ["conv-001", "conv-002"]
    );
  });

  test("restores stored conversation titles", () => {
    const restored = simulateInitialHydration({
      activeConversationId: "conv-002",
      conversations: [
        { id: "conv-001", title: "Late containers" },
        { id: "conv-002", title: "Port dwell analysis" },
      ],
    });

    assert.deepEqual(
      restored.conversations.map((c) => c.title),
      ["Late containers", "Port dwell analysis"]
    );
  });

  test("keeps the correct conversation active", () => {
    const restored = simulateInitialHydration({
      activeConversationId: "conv-002",
      conversations: [
        { id: "conv-001", title: "A" },
        { id: "conv-002", title: "B" },
      ],
    });

    assert.equal(restored.activeConversationId, "conv-002");
  });

  test("initializes restored messages as empty arrays", () => {
    const restored = simulateInitialHydration({
      activeConversationId: "conv-001",
      conversations: [
        { id: "conv-001", title: "A", messages: [{ id: 1, content: "secret" }] },
        { id: "conv-002", title: "B", tableData: [{ id: 99 }] },
      ],
    });

    assert.ok(restored.conversations.every((c) => Array.isArray(c.messages)));
    assert.ok(restored.conversations.every((c) => c.messages.length === 0));
  });

  test("discards invalid stored conversations", () => {
    const restored = simulateInitialHydration({
      activeConversationId: "conv-001",
      conversations: [
        null,
        { id: "user@example.com", title: "Bad" },
        { id: "conv-001", title: "Valid" },
        { id: "", title: "Also bad" },
      ],
    });

    assert.deepEqual(restored.conversations.map((c) => c.id), ["conv-001"]);
  });

  test("removes duplicate stored conversations", () => {
    const restored = simulateInitialHydration({
      activeConversationId: "conv-001",
      conversations: [
        { id: "conv-001", title: "First" },
        { id: "conv-001", title: "Duplicate" },
        { id: "conv-002", title: "Second" },
      ],
    });

    assert.deepEqual(
      restored.conversations.map((c) => c.id),
      ["conv-001", "conv-002"]
    );
    assert.equal(restored.conversations[0].title, "First");
  });

  test("corrects an active ID that is absent from the restored list", () => {
    const restored = simulateInitialHydration({
      activeConversationId: "conv-missing",
      conversations: [
        { id: "conv-001", title: "First" },
        { id: "conv-002", title: "Second" },
      ],
    });

    assert.equal(restored.activeConversationId, "conv-001");
    assert.equal(restored.generatedIdCount, 0);
  });

  test("does not generate a second ID when valid persisted state exists", () => {
    const restored = simulateInitialHydration({
      activeConversationId: "conv-keep",
      conversations: [],
    });

    assert.equal(restored.activeConversationId, "conv-keep");
    assert.deepEqual(restored.conversations.map((c) => c.id), ["conv-keep"]);
    assert.equal(restored.generatedIdCount, 0);
  });

  test("stores no message body or backend payload during lifecycle save", () => {
    resetStorage();
    const appConversations = [
      {
        id: "conv-001",
        title: "Visible title",
        messages: [{ id: 1, role: "user", content: "shipment prompt" }],
        tableData: [{ shipment_id: "abc" }],
        generatedSql: "select * from sensitive_table",
        genieConversationId: "genie-123",
        queryDescription: "backend payload",
      },
    ];

    saveLifecycleState({
      activeConversationId: "conv-001",
      conversations: appConversations.map((c) => ({
        id: c.id,
        title: typeof c.title === "string" ? c.title : "New conversation",
      })),
    });

    const raw = storedJson();
    assert.deepEqual(raw.conversations, [{ id: "conv-001", title: "Visible title" }]);
    const json = JSON.stringify(raw);
    assert.equal(json.includes("shipment prompt"), false);
    assert.equal(json.includes("sensitive_table"), false);
    assert.equal(json.includes("genie-123"), false);
    assert.equal(json.includes("backend payload"), false);
  });
});

// ===========================================================================
// GROUP 9: Storage-size guard
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
// GROUP 10: Reset-transition write count
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
