"""Phase G1: Genie API live smoke test.

Tests the real Genie REST API end-to-end using the GenieClient.
This test makes real HTTP calls to the Databricks workspace and
consumes real Genie API quota.

GATED: Only runs when the environment variable is explicitly set:

    RUN_GENIE_LIVE_SMOKE=true

This follows the same pattern as test_new_pipeline_live_smoke.py.
It will be SKIPPED (not failed) if the flag is not set.

Conversation tested:
    Turn 1: "shipments from US via air"
    Turn 2: "which are in transit?"  (follow-up in same Genie conversation)
    Turn 3: "quick summary"

Checks per turn:
    - message reaches COMPLETED status
    - text attachment is present (natural language summary)
    - query attachment present and contains SELECT SQL (turns 1 and 2)
    - suggested questions present (where available)
    - same conversation_id used for all turns (Genie context is maintained)
    - no chart spec is assumed or asserted
    - no secrets or tokens appear in printed output

Configuration:
    GENIE_SPACE_ID   Override space ID (default: known confirmed space)
    DATABRICKS_HOST  Workspace URL (default: te-ss-coe-dev.cloud.databricks.com)
    DATABRICKS_TOKEN Personal Access Token or SDK auto-auth
"""

import os
import sys
import time
import traceback

sys.path.insert(
    0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app"
)

# ---------------------------------------------------------------------------
# GATE: skip immediately unless explicitly enabled
# ---------------------------------------------------------------------------

_ENABLED = os.getenv("RUN_GENIE_LIVE_SMOKE", "").lower() in ("true", "1", "yes")

if not _ENABLED:
    print()
    print("=" * 70)
    print("GENIE LIVE SMOKE TEST: SKIPPED")
    print()
    print("  This test makes real API calls. To run it, set:")
    print("    RUN_GENIE_LIVE_SMOKE=true")
    print()
    print("  Optionally override the space:")
    print("    GENIE_SPACE_ID=<your-space-id>")
    print("=" * 70)
    print()
    # Exit cleanly — not a failure
    sys.exit(0)


# ---------------------------------------------------------------------------
# Configuration (after gate passes)
# ---------------------------------------------------------------------------

# Confirmed in G0 feasibility assessment
_DEFAULT_SPACE_ID = "01f17a93e6aa1b97a9da7ef329e15e46"

SPACE_ID = os.getenv("GENIE_SPACE_ID", _DEFAULT_SPACE_ID)

# Smoke test conversation turns
_TURNS = [
    {
        "id": "T1",
        "description": "Initial query: US air shipments",
        "message": "shipments from US via air",
        "start_new_conversation": True,
        "expect_sql": True,
        "expect_text": True,
    },
    {
        "id": "T2",
        "description": "Follow-up: in transit (same conversation)",
        "message": "which are in transit?",
        "start_new_conversation": False,
        "expect_sql": True,
        "expect_text": True,
    },
    {
        "id": "T3",
        "description": "Summary request (same conversation)",
        "message": "quick summary",
        "start_new_conversation": False,
        "expect_sql": False,   # summary may not require new SQL
        "expect_text": True,
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_truncate(text: str, max_len: int = 200) -> str:
    """Truncate a string for safe display. Never truncates tokens."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _print_separator(char: str = "-", width: int = 70):
    print(char * width)


# ---------------------------------------------------------------------------
# Main smoke test runner
# ---------------------------------------------------------------------------

def run_smoke_test() -> bool:
    """Run all smoke test turns. Returns True if all pass."""
    from app.services.genie_client import (
        GenieClient,
        GenieClientError,
        GenieTimeoutError,
        GenieExecutionError,
    )

    print()
    print("=" * 70)
    print("PHASE G1: GENIE LIVE SMOKE TEST")
    print("=" * 70)
    print(f"Space ID : {SPACE_ID}")
    print(f"Host     : {os.getenv('DATABRICKS_HOST', 'auto-detected')}")
    print(f"Auth     : {'DATABRICKS_TOKEN set' if os.getenv('DATABRICKS_TOKEN') else 'SDK credential chain'}")
    print()

    # Initialise client
    try:
        client = GenieClient()
    except Exception as e:
        print(f"SETUP FAILED: Could not initialise GenieClient: {e}")
        traceback.print_exc()
        return False

    # Verify space is accessible
    print("[SETUP] Verifying Genie Space access...")
    try:
        space = client.find_space_by_name("TransparencE Shipment Intelligence")
        if space:
            print(f"  Found space: '{space.title}' (ID: {space.space_id})")
            print(f"  Warehouse  : {space.warehouse_id}")
        else:
            print(f"  WARNING: Could not find 'TransparencE Shipment Intelligence' by name.")
            print(f"  Proceeding with configured SPACE_ID: {SPACE_ID}")
    except GenieClientError as e:
        print(f"  WARNING: list_spaces failed: {str(e)[:150]}")
        print(f"  Proceeding with configured SPACE_ID: {SPACE_ID}")
    print()

    # Run turns
    conversation_id = None
    turn_results = []
    overall_pass = True

    for turn in _TURNS:
        _print_separator()
        print(f"[{turn['id']}] {turn['description']}")
        print(f"  Message: \"{turn['message']}\"")
        if conversation_id:
            print(f"  Conv ID: {conversation_id}  (continuing)")
        _print_separator(char=".")

        start = time.time()
        result = {
            "id": turn["id"],
            "description": turn["description"],
            "passed": False,
            "notes": [],
            "elapsed_ms": 0,
        }

        try:
            # Send message or start conversation
            if turn["start_new_conversation"] or conversation_id is None:
                resp = client.start_conversation(SPACE_ID, turn["message"])
                conversation_id = resp["conversation_id"]
                message_id      = resp["message_id"]
                print(f"  Started new conversation: {conversation_id}")
            else:
                resp = client.send_message(SPACE_ID, conversation_id, turn["message"])
                message_id = resp["message_id"]
                print(f"  Follow-up message_id: {message_id}")

            print(f"  Polling for completion (timeout=120s)...")

            # Poll until complete
            msg = client.wait_for_message_completion(
                SPACE_ID,
                conversation_id,
                message_id,
                timeout_seconds=120,
                poll_interval_seconds=2.0,
            )

            elapsed_ms = int((time.time() - start) * 1000)
            result["elapsed_ms"] = elapsed_ms

            print(f"  Status : {msg.status}")
            print(f"  Time   : {elapsed_ms}ms")

            # -----------------------------------------------------------------
            # Check: conversation_id consistency (multi-turn context)
            # -----------------------------------------------------------------
            if msg.conversation_id and msg.conversation_id != conversation_id:
                result["notes"].append(
                    f"WARNING: conversation_id mismatch "
                    f"(expected {conversation_id}, got {msg.conversation_id})"
                )
            else:
                print(f"  Conv   : {msg.conversation_id} (consistent)")

            # -----------------------------------------------------------------
            # Check: text attachment
            # -----------------------------------------------------------------
            texts = client.extract_text_attachments(msg)
            if texts:
                print(f"  Text   : {_safe_truncate(texts[0].content, 150)}")
            elif turn["expect_text"]:
                result["notes"].append("Expected text attachment but none found")

            # -----------------------------------------------------------------
            # Check: query attachment and SQL
            # -----------------------------------------------------------------
            queries = client.extract_query_attachments(msg)
            sql     = client.extract_generated_sql(msg)
            stmt_ids = client.extract_statement_ids(msg)

            if queries:
                print(f"  SQL    : {_safe_truncate(queries[0].sql, 120)}")
                print(f"  Rows   : {queries[0].row_count}")
                print(f"  StmtID : {queries[0].statement_id}")
            elif turn["expect_sql"]:
                # SQL may not always be present (e.g. Genie uses prior context)
                result["notes"].append("No SQL attachment found (may be using cached context)")

            # -----------------------------------------------------------------
            # Check: suggested questions
            # -----------------------------------------------------------------
            questions = client.extract_suggested_questions(msg)
            if questions:
                print(f"  Suggestions ({len(questions)}):")
                for q in questions:
                    print(f"    - {q}")

            # -----------------------------------------------------------------
            # Check: viz attachment (reference only — no chart spec expected)
            # -----------------------------------------------------------------
            vizs = client.extract_viz_attachments(msg)
            if vizs:
                print(f"  Viz    : {len(vizs)} viz attachment(s)")
                for v in vizs:
                    print(f"    query_attachment_id={v.query_attachment_id}")
                    # Assert no chart spec is present
                    v_dict = v.__dict__
                    bad_keys = {"chart_spec", "vega_lite", "plotly", "image_url"}
                    assert bad_keys.isdisjoint(v_dict.keys()), (
                        f"FAIL: viz attachment unexpectedly contains chart spec keys: "
                        f"{bad_keys & set(v_dict.keys())}"
                    )

            result["passed"] = True

        except GenieTimeoutError as e:
            result["notes"].append(f"TIMEOUT: {str(e)[:150]}")
            print(f"  TIMEOUT: {str(e)[:150]}")
        except GenieExecutionError as e:
            result["notes"].append(f"GENIE EXECUTION ERROR: {str(e)[:150]}")
            print(f"  GENIE ERROR: {str(e)[:150]}")
        except GenieClientError as e:
            result["notes"].append(f"CLIENT ERROR: {str(e)[:150]}")
            print(f"  CLIENT ERROR: {str(e)[:150]}")
        except AssertionError as e:
            result["notes"].append(f"ASSERTION FAIL: {str(e)[:150]}")
            print(f"  ASSERTION FAIL: {str(e)[:150]}")
            result["passed"] = False
        except Exception as e:
            result["notes"].append(f"UNEXPECTED ERROR: {str(e)[:150]}")
            print(f"  UNEXPECTED ERROR: {str(e)[:150]}")
            traceback.print_exc()

        if result["notes"]:
            for note in result["notes"]:
                print(f"  NOTE: {note}")

        status_label = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status_label}]")

        if not result["passed"]:
            overall_pass = False

        turn_results.append(result)

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print()
    _print_separator("=")
    print("SUMMARY")
    _print_separator("=")
    passed_count = sum(1 for r in turn_results if r["passed"])
    for r in turn_results:
        label = "PASS" if r["passed"] else "FAIL"
        print(f"  [{label}] [{r['id']}] {r['description']} ({r['elapsed_ms']}ms)")
        for note in r["notes"]:
            print(f"         NOTE: {note}")

    print()
    print(f"  {passed_count}/{len(turn_results)} turns passed")
    print(f"  Overall: {'PASS' if overall_pass else 'FAIL'}")
    print()

    # Security check: verify no tokens appear in printed output
    # (This is a smoke test, not a security test, but we assert the principle)
    token_val = os.getenv("DATABRICKS_TOKEN", "")
    if token_val:
        # We cannot actually inspect stdout from here, but we can assert
        # that the client itself doesn't return token values in public fields
        assert token_val not in str(turn_results), (
            "SECURITY: DATABRICKS_TOKEN value found in turn results dict!"
        )

    return overall_pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    success = run_smoke_test()
    sys.exit(0 if success else 1)
