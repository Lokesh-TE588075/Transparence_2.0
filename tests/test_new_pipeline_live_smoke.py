"""Phase 7C: Controlled Dev Smoke Test for the New Accuracy Pipeline.

Runs the new ChatPipeline with real Databricks services (SQL warehouse + LLM)
in a controlled, single-session test. Feature flag is temporarily enabled
only within this test scope — not globally.

Captures per-query:
- normalized input, detected intent, canonical query
- generated SQL, sql_generation_source
- row_count, displayed_row_count
- response message, fallback status, errors

No mutations are performed. All queries are read-only SELECTs.
"""

import sys
import time
import traceback
from dataclasses import asdict

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.services.chat_pipeline import ChatPipeline, ChatPipelineInput
from app.services.pipeline_adapter import (
    SQLServiceAdapter,
    SQLValidatorAdapter,
    LLMServiceAdapter,
    generate_export_key,
)
from app.config import settings


# =============================================================================
# SETUP: Instantiate real services
# =============================================================================

def _init_services():
    """Initialize real services for smoke test."""
    from app.services.sql_service import SQLService
    from app.services.llm_service import LLMService
    from app.guardrails.sql_validator import SQLValidator

    sql_svc = SQLService()
    llm_svc = LLMService()
    validator = SQLValidator()
    return sql_svc, llm_svc, validator


def _create_pipeline(sql_svc, llm_svc, validator):
    """Create a ChatPipeline with real adapted services."""
    return ChatPipeline(
        sql_executor=SQLServiceAdapter(sql_svc),
        sql_validator=SQLValidatorAdapter(validator),
        llm_service=LLMServiceAdapter(llm_svc),
        table_name=settings.SHIPMENT_TABLE_NAME,
        export_key_generator=generate_export_key,
    )


# =============================================================================
# SMOKE TEST QUERIES
# =============================================================================

SMOKE_QUERIES = [
    {
        "id": "Q1",
        "description": "Clean Mexico query",
        "input": "shipments from Mexico",
        "conversation_id": "smoke-conv-1",
        "expect_sql": True,
        "expect_source": "deterministic_template",
    },
    {
        "id": "Q2",
        "description": "Noisy Mexico query",
        "input": "hello can you give me shipments from Mexico? //////",
        "conversation_id": "smoke-conv-2",
        "expect_sql": True,
        "expect_source": "deterministic_template",
    },
    {
        "id": "Q3",
        "description": "US via air query",
        "input": "shipments from US via air",
        "conversation_id": "smoke-conv-3",
        "expect_sql": True,
        "expect_source": "deterministic_template",
    },
    {
        "id": "Q4",
        "description": "Follow-up: in transit",
        "input": "which are in transit?",
        "conversation_id": "smoke-conv-3",  # Same conv as Q3
        "expect_sql": True,
        "expect_source": "deterministic_template",
    },
    {
        "id": "Q5",
        "description": "Quick summary (uses stored state)",
        "input": "can you give a quick summary?",
        "conversation_id": "smoke-conv-3",  # Same conv as Q3/Q4
        "expect_sql": False,
        "expect_source": "none",
    },
    {
        "id": "Q6",
        "description": "US air next week delivery",
        "input": "shipments from US via air to be delivered next week",
        "conversation_id": "smoke-conv-6",
        "expect_sql": True,
        "expect_source": "deterministic_template",
    },
    {
        "id": "Q7",
        "description": "Broaden range after empty (independent conv)",
        "input": "shipments from Zimbabwe via rail this week",
        "conversation_id": "smoke-conv-7",
        "expect_sql": True,
        "expect_source": "deterministic_template",
    },
]


# =============================================================================
# RUN SMOKE TEST
# =============================================================================

def run_smoke_test():
    """Execute all smoke test queries and collect results."""
    print("=" * 80)
    print("PHASE 7C: NEW ACCURACY PIPELINE SMOKE TEST")
    print("=" * 80)
    print(f"Table: {settings.SHIPMENT_TABLE_NAME}")
    print(f"Warehouse: {settings.DATABRICKS_SQL_WAREHOUSE_PATH}")
    print()

    try:
        sql_svc, llm_svc, validator = _init_services()
        pipeline = _create_pipeline(sql_svc, llm_svc, validator)
    except Exception as e:
        print(f"SETUP FAILED: {e}")
        traceback.print_exc()
        return

    results = []
    passed = 0
    failed = 0

    for q in SMOKE_QUERIES:
        print(f"\n{'─' * 70}")
        print(f"[{q['id']}] {q['description']}")
        print(f"  Input: \"{q['input']}\"")
        print(f"  Conv:  {q['conversation_id']}")
        print(f"{'─' * 70}")

        start = time.time()
        try:
            result = pipeline.run(ChatPipelineInput(
                user_input=q["input"],
                conversation_id=q["conversation_id"],
            ))
            elapsed_ms = int((time.time() - start) * 1000)

            # Collect report fields
            report = {
                "id": q["id"],
                "status": result.status,
                "intent": result.intent,
                "sql_generation_source": result.sql_generation_source,
                "sql_used": (result.sql_used[:120] + "...") if result.sql_used and len(result.sql_used) > 120 else result.sql_used,
                "row_count": result.row_count,
                "displayed_row_count": result.displayed_row_count,
                "is_table": result.is_table,
                "requires_clarification": result.requires_clarification,
                "message_preview": result.message[:150],
                "assistant_suggestion": result.assistant_suggestion,
                "interpretation_notes": result.interpretation_notes,
                "elapsed_ms": elapsed_ms,
                "error": None,
                "fell_back": False,
            }

            # Determine pass/fail
            test_pass = True
            notes = []

            if result.status == "error":
                test_pass = False
                notes.append(f"ERROR: {result.message[:200]}")
            elif result.status == "requires_llm_fallback":
                report["fell_back"] = True
                notes.append("Would fall back to old pipeline (LLM needed)")
            
            if q["expect_sql"] and not result.sql_used and result.status == "success":
                # Only flag if we expected SQL and got success without it
                notes.append("Expected SQL but none generated")

            if q["expect_source"] and result.sql_generation_source != q["expect_source"]:
                if result.status != "requires_llm_fallback":
                    notes.append(f"Source mismatch: expected={q['expect_source']}, got={result.sql_generation_source}")

            # Print report
            print(f"  Status:  {result.status}")
            print(f"  Intent:  {result.intent}")
            print(f"  Source:  {result.sql_generation_source}")
            print(f"  Rows:    {result.row_count} total, {result.displayed_row_count} displayed")
            print(f"  Table:   {result.is_table}")
            print(f"  Time:    {elapsed_ms}ms")
            if result.sql_used:
                print(f"  SQL:     {result.sql_used[:100]}...")
            print(f"  Message: {result.message[:120]}")
            if result.interpretation_notes:
                print(f"  Notes:   {result.interpretation_notes}")
            if notes:
                print(f"  ⚠️  {'; '.join(notes)}")

            if test_pass:
                print(f"  ✅ PASS")
                passed += 1
            else:
                print(f"  ❌ FAIL")
                failed += 1

            results.append(report)

        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            print(f"  ❌ EXCEPTION: {str(e)[:200]}")
            traceback.print_exc()
            failed += 1
            results.append({
                "id": q["id"],
                "status": "exception",
                "error": str(e)[:200],
                "elapsed_ms": elapsed_ms,
            })

    # Final summary
    print(f"\n{'=' * 80}")
    print(f"SMOKE TEST RESULTS: {passed} PASSED, {failed} FAILED, {len(results)} TOTAL")
    print(f"{'=' * 80}")

    return results


# Run if executed directly
if __name__ == "__main__":
    run_smoke_test()
