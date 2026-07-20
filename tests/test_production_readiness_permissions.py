"""Phase 4D2 — Production readiness permission snapshot tests.

Tests the permission-readiness check using controlled, non-live snapshots.
No live API calls, no database connections, no secret reads.

Covers:
  - Full sufficient snapshot produces READY
  - Missing Genie permission produces BLOCKED
  - Missing Lakebase permission produces BLOCKED
  - Missing secret resource produces BLOCKED
  - Missing shipment-table permission produces BLOCKED
  - Missing optional permission does not block
  - CANNOT_VERIFY is reported but does not silently pass
  - No secret values appear in permission reports
  - PermissionReadinessReport structure and immutability
  - All known permission keys are represented
  - Blocking vs. non-blocking classification
"""
from __future__ import annotations

import pytest

from app.services.production_readiness import (
    check_permission_snapshot,
    PermissionReadinessReport,
    PermissionStatus,
    PermissionEntry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_SUFFICIENT: dict = {
    "genie_space_can_run": "PRESENT_AND_SUFFICIENT",
    "sql_warehouse_can_use": "PRESENT_AND_SUFFICIENT",
    "shipment_table_select": "PRESENT_AND_SUFFICIENT",
    "lakebase_can_connect": "PRESENT_AND_SUFFICIENT",
    "lakebase_app_conversation_dml": "PRESENT_AND_SUFFICIENT",
    "secret_scope_hmac_read": "PRESENT_AND_SUFFICIENT",
    "genie_space_end_user_access": "NOT_REQUIRED",
}

_ALL_KEYS = list(_ALL_SUFFICIENT.keys())


def _snapshot_with(**overrides) -> dict:
    s = dict(_ALL_SUFFICIENT)
    s.update(overrides)
    return s


# ===========================================================================
# GROUP 1: All-sufficient snapshot (Tests 1–4)
# ===========================================================================


class TestAllSufficientSnapshot:
    """A fully sufficient snapshot must produce overall_ready=True."""

    def test_1_all_sufficient_is_ready(self):
        report = check_permission_snapshot(_ALL_SUFFICIENT)
        assert report.overall_ready is True

    def test_2_no_blocking_entries_when_all_sufficient(self):
        report = check_permission_snapshot(_ALL_SUFFICIENT)
        assert len(report.blocking_entries) == 0

    def test_3_report_contains_all_known_permission_keys(self):
        report = check_permission_snapshot(_ALL_SUFFICIENT)
        resource_labels = {e.resource for e in report.entries}
        # Must have entries for all known resources
        assert len(report.entries) >= len(_ALL_KEYS)

    def test_4_unverifiable_list_empty_when_all_sufficient(self):
        report = check_permission_snapshot(_ALL_SUFFICIENT)
        assert len(report.unverifiable_entries) == 0


# ===========================================================================
# GROUP 2: Missing blocking permissions (Tests 5–10)
# ===========================================================================


class TestMissingBlockingPermissions:
    """Missing required permissions must produce BLOCKED overall and appear in blocking_entries."""

    def test_5_missing_genie_space_permission_blocks(self):
        s = _snapshot_with(genie_space_can_run="MISSING")
        report = check_permission_snapshot(s)
        assert not report.overall_ready
        genie_blocking = [e for e in report.blocking_entries if "Genie Space" in e.resource]
        assert len(genie_blocking) > 0

    def test_6_missing_warehouse_permission_blocks(self):
        s = _snapshot_with(sql_warehouse_can_use="MISSING")
        report = check_permission_snapshot(s)
        assert not report.overall_ready
        blocking_ids = [e.resource_id for e in report.blocking_entries]
        assert any("8e46614f7064d8fd" in bid for bid in blocking_ids)

    def test_7_missing_shipment_table_permission_blocks(self):
        s = _snapshot_with(shipment_table_select="MISSING")
        report = check_permission_snapshot(s)
        assert not report.overall_ready
        blocking_ids = [e.resource_id for e in report.blocking_entries]
        assert any("lbn_with_scorecard" in bid for bid in blocking_ids)

    def test_8_missing_lakebase_connect_permission_blocks(self):
        s = _snapshot_with(lakebase_can_connect="MISSING")
        report = check_permission_snapshot(s)
        assert not report.overall_ready

    def test_9_missing_lakebase_dml_permission_blocks(self):
        s = _snapshot_with(lakebase_app_conversation_dml="MISSING")
        report = check_permission_snapshot(s)
        assert not report.overall_ready

    def test_10_missing_hmac_secret_permission_blocks(self):
        s = _snapshot_with(secret_scope_hmac_read="MISSING")
        report = check_permission_snapshot(s)
        assert not report.overall_ready


# ===========================================================================
# GROUP 3: Insufficient permissions (Tests 11–13)
# ===========================================================================


class TestInsufficientPermissions:
    """PRESENT_BUT_INSUFFICIENT must also block required permissions."""

    def test_11_insufficient_genie_permission_blocks(self):
        s = _snapshot_with(genie_space_can_run="PRESENT_BUT_INSUFFICIENT")
        report = check_permission_snapshot(s)
        assert not report.overall_ready

    def test_12_insufficient_warehouse_permission_blocks(self):
        s = _snapshot_with(sql_warehouse_can_use="PRESENT_BUT_INSUFFICIENT")
        report = check_permission_snapshot(s)
        assert not report.overall_ready

    def test_13_insufficient_lakebase_connect_blocks(self):
        s = _snapshot_with(lakebase_can_connect="PRESENT_BUT_INSUFFICIENT")
        report = check_permission_snapshot(s)
        assert not report.overall_ready


# ===========================================================================
# GROUP 4: Optional permissions (Tests 14–16)
# ===========================================================================


class TestOptionalPermissions:
    """Optional / NOT_REQUIRED permissions must not block readiness."""

    def test_14_end_user_genie_access_not_required_does_not_block(self):
        s = _snapshot_with(genie_space_end_user_access="NOT_REQUIRED")
        report = check_permission_snapshot(s)
        # End-user direct access is not required; should not block
        end_user_blocking = [
            e for e in report.blocking_entries
            if "end-user" in e.resource.lower() or "end_user" in e.resource.lower()
        ]
        assert len(end_user_blocking) == 0

    def test_15_missing_optional_permission_does_not_block_overall(self):
        s = _snapshot_with(genie_space_end_user_access="MISSING")
        # end_user is non-blocking; all others sufficient
        report = check_permission_snapshot(s)
        assert report.overall_ready

    def test_16_not_required_status_never_appears_in_blocking_entries(self):
        s = _snapshot_with(genie_space_end_user_access="NOT_REQUIRED")
        report = check_permission_snapshot(s)
        not_required_blocking = [
            e for e in report.blocking_entries
            if e.status == PermissionStatus.NOT_REQUIRED
        ]
        assert len(not_required_blocking) == 0


# ===========================================================================
# GROUP 5: CANNOT_VERIFY (Tests 17–20)
# ===========================================================================


class TestCannotVerifyPermissions:
    """CANNOT_VERIFY must be reported explicitly and should block required permissions."""

    def test_17_cannot_verify_blocking_permission_blocks_overall(self):
        s = _snapshot_with(genie_space_can_run="CANNOT_VERIFY")
        report = check_permission_snapshot(s)
        assert not report.overall_ready

    def test_18_cannot_verify_entries_appear_in_unverifiable_list(self):
        s = _snapshot_with(
            genie_space_can_run="CANNOT_VERIFY",
            sql_warehouse_can_use="CANNOT_VERIFY",
        )
        report = check_permission_snapshot(s)
        assert len(report.unverifiable_entries) >= 2

    def test_19_unknown_snapshot_key_defaults_to_cannot_verify(self):
        """Keys not present in snapshot default to CANNOT_VERIFY."""
        # Provide only one key; all others default
        report = check_permission_snapshot({"genie_space_can_run": "PRESENT_AND_SUFFICIENT"})
        # Other required keys were omitted and should be CANNOT_VERIFY
        unverified_required = [
            e for e in report.entries
            if e.status == PermissionStatus.CANNOT_VERIFY and e.blocking
        ]
        assert len(unverified_required) > 0

    def test_20_empty_snapshot_produces_blocked_report(self):
        """Empty snapshot — all permissions CANNOT_VERIFY — must block."""
        report = check_permission_snapshot({})
        assert not report.overall_ready
        assert len(report.unverifiable_entries) >= len(_ALL_KEYS) - 1  # non-blocking excluded


# ===========================================================================
# GROUP 6: Report structure and safety (Tests 21–27)
# ===========================================================================


class TestPermissionReportStructure:
    """PermissionReadinessReport must have correct structure and never leak secrets."""

    def test_21_report_entries_are_permission_entry_instances(self):
        report = check_permission_snapshot(_ALL_SUFFICIENT)
        for e in report.entries:
            assert isinstance(e, PermissionEntry)

    def test_22_blocking_entries_are_subset_of_entries(self):
        s = _snapshot_with(genie_space_can_run="MISSING")
        report = check_permission_snapshot(s)
        entry_set = set(id(e) for e in report.entries)
        for b in report.blocking_entries:
            assert id(b) in entry_set

    def test_23_permission_entry_resource_id_is_non_sensitive(self):
        """Resource IDs in entries must not look like passwords or tokens."""
        report = check_permission_snapshot(_ALL_SUFFICIENT)
        for e in report.entries:
            # Resource IDs should not contain obvious secret patterns
            assert "password" not in e.resource_id.lower()
            assert "token" not in e.resource_id.lower()

    def test_24_unverifiable_entries_are_subset_of_entries(self):
        s = _snapshot_with(genie_space_can_run="CANNOT_VERIFY")
        report = check_permission_snapshot(s)
        entry_set = set(id(e) for e in report.entries)
        for u in report.unverifiable_entries:
            assert id(u) in entry_set

    def test_25_overall_ready_false_when_any_blocking_missing(self):
        for key in [k for k in _ALL_KEYS if _ALL_SUFFICIENT.get(k) == "PRESENT_AND_SUFFICIENT"]:
            s = _snapshot_with(**{key: "MISSING"})
            report = check_permission_snapshot(s)
            # If the key is blocking, overall should be False
            matching = [e for e in report.entries if e.status == PermissionStatus.MISSING]
            if any(e.blocking for e in matching):
                assert not report.overall_ready, (
                    f"Expected not overall_ready when {key} is MISSING"
                )

    def test_26_permission_entry_is_frozen(self):
        report = check_permission_snapshot(_ALL_SUFFICIENT)
        entry = report.entries[0]
        with pytest.raises((AttributeError, TypeError)):
            entry.status = PermissionStatus.MISSING  # type: ignore

    def test_27_invalid_status_string_defaults_gracefully(self):
        """An unrecognised status string must not crash and must default to CANNOT_VERIFY."""
        s = _snapshot_with(genie_space_can_run="TOTALLY_UNKNOWN_STATUS")
        report = check_permission_snapshot(s)
        genie_entries = [e for e in report.entries if "Genie Space" in e.resource and e.blocking]
        assert len(genie_entries) > 0
        # Should not be PRESENT_AND_SUFFICIENT
        for ge in genie_entries:
            assert ge.status != PermissionStatus.PRESENT_AND_SUFFICIENT
