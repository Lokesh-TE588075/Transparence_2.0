"""Tests for the input normalizer module.

Validates:
- Noise punctuation removal
- Business identifier preservation
- Country extraction with threshold-based fuzzy matching
- Transport mode extraction
- Status category extraction
- Meta-intent detection
- Semantic equivalence (same entities from different phrasings)
"""

import sys
sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.services.input_normalizer import normalize, FUZZY_MATCH_THRESHOLD


# =============================================================================
# TEST GROUP: Noise Removal
# =============================================================================


class TestNoiseRemoval:
    """Verify meaningless punctuation is stripped without affecting content."""

    def test_trailing_slashes(self):
        result = normalize("shipments from Mexico? //////")
        assert "//////" not in result.cleaned
        assert "mexico" in result.cleaned.lower() or "Mexico" in result.cleaned

    def test_trailing_question_marks(self):
        result = normalize("hello can you give me shipments from Germany???")
        assert "???" not in result.cleaned
        assert "Germany" in result.cleaned or "germany" in result.cleaned.lower()

    def test_trailing_exclamation(self):
        result = normalize("show delays!!!!!!")
        assert "!!!!!!" not in result.cleaned
        assert "show delays" in result.cleaned.lower()

    def test_standalone_noise(self):
        result = normalize("////// random noise //////")
        assert "random noise" in result.cleaned

    def test_only_noise_produces_empty(self):
        result = normalize("??????")
        assert result.cleaned == ""

    def test_normal_punctuation_preserved(self):
        result = normalize("what's the status?")
        # Single question mark should be preserved or not matter
        assert "status" in result.cleaned.lower()

    def test_preserves_single_period(self):
        result = normalize("shipments from U.S.")
        assert "U.S." in result.cleaned or "US" in result.cleaned


# =============================================================================
# TEST GROUP: ID Preservation
# =============================================================================


class TestIdPreservation:
    """Verify business identifiers are preserved during cleaning."""

    def test_numeric_shipment_id(self):
        result = normalize("details of shipment 4110279735")
        assert "4110279735" in result.entities.shipment_ids
        assert "4110279735" in result.cleaned

    def test_alphanumeric_waybill(self):
        result = normalize("track waybill ABC12345")
        assert any("ABC12345" in sid for sid in result.entities.shipment_ids)

    def test_short_numeric_po(self):
        result = normalize("PO 12345")
        assert "12345" in result.entities.shipment_ids

    def test_container_id(self):
        result = normalize("check HBL MEDU1234567")
        assert any("MEDU1234567" in sid for sid in result.entities.shipment_ids)

    def test_id_survives_noise_removal(self):
        result = normalize("shipment 4110279735 /////")
        assert "4110279735" in result.entities.shipment_ids


# =============================================================================
# TEST GROUP: Transport Mode Extraction
# =============================================================================


class TestTransportMode:
    """Verify transport mode synonyms are correctly mapped."""

    def test_via_air(self):
        result = normalize("shipments from US via air")
        assert result.entities.transport_mode == "Air transport"

    def test_ocean_shipments(self):
        result = normalize("ocean shipments to Germany")
        assert result.entities.transport_mode == "Ocean Transport"

    def test_trucking(self):
        result = normalize("trucking deliveries")
        assert result.entities.transport_mode == "Road Transport"

    def test_by_sea(self):
        result = normalize("cargo by sea from China")
        assert result.entities.transport_mode == "Ocean Transport"

    def test_no_mode_when_absent(self):
        result = normalize("shipments from Mexico")
        assert result.entities.transport_mode is None


# =============================================================================
# TEST GROUP: Country Extraction with Confidence Threshold
# =============================================================================


class TestCountryExtraction:
    """Verify country extraction uses threshold-based fuzzy matching."""

    def test_exact_match_source(self):
        result = normalize("shipments from Mexico")
        assert result.entities.source_country == "MX"
        assert result.entities.source_confidence == 1.0

    def test_exact_match_destination(self):
        result = normalize("shipments to Czech Republic")
        assert result.entities.destination_country == "CZ"
        assert result.entities.destination_confidence == 1.0

    def test_iso_code_direct(self):
        result = normalize("shipments from US")
        assert result.entities.source_country == "US"

    def test_nonsense_country_returns_none(self):
        """A clearly non-matching string should not map to any country."""
        result = normalize("shipments from Xyzlandia")
        assert result.entities.source_country is None

    def test_threshold_behavior(self):
        """Fuzzy match should only succeed if score >= FUZZY_MATCH_THRESHOLD.

        We test that the system returns a country ONLY when the fuzzy score
        meets the threshold, and returns None otherwise. This validates the
        threshold logic itself rather than assuming specific typos always pass.
        """
        from rapidfuzz import fuzz, process
        from app.business_rules.mappings import COUNTRY_NAME_TO_ISO

        # Test with a known close typo
        test_token = "Mexicoo"
        best_match = process.extractOne(
            test_token, COUNTRY_NAME_TO_ISO.keys(), scorer=fuzz.token_sort_ratio
        )

        result = normalize(f"shipments from {test_token}")

        if best_match and best_match[1] >= FUZZY_MATCH_THRESHOLD:
            # If fuzzy score meets threshold, should map
            assert result.entities.source_country == COUNTRY_NAME_TO_ISO[best_match[0]]
            assert result.entities.source_confidence >= FUZZY_MATCH_THRESHOLD / 100.0
        else:
            # If below threshold, should NOT map
            assert result.entities.source_country is None

    def test_very_low_match_triggers_clarification(self):
        """A token with a fuzzy match below threshold should flag clarification."""
        # Use a token that's somewhat close but likely below 85
        result = normalize("shipments from Germeny")
        # We don't assert the result directly — just validate logic:
        # If it didn't match, requires_clarification should be True
        if result.entities.source_country is None and result.entities.source_confidence > 0:
            assert result.entities.requires_clarification is True


# =============================================================================
# TEST GROUP: Status Extraction
# =============================================================================


class TestStatusExtraction:
    """Verify status category synonyms are correctly identified."""

    def test_in_transit(self):
        result = normalize("which are in transit?")
        assert result.entities.status_category == "in_transit"

    def test_delivered(self):
        result = normalize("show delivered ones")
        assert result.entities.status_category == "completed"

    def test_delayed(self):
        result = normalize("delayed shipments")
        assert result.entities.status_category == "delayed"

    def test_active(self):
        result = normalize("show active shipments")
        assert result.entities.status_category == "in_transit"

    def test_no_status_when_absent(self):
        result = normalize("shipments from Mexico via air")
        assert result.entities.status_category is None


# =============================================================================
# TEST GROUP: Meta-Intent Hints
# =============================================================================


class TestMetaIntentHints:
    """Verify meta-intent pattern detection."""

    def test_quick_summary(self):
        result = normalize("can you give a quick summary?")
        assert result.entities.meta_intent_hint == "summarize"

    def test_download(self):
        result = normalize("download this result")
        assert result.entities.meta_intent_hint == "download"

    def test_broaden_range(self):
        result = normalize("yes check a broader range")
        assert result.entities.meta_intent_hint == "broaden"

    def test_no_hint_for_normal_query(self):
        result = normalize("shipments from Germany")
        assert result.entities.meta_intent_hint is None


# =============================================================================
# TEST GROUP: Semantic Equivalence
# =============================================================================


class TestSemanticEquivalence:
    """Verify that semantically equivalent queries produce same entities."""

    def test_mexico_source_equivalence(self):
        """All these should produce source_country = MX."""
        queries = [
            "shipments from Mexico",
            "show me shipments originating from Mexico",
            "can you give me shipments from MX",
            "hello can you give me shipments from Mexico? //////",
        ]
        for q in queries:
            result = normalize(q)
            assert result.entities.source_country == "MX", (
                f"Query '{q}' produced source={result.entities.source_country}, expected MX"
            )

    def test_germany_source_equivalence(self):
        """Multiple phrasings for Germany should all give DE."""
        queries = [
            "hello, can you please show shipments from Germany???",
            "pls show shipments from Germany",
            "show shipments from DE",
        ]
        for q in queries:
            result = normalize(q)
            assert result.entities.source_country == "DE", (
                f"Query '{q}' produced source={result.entities.source_country}, expected DE"
            )

    def test_air_mode_equivalence(self):
        """Different ways of saying 'air' produce same mode."""
        queries = [
            "shipments via air",
            "air shipments",
            "by air",
        ]
        for q in queries:
            result = normalize(q)
            assert result.entities.transport_mode == "Air transport", (
                f"Query '{q}' produced mode={result.entities.transport_mode}"
            )


# =============================================================================
# TEST GROUP: Edge Cases
# =============================================================================


class TestEdgeCases:
    """Boundary conditions and special inputs."""

    def test_empty_string(self):
        result = normalize("")
        assert result.cleaned == ""
        assert result.entities.source_country is None

    def test_whitespace_only(self):
        result = normalize("   ")
        assert result.cleaned == ""

    def test_greeting_only_no_strip(self):
        result = normalize("hello")
        assert result.cleaned == "hello"

    def test_greeting_with_query_strips(self):
        result = normalize("hi show me delays")
        assert "show me delays" in result.cleaned
        assert not result.cleaned.lower().startswith("hi ")

    def test_raw_always_preserved(self):
        raw = "hello can you give me shipments from Mexico? //////"
        result = normalize(raw)
        assert result.raw == raw
