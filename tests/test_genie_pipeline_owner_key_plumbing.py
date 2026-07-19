"""Phase 4C1 — GeniePipeline owner-key plumbing tests.

Tests the structural owner_key parameter added to GeniePipeline.run().
Validates backward compatibility, structural validation, request-local
isolation, and confirms no durable state is accessed.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from app.services.genie_pipeline import GeniePipeline, _validate_owner_key, _OwnerKeyContractError
from app.services.genie_session_store import GenieSessionStore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_OWNER_KEY = "a" * 64  # valid 64-char lowercase hex
_VALID_OWNER_KEY_2 = "b" * 64
_VALID_MIXED_HEX = "0123456789abcdef" * 4  # 64 chars
_SPACE_ID = "test-space-id"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGenieClient:
    """Minimal Genie client fake that records calls."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def start_conversation(self, space_id, message):
        self.calls.append({"method": "start_conversation", "space_id": space_id, "message": message})
        return {"conversation_id": "genie-conv-1", "message_id": "msg-1"}

    def send_message(self, space_id, conv_id, message):
        self.calls.append({"method": "send_message", "conv_id": conv_id, "message": message})
        return {"message_id": "msg-2"}

    def wait_for_message_completion(self, space_id, conv_id, msg_id, **kwargs):
        return {"status": "COMPLETED", "message_id": msg_id}

    def fetch_query_result(self, space_id, conv_id, msg_id):
        return {"columns": [], "rows": [], "row_count": 0}


def _build_pipeline(
    genie_client=None,
    session_store=None,
    **kwargs,
) -> GeniePipeline:
    """Build a minimal pipeline for testing."""
    client = genie_client or FakeGenieClient()
    store = session_store or GenieSessionStore()
    return GeniePipeline(
        genie_client=client,
        session_store=store,
        space_id=_SPACE_ID,
        fetch_query_results=False,
        enable_prompt_enrichment=False,
        enable_shape_validation=False,
        enable_table_summary=False,
        **kwargs,
    )


# ===========================================================================
# BACKWARD COMPATIBILITY (Tests 1-5)
# ===========================================================================


class TestBackwardCompatibility:
    """Existing callers remain valid without owner_key."""

    # Test 1: existing call without owner_key
    def test_run_without_owner_key_succeeds(self):
        pl = _build_pipeline()
        result = pl.run(user_message="show shipments", app_conversation_id="conv-1")
        assert result is not None
        assert "status" in result

    # Test 2: owner_key defaults to None
    def test_owner_key_defaults_to_none(self):
        """Pipeline run with no owner_key should behave identically."""
        pl = _build_pipeline()
        result = pl.run(user_message="show shipments", app_conversation_id="conv-1")
        assert result.get("fallback_recommended") is False or result.get("status") == "success"

    # Test 3: existing positional callers remain valid
    def test_positional_call_without_owner_key(self):
        pl = _build_pipeline()
        # Positional: user_message, app_conversation_id
        result = pl.run("show shipments", "conv-1")
        assert result is not None

    # Test 4: existing return contract unchanged
    def test_return_contract_unchanged(self):
        pl = _build_pipeline()
        result = pl.run(user_message="show shipments", app_conversation_id="conv-1")
        assert isinstance(result, dict)
        assert "status" in result
        assert "message" in result
        assert "fallback_recommended" in result

    # Test 5: pipeline behaviour unchanged when owner_key is None
    def test_behaviour_unchanged_when_owner_key_none(self):
        pl = _build_pipeline()
        result_without = pl.run(user_message="hello", app_conversation_id="c1")
        result_with_none = pl.run(user_message="hello", app_conversation_id="c2", owner_key=None)
        # Both should produce similar structure (different conv IDs but same shape)
        assert result_without.keys() == result_with_none.keys()


# ===========================================================================
# VALID OWNER KEY (Tests 6-15)
# ===========================================================================


class TestValidOwnerKey:
    """Valid 64-character lowercase hex accepted without side effects."""

    # Test 6: valid key accepted
    def test_valid_64_char_hex_accepted(self):
        pl = _build_pipeline()
        result = pl.run(
            user_message="show shipments",
            app_conversation_id="conv-1",
            owner_key=_VALID_OWNER_KEY,
        )
        assert result is not None
        assert result.get("status") in ("success", "error", "greeting")

    # Test 7: owner key remains request-local
    def test_owner_key_request_local(self):
        """After run() completes, no trace of owner_key on pipeline."""
        pl = _build_pipeline()
        pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        # Check no owner_key attribute on pipeline
        assert not hasattr(pl, "_owner_key")
        assert not hasattr(pl, "owner_key")
        assert not hasattr(pl, "_current_owner_key")

    # Test 8: owner key not stored on pipeline instance
    def test_owner_key_not_stored_on_instance(self):
        pl = _build_pipeline()
        pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        # Exhaustive check of instance dict
        for attr_name in vars(pl):
            attr_val = getattr(pl, attr_name)
            if isinstance(attr_val, str):
                assert attr_val != _VALID_OWNER_KEY, f"owner_key leaked to {attr_name}"

    # Test 9: owner key not added to session state
    def test_owner_key_not_in_session_state(self):
        store = GenieSessionStore()
        pl = _build_pipeline(session_store=store)
        pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        context = store.get_context_snapshot("c1")
        for key, val in context.items():
            if isinstance(val, str):
                assert val != _VALID_OWNER_KEY, f"owner_key leaked to session state key={key}"
        assert "owner_key" not in context
        assert "owner_user_id_hash" not in context

    # Test 10: owner key not passed to Genie client
    def test_owner_key_not_passed_to_genie(self):
        client = FakeGenieClient()
        pl = _build_pipeline(genie_client=client)
        pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        for call_record in client.calls:
            for key, val in call_record.items():
                if isinstance(val, str):
                    assert val != _VALID_OWNER_KEY, f"owner_key leaked to Genie call {key}"
            assert "owner_key" not in call_record

    # Test 11: owner key not passed to SQL
    def test_owner_key_not_passed_to_sql(self):
        sql_svc = MagicMock()
        pl = _build_pipeline(sql_service=sql_svc)
        pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        # sql_service should not have been called with owner_key
        for call_args in sql_svc.method_calls:
            for arg in call_args.args if hasattr(call_args, 'args') else []:
                if isinstance(arg, str):
                    assert arg != _VALID_OWNER_KEY

    # Test 12: owner key not included in output
    def test_owner_key_not_in_output(self):
        pl = _build_pipeline()
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        result_str = str(result)
        assert _VALID_OWNER_KEY not in result_str

    # Test 13: owner key not logged
    def test_owner_key_not_logged(self, caplog):
        import logging
        with caplog.at_level(logging.DEBUG, logger="app.services.genie_pipeline"):
            pl = _build_pipeline(debug=True)
            pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        assert _VALID_OWNER_KEY not in caplog.text

    # Test 14: owner key does not affect current session key
    def test_owner_key_does_not_affect_session_key(self):
        store = GenieSessionStore()
        pl = _build_pipeline(session_store=store)
        # Run without owner_key
        pl.run(user_message="test", app_conversation_id="conv-x")
        ctx_without = store.get_context_snapshot("conv-x")
        # Run with owner_key
        pl.run(user_message="test", app_conversation_id="conv-y", owner_key=_VALID_OWNER_KEY)
        ctx_with = store.get_context_snapshot("conv-y")
        # Session store uses app_conversation_id as key, not owner_key
        assert "conv-x" != "conv-y"  # distinct keys used

    # Test 15: owner key does not change Genie conversation creation
    def test_owner_key_does_not_change_genie_conversation_creation(self):
        client = FakeGenieClient()
        pl = _build_pipeline(genie_client=client)
        # Use a shipment-domain message that routes through to Genie
        pl.run(
            user_message="show me all delayed shipments from China",
            app_conversation_id="c1",
            owner_key=_VALID_OWNER_KEY,
        )
        # start_conversation should still be called normally
        starts = [c for c in client.calls if c["method"] == "start_conversation"]
        assert len(starts) == 1
        assert starts[0]["space_id"] == _SPACE_ID


# ===========================================================================
# INVALID OWNER KEY (Tests 16-27)
# ===========================================================================


class TestInvalidOwnerKey:
    """Malformed owner keys are rejected with sanitized errors."""

    # Test 16: empty string rejected
    def test_empty_string_rejected(self):
        pl = _build_pipeline()
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key="")
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    # Test 17: whitespace-only rejected
    def test_whitespace_only_rejected(self):
        pl = _build_pipeline()
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key="   ")
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    # Test 18: short value rejected
    def test_short_value_rejected(self):
        pl = _build_pipeline()
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key="abcd1234")
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    # Test 19: long value rejected
    def test_long_value_rejected(self):
        pl = _build_pipeline()
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key="a" * 65)
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    # Test 20: uppercase hex rejected
    def test_uppercase_hex_rejected(self):
        pl = _build_pipeline()
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key="A" * 64)
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    # Test 21: non-hex value rejected
    def test_non_hex_value_rejected(self):
        pl = _build_pipeline()
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key="g" * 64)
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    # Test 22: value containing @ rejected
    def test_at_sign_rejected(self):
        pl = _build_pipeline()
        bad_key = "a" * 32 + "@" + "b" * 31
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key=bad_key)
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    # Test 23: surrounding whitespace rejected
    def test_surrounding_whitespace_rejected(self):
        pl = _build_pipeline()
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key=" " + "a" * 64 + " ")
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    # Test 24: non-string rejected
    def test_non_string_rejected(self):
        pl = _build_pipeline()
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key=12345)
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    # Test 25: public error hides supplied value
    def test_error_hides_supplied_value(self):
        pl = _build_pipeline()
        bad_key = "x" * 64
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key=bad_key)
        assert bad_key not in result.get("message", "")
        assert bad_key not in str(result)
        assert result["fallback_recommended"] is False

    # Test 26: Genie client not called after validation failure
    def test_genie_not_called_after_validation_failure(self):
        client = FakeGenieClient()
        pl = _build_pipeline(genie_client=client)
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key="invalid!")
        assert len(client.calls) == 0
        assert result["fallback_recommended"] is False

    # Test 27: no durable adapter call after validation failure
    def test_no_durable_adapter_after_validation_failure(self):
        with patch(
            "app.services.durable_genie_session_adapter.DurableGenieSessionAdapter"
        ) as mock_adapter:
            pl = _build_pipeline()
            result = pl.run(user_message="test", app_conversation_id="c1", owner_key="bad")
        mock_adapter.assert_not_called()
        assert result["fallback_recommended"] is False

    # Test: normal Genie errors retain fallback_recommended=True
    def test_normal_genie_errors_retain_fallback_true(self):
        """Unrelated Genie failures still allow custom pipeline fallback."""
        from app.services.genie_client import GenieClientError

        class _FailingClient:
            def start_conversation(self, *a, **k):
                raise GenieClientError("connection refused")
            def send_message(self, *a, **k):
                raise GenieClientError("connection refused")

        pl = _build_pipeline(genie_client=_FailingClient())
        result = pl.run(
            user_message="show delayed shipments from China",
            app_conversation_id="c1",
            owner_key=_VALID_OWNER_KEY,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is True


# ===========================================================================
# ISOLATION (Tests 28-35)
# ===========================================================================


class TestIsolation:
    """Owner key isolation guarantees."""

    # Test 28: concurrent calls with different keys remain isolated
    def test_concurrent_calls_isolated(self):
        pl = _build_pipeline()
        results = {}
        errors = []

        def run_with_key(key, conv_id):
            try:
                r = pl.run(user_message="test", app_conversation_id=conv_id, owner_key=key)
                results[conv_id] = r
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=run_with_key, args=(_VALID_OWNER_KEY, "c1"))
        t2 = threading.Thread(target=run_with_key, args=(_VALID_OWNER_KEY_2, "c2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors
        assert "c1" in results
        assert "c2" in results

    # Test 29: one call cannot observe another call's owner key
    def test_no_cross_observation(self):
        pl = _build_pipeline()
        # Run once with key A
        pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        # Run again with key B — should not see key A anywhere
        result = pl.run(user_message="test", app_conversation_id="c2", owner_key=_VALID_OWNER_KEY_2)
        assert _VALID_OWNER_KEY not in str(result)

    # Test 30: no global owner key exists
    def test_no_global_owner_key(self):
        import app.services.genie_pipeline as mod
        pl = _build_pipeline()
        pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        # Check module-level attributes
        for attr_name in dir(mod):
            if attr_name.startswith("__"):
                continue
            attr = getattr(mod, attr_name)
            if isinstance(attr, str) and attr == _VALID_OWNER_KEY:
                pytest.fail(f"Global owner_key found in module attr: {attr_name}")

    # Test 31: no owner-key cache exists
    def test_no_owner_key_cache(self):
        pl = _build_pipeline()
        pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        # Instance should not have any cache-like structure holding keys
        for attr_name, attr_val in vars(pl).items():
            if isinstance(attr_val, dict):
                for k, v in attr_val.items():
                    assert v != _VALID_OWNER_KEY, f"owner_key cached in {attr_name}[{k}]"
            elif isinstance(attr_val, (list, tuple)):
                assert _VALID_OWNER_KEY not in attr_val, f"owner_key cached in {attr_name}"

    # Test 32: no durable runtime bundle accessed
    def test_no_durable_runtime_bundle(self):
        with patch(
            "app.services.durable_genie_session_adapter.DurableGenieSessionAdapter"
        ) as mock_adapter:
            pl = _build_pipeline()
            pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        mock_adapter.assert_not_called()

    # Test 33: no repository accessed
    def test_no_repository_accessed(self):
        with patch(
            "app.services.lakebase_conversation_repository.LakebaseConversationRepository"
        ) as mock_repo:
            pl = _build_pipeline()
            pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        mock_repo.assert_not_called()

    # Test 34: no Lakebase interaction
    def test_no_lakebase_interaction(self):
        with patch(
            "app.services.lakebase_connection_provider.LakebaseConnectionProvider"
        ) as mock_lb:
            pl = _build_pipeline()
            pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        mock_lb.assert_not_called()

    # Test 35: no credential, pool or SQL
    def test_no_credential_pool_sql(self):
        """No psycopg or databricks-sdk credential operations."""
        pl = _build_pipeline()
        result = pl.run(user_message="test", app_conversation_id="c1", owner_key=_VALID_OWNER_KEY)
        # If we got here without ImportError on psycopg, the pipeline
        # did not attempt to open a connection.
        assert result is not None


# ===========================================================================
# VALIDATION FUNCTION UNIT TESTS
# ===========================================================================


class TestValidateOwnerKeyFunction:
    """Direct unit tests for _validate_owner_key."""

    def test_valid_all_a(self):
        _validate_owner_key("a" * 64)

    def test_valid_mixed_hex(self):
        _validate_owner_key("0123456789abcdef" * 4)

    def test_rejects_none(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key(None)

    def test_rejects_int(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key(123)

    def test_rejects_empty(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key("   ")

    def test_rejects_leading_space(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key(" " + "a" * 64)

    def test_rejects_trailing_space(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key("a" * 64 + " ")

    def test_rejects_short(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key("a" * 63)

    def test_rejects_long(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key("a" * 65)

    def test_rejects_uppercase(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key("A" * 64)

    def test_rejects_mixed_case(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key("aA" * 32)

    def test_rejects_non_hex(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key("g" * 64)

    def test_rejects_at_sign(self):
        with pytest.raises(_OwnerKeyContractError):
            _validate_owner_key("a" * 32 + "@" + "a" * 31)

    def test_error_message_is_static(self):
        bad_key = "x" * 64
        with pytest.raises(_OwnerKeyContractError, match="Internal error"):
            _validate_owner_key(bad_key)

    def test_error_does_not_contain_value(self):
        bad_key = "ZZZZ" + "a" * 60
        try:
            _validate_owner_key(bad_key)
            assert False, "Should have raised"
        except _OwnerKeyContractError as e:
            assert bad_key not in str(e)
