"""Additional unit tests for GenieSessionStore business-context fields (Phase E2)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.genie_session_store import GenieSessionStore


APP_CONV = "app-conv-e2-0001"
GENIE_CONV = "genie-conv-e2-0001"


def _store() -> GenieSessionStore:
    return GenieSessionStore(ttl_hours=24)


class TestContextSnapshot:
    def test_update_context_persists_entities_filters_and_download_key(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV, GENIE_CONV)
        store.update_context(
            APP_CONV,
            last_entities=["NB15524001", "605705-31"],
            last_entity_type="part_number",
            last_filters={"destination": "US", "transport_mode": "AIR"},
            last_intent="EXPLICIT_ENTITY_SEARCH",
            last_user_prompt="shipments for part numbers going to US",
            last_enriched_prompt="Find shipments where part_number is one of [...]",
            last_download_key="dl-123",
            last_table_headers=["shipment_number_id", "part_number", "destination"],
            last_row_count=500,
            last_total_row_count=5000,
            last_returned_row_count=500,
        )

        snapshot = store.get_context_snapshot(APP_CONV)
        assert snapshot["genie_conversation_id"] == GENIE_CONV
        assert snapshot["last_entities"] == ["NB15524001", "605705-31"]
        assert snapshot["last_entity_type"] == "part_number"
        assert snapshot["last_filters"] == {"destination": "US", "transport_mode": "AIR"}
        assert snapshot["last_download_key"] == "dl-123"
        assert snapshot["last_total_row_count"] == 5000
        assert snapshot["last_returned_row_count"] == 500

    def test_update_context_can_create_session_without_genie_conversation(self):
        store = _store()
        store.update_context(
            APP_CONV,
            last_entities=["42570415"],
            last_entity_type="shipment_id",
            last_intent="DIRECT_LOOKUP",
        )
        snapshot = store.get_context_snapshot(APP_CONV)
        assert snapshot["genie_conversation_id"] is None
        assert snapshot["last_entities"] == ["42570415"]
        assert snapshot["last_intent"] == "DIRECT_LOOKUP"

    def test_update_context_is_partial_and_does_not_clear_existing_fields(self):
        store = _store()
        store.update_context(
            APP_CONV,
            last_entities=["NB15524001"],
            last_entity_type="part_number",
            last_filters={"destination": "US"},
            last_download_key="dl-1",
        )
        store.update_context(APP_CONV, last_intent="CORRECTION")

        snapshot = store.get_context_snapshot(APP_CONV)
        assert snapshot["last_entities"] == ["NB15524001"]
        assert snapshot["last_filters"] == {"destination": "US"}
        assert snapshot["last_download_key"] == "dl-1"
        assert snapshot["last_intent"] == "CORRECTION"


class TestResetBehavior:
    def test_reset_genie_mapping_clears_ids_but_preserves_business_context(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV, GENIE_CONV)
        store.set_last_message_id(APP_CONV, "msg-1")
        store.update_context(
            APP_CONV,
            last_entities=["NB15524001"],
            last_entity_type="part_number",
            last_filters={"destination": "US"},
            last_download_key="dl-123",
        )

        store.reset_genie_mapping(APP_CONV)

        assert store.get_genie_conversation_id(APP_CONV) is None
        assert store.get_last_message_id(APP_CONV) is None
        snapshot = store.get_context_snapshot(APP_CONV)
        assert snapshot["last_entities"] == ["NB15524001"]
        assert snapshot["last_download_key"] == "dl-123"
        assert snapshot["last_filters"] == {"destination": "US"}


class TestConvenienceGetters:
    def test_get_last_download_key_returns_value(self):
        store = _store()
        store.update_context(APP_CONV, last_download_key="dl-999")
        assert store.get_last_download_key(APP_CONV) == "dl-999"

    def test_get_context_snapshot_for_missing_session_returns_empty_dict(self):
        store = _store()
        assert store.get_context_snapshot("missing") == {}


class TestSerialization:
    def test_serialize_deserialize_preserves_new_context_fields(self):
        store = _store()
        store.update_context(
            APP_CONV,
            last_entities=["NB15524001", "NB19684001"],
            last_entity_type="part_number",
            last_filters={"destination": "US"},
            last_intent="EXPLICIT_ENTITY_SEARCH",
            last_user_prompt="original prompt",
            last_enriched_prompt="enriched prompt",
            last_download_key="dl-abc",
            last_table_headers=["shipment_number_id", "part_number"],
            last_row_count=100,
            last_total_row_count=5000,
            last_returned_row_count=500,
        )
        session = store.get_session(APP_CONV)

        data = store.serialize_session(session)
        restored = store.deserialize_session(data)

        assert restored.last_entities == ["NB15524001", "NB19684001"]
        assert restored.last_entity_type == "part_number"
        assert restored.last_filters == {"destination": "US"}
        assert restored.last_intent == "EXPLICIT_ENTITY_SEARCH"
        assert restored.last_download_key == "dl-abc"
        assert restored.last_total_row_count == 5000
        assert restored.last_returned_row_count == 500
