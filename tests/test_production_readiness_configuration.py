"""Phase 4D2 — Production readiness configuration and feature-flag tests.

Tests the production_readiness service against:
  - Required environment variable presence and format
  - Feature-flag parsing determinism (case, whitespace, unknown values)
  - Feature-flag dependency invariants
  - Unsafe-flag-combination rejection
  - Safe test-deployment and production flag combinations
  - Hard-delete default
  - Debug-mode warnings
  - Secret validation (presence only, never value)
  - Diagnostic flag behaviour
  - Boolean parsing edge cases

All tests are non-destructive (no network, no file I/O, no env mutation).
"""
from __future__ import annotations

import pytest

from app.services.production_readiness import (
    check_production_readiness,
    ReadinessReport,
    PRODUCTION_FLAGS,
    CONNECTIVITY_SMOKE_FLAGS,
    CONTROLLED_TEST_DEPLOYMENT_FLAGS,
    _parse_bool_flag,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_GENIE_SPACE_ID = "01f17a93e6aa1b97a9da7ef329e15e46"
_VALID_HOST = "https://te-ss-coe-dev.cloud.databricks.com"
_VALID_WAREHOUSE = "/sql/1.0/warehouses/8e46614f7064d8fd"
_VALID_TABLE = "onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard"
_VALID_ENDPOINT = "projects/transparence-sessions/branches/production/endpoints/primary"
_VALID_HMAC_PLACEHOLDER = "x" * 64  # 64 chars ≥ 32 bytes


def _base_env(**overrides) -> dict:
    """Minimal valid environment for the test deployment configuration."""
    base = {
        "DATABRICKS_HOST": _VALID_HOST,
        "GENIE_SPACE_ID": _VALID_GENIE_SPACE_ID,
        "DATABRICKS_SQL_WAREHOUSE_PATH": _VALID_WAREHOUSE,
        "SHIPMENT_TABLE_NAME": _VALID_TABLE,
        "LLM_ENDPOINT_PRIMARY": "databricks-claude-sonnet-5",
        "USE_GENIE_BACKEND": "true",
        "GENIE_FALLBACK_TO_CUSTOM_PIPELINE": "true",
        "ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "false",
        "CONVERSATION_REPOSITORY_BACKEND": "memory",
        "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": "false",
        "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY": "false",
        "GENIE_DEBUG": "false",
        "NEW_PIPELINE_DEBUG": "false",
        "CONVERSATION_STATE_CLEANUP_HARD_DELETE": "false",
        "TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED": "false",
        "TRANSPARENCE_DIAGNOSTIC_LOG_SQL": "false",
        "GENIE_EXPORT_MODE": "returned_rows_only",
        "USE_NEW_ACCURACY_PIPELINE": "true",
        "NEW_PIPELINE_FALLBACK_TO_OLD": "true",
        "GENIE_RESPONSE_TIMEOUT_SECONDS": "120",
    }
    base.update(overrides)
    return base


# ===========================================================================
# GROUP 1: Boolean parser determinism (Tests 1–6)
# ===========================================================================


class TestBooleanParserDeterminism:
    """_parse_bool_flag must be deterministic for all supported inputs."""

    def test_1_true_lowercase(self):
        assert _parse_bool_flag("true") is True

    def test_2_true_uppercase(self):
        assert _parse_bool_flag("TRUE") is True

    def test_3_true_mixed_case_with_whitespace(self):
        assert _parse_bool_flag("  True  ") is True

    def test_4_false_lowercase(self):
        assert _parse_bool_flag("false") is False

    def test_5_false_numeric_zero(self):
        assert _parse_bool_flag("0") is False

    def test_6_unknown_value_returns_none(self):
        assert _parse_bool_flag("maybe") is None


# ===========================================================================
# GROUP 2: Required configuration variables (Tests 7–12)
# ===========================================================================


class TestRequiredConfiguration:
    """Required configuration variables must produce blocking reasons when absent."""

    def test_7_missing_host_is_blocking(self):
        env = _base_env(DATABRICKS_HOST="")
        report = check_production_readiness(env)
        assert not report.configuration_ready
        assert any("DATABRICKS_HOST" in r for r in report.blocking_reasons)

    def test_8_host_without_scheme_is_blocking(self):
        env = _base_env(DATABRICKS_HOST="te-ss-coe-dev.cloud.databricks.com")
        report = check_production_readiness(env)
        assert not report.configuration_ready
        assert any("https" in r for r in report.blocking_reasons)

    def test_9_missing_genie_space_id_is_blocking(self):
        env = _base_env(GENIE_SPACE_ID="")
        report = check_production_readiness(env)
        assert not report.configuration_ready
        assert any("GENIE_SPACE_ID" in r for r in report.blocking_reasons)

    def test_10_missing_warehouse_path_is_blocking(self):
        env = _base_env(DATABRICKS_SQL_WAREHOUSE_PATH="")
        report = check_production_readiness(env)
        assert not report.configuration_ready
        assert any("DATABRICKS_SQL_WAREHOUSE_PATH" in r for r in report.blocking_reasons)

    def test_11_warehouse_wrong_prefix_is_blocking(self):
        env = _base_env(DATABRICKS_SQL_WAREHOUSE_PATH="8e46614f7064d8fd")
        report = check_production_readiness(env)
        assert not report.configuration_ready
        assert any("DATABRICKS_SQL_WAREHOUSE_PATH" in r for r in report.blocking_reasons)

    def test_12_missing_shipment_table_is_blocking(self):
        env = _base_env(SHIPMENT_TABLE_NAME="")
        report = check_production_readiness(env)
        assert not report.configuration_ready
        assert any("SHIPMENT_TABLE_NAME" in r for r in report.blocking_reasons)


# ===========================================================================
# GROUP 3: Feature-flag dependency invariants (Tests 13–22)
# ===========================================================================


class TestFeatureFlagInvariants:
    """Feature-flag dependency graph invariants must be enforced."""

    def test_13_durable_without_trusted_identity_is_blocked(self):
        """Invariant 1: durable state cannot be enabled without trusted identity."""
        env = _base_env(
            ENABLE_DURABLE_GENIE_SESSION_ADAPTER="true",
            ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="false",
        )
        report = check_production_readiness(env)
        assert not report.feature_flags_ready or not report.durable_state_ready
        all_reasons = " ".join(report.blocking_reasons)
        assert "INVARIANT" in all_reasons or "requires" in all_reasons.lower()

    def test_14_lakebase_backend_without_enable_flag_is_blocked(self):
        """Invariant 2: lakebase backend requires explicit enable flag."""
        env = _base_env(
            CONVERSATION_REPOSITORY_BACKEND="lakebase",
            ENABLE_LAKEBASE_CONVERSATION_REPOSITORY="false",
        )
        report = check_production_readiness(env)
        assert not report.feature_flags_ready
        assert any("LAKEBASE" in r.upper() for r in report.blocking_reasons)

    def test_15_hard_delete_true_is_blocked(self):
        """Hard delete must not be enabled without explicit approval."""
        env = _base_env(CONVERSATION_STATE_CLEANUP_HARD_DELETE="true")
        report = check_production_readiness(env)
        assert not report.feature_flags_ready
        assert any("hard delete" in r.lower() or "HARD_DELETE" in r for r in report.blocking_reasons)

    def test_16_hard_delete_default_is_false(self):
        """Hard delete must default to false."""
        env = _base_env()
        report = check_production_readiness(env)
        assert not any("HARD_DELETE" in r for r in report.blocking_reasons)

    def test_17_genie_backend_without_space_id_is_blocked(self):
        """Invariant 4: USE_GENIE_BACKEND=true requires GENIE_SPACE_ID."""
        env = _base_env(USE_GENIE_BACKEND="true", GENIE_SPACE_ID="")
        report = check_production_readiness(env)
        assert not report.overall_ready

    def test_18_unknown_flag_value_is_blocked(self):
        """Unknown feature flag values must produce a blocking reason."""
        env = _base_env(USE_GENIE_BACKEND="maybe")
        report = check_production_readiness(env)
        assert not report.feature_flags_ready
        assert any("unrecognised" in r.lower() for r in report.blocking_reasons)

    def test_19_genie_export_mode_invalid_is_blocked(self):
        """Unknown GENIE_EXPORT_MODE must be blocked."""
        env = _base_env(GENIE_EXPORT_MODE="streaming")
        report = check_production_readiness(env)
        assert not report.feature_flags_ready
        assert any("GENIE_EXPORT_MODE" in r for r in report.blocking_reasons)

    def test_20_valid_export_mode_returned_rows_only(self):
        env = _base_env(GENIE_EXPORT_MODE="returned_rows_only")
        report = check_production_readiness(env)
        assert not any("GENIE_EXPORT_MODE" in r for r in report.blocking_reasons)

    def test_21_valid_export_mode_async_full_query(self):
        env = _base_env(GENIE_EXPORT_MODE="async_full_query")
        report = check_production_readiness(env)
        assert not any("GENIE_EXPORT_MODE" in r for r in report.blocking_reasons)

    def test_22_diagnostic_invalid_store_is_blocked_when_tracing_enabled(self):
        env = _base_env(
            TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED="true",
            TRANSPARENCE_DIAGNOSTIC_STORE="kafka",
        )
        report = check_production_readiness(env)
        assert not report.feature_flags_ready


# ===========================================================================
# GROUP 4: Safe flag combinations (Tests 23–28)
# ===========================================================================


class TestSafeFlagCombinations:
    """Valid test-deployment and production flag combinations must pass cleanly."""

    def test_23_smoke_flags_pass_flag_checks(self):
        """CONNECTIVITY_SMOKE_FLAGS combined with required config must pass flag checks."""
        env = _base_env(**CONNECTIVITY_SMOKE_FLAGS)
        report = check_production_readiness(env)
        # No blocking reasons from domain flag invariants
        flag_reasons = [r for r in report.blocking_reasons if "INVARIANT" in r]
        assert not flag_reasons, f"Unexpected invariant violations: {flag_reasons}"
        assert report.feature_flags_ready
        assert report.genie_configuration_ready

    def test_24_production_flags_with_full_config_pass(self):
        """PRODUCTION_FLAGS with Lakebase + HMAC env must pass invariant checks."""
        env = _base_env(**PRODUCTION_FLAGS)
        env["CONVERSATION_OWNER_HMAC_SECRET"] = _VALID_HMAC_PLACEHOLDER
        env["LAKEBASE_ENDPOINT_NAME"] = _VALID_ENDPOINT
        env["PGHOST"] = "ep-withered-king-d257e0k1.database.us-east-1.cloud.databricks.com"
        env["PGDATABASE"] = "databricks_postgres"
        env["PGPORT"] = "5432"
        env["PGUSER"] = "sp_user"
        env["PGSSLMODE"] = "require"
        report = check_production_readiness(env)
        assert report.feature_flags_ready, f"Blocking: {report.blocking_reasons}"
        assert report.trusted_identity_ready
        assert report.durable_state_ready
        assert report.genie_configuration_ready
        assert report.lakebase_configuration_ready

    def test_25_smoke_profile_is_connectivity_smoke_ready(self):
        """Smoke profile must produce connectivity_smoke_ready=True.

        Note: connectivity_smoke_ready=True does NOT authorise controlled
        test deployment. Use controlled_test_deployment_ready for that gate.
        """
        env = _base_env(**CONNECTIVITY_SMOKE_FLAGS)
        report = check_production_readiness(env)
        assert report.connectivity_smoke_ready, (
            f"Expected connectivity_smoke_ready=True. Blocking: {report.blocking_reasons}"
        )
        # Domain checks pass even for smoke profile
        assert report.overall_ready, (
            f"Expected overall_ready=True for smoke. Blocking: {report.blocking_reasons}"
        )

    def test_26_production_flags_include_no_debug(self):
        assert PRODUCTION_FLAGS["GENIE_DEBUG"] == "false"
        assert PRODUCTION_FLAGS["NEW_PIPELINE_DEBUG"] == "false"

    def test_27_production_flags_no_fallback_to_legacy(self):
        assert PRODUCTION_FLAGS["NEW_PIPELINE_FALLBACK_TO_OLD"] == "false"

    def test_28_production_flags_no_hard_delete(self):
        assert PRODUCTION_FLAGS["CONVERSATION_STATE_CLEANUP_HARD_DELETE"] == "false"


# ===========================================================================
# GROUP 5: Secret validation (Tests 29–33)
# ===========================================================================


class TestSecretValidation:
    """Secret validation must check presence only; values must never appear."""

    def test_29_trusted_identity_enabled_without_hmac_is_blocked(self):
        env = _base_env(ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="true")
        # CONVERSATION_OWNER_HMAC_SECRET deliberately absent
        env.pop("CONVERSATION_OWNER_HMAC_SECRET", None)
        report = check_production_readiness(env)
        assert not report.trusted_identity_ready
        assert any("CONVERSATION_OWNER_HMAC_SECRET" in r for r in report.blocking_reasons)

    def test_30_secret_value_does_not_appear_in_report(self):
        """Even if a secret is present its value must never appear in the report."""
        secret_value = "super-secret-hmac-key-that-must-not-appear-in-report"
        env = _base_env(
            ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="true",
            CONVERSATION_OWNER_HMAC_SECRET=secret_value,
        )
        report = check_production_readiness(env)
        all_text = " ".join(report.blocking_reasons + report.warnings)
        assert secret_value not in all_text

    def test_31_trusted_identity_enabled_with_hmac_present_passes(self):
        env = _base_env(
            ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="true",
            CONVERSATION_OWNER_HMAC_SECRET=_VALID_HMAC_PLACEHOLDER,
        )
        report = check_production_readiness(env)
        assert report.trusted_identity_ready
        assert not any(
            "CONVERSATION_OWNER_HMAC_SECRET" in r
            for r in report.blocking_reasons
        )

    def test_32_secret_not_required_when_trusted_identity_disabled(self):
        """When trusted identity is disabled, missing HMAC secret does not block."""
        env = _base_env(ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="false")
        env.pop("CONVERSATION_OWNER_HMAC_SECRET", None)
        report = check_production_readiness(env)
        assert report.trusted_identity_ready
        assert not any(
            "CONVERSATION_OWNER_HMAC_SECRET" in r
            for r in report.blocking_reasons
        )

    def test_33_secret_reference_name_only_not_value_in_blocking(self):
        """Blocking reasons must reference variable names, not values."""
        env = _base_env(ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="true")
        env.pop("CONVERSATION_OWNER_HMAC_SECRET", None)
        report = check_production_readiness(env)
        for reason in report.blocking_reasons:
            # Must mention the name of the variable, not any value
            assert "password" not in reason.lower()
            assert "token" not in reason.lower()


# ===========================================================================
# GROUP 6: Debug-mode warnings (Tests 34–38)
# ===========================================================================


class TestDebugModeWarnings:
    """Debug flags must produce warnings (not blocking reasons) in readiness output."""

    def test_34_genie_debug_true_produces_warning(self):
        env = _base_env(GENIE_DEBUG="true")
        report = check_production_readiness(env)
        assert any("GENIE_DEBUG" in w for w in report.warnings)

    def test_35_new_pipeline_debug_true_produces_warning(self):
        env = _base_env(NEW_PIPELINE_DEBUG="true")
        report = check_production_readiness(env)
        assert any("NEW_PIPELINE_DEBUG" in w for w in report.warnings)

    def test_36_diagnostic_tracing_enabled_produces_warning(self):
        env = _base_env(
            TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED="true",
            TRANSPARENCE_DIAGNOSTIC_STORE="delta",
        )
        report = check_production_readiness(env)
        assert any("diagnostic" in w.lower() for w in report.warnings)

    def test_37_diagnostic_log_sql_true_produces_warning(self):
        env = _base_env(TRANSPARENCE_DIAGNOSTIC_LOG_SQL="true")
        report = check_production_readiness(env)
        assert any("SQL" in w for w in report.warnings)

    def test_38_all_debug_false_produces_no_debug_warnings(self):
        env = _base_env(
            GENIE_DEBUG="false",
            NEW_PIPELINE_DEBUG="false",
            TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED="false",
            TRANSPARENCE_DIAGNOSTIC_LOG_SQL="false",
        )
        report = check_production_readiness(env)
        debug_warnings = [
            w for w in report.warnings
            if "debug" in w.lower() or "tracing" in w.lower()
        ]
        assert not debug_warnings


# ===========================================================================
# GROUP 7: Lakebase configuration (Tests 39–43)
# ===========================================================================


class TestLakebaseConfiguration:
    """Lakebase configuration must be validated when durable state is required."""

    def test_39_lakebase_not_required_when_durable_disabled(self):
        """Missing PG vars should not block when durable adapter is disabled."""
        env = _base_env(
            ENABLE_DURABLE_GENIE_SESSION_ADAPTER="false",
            CONVERSATION_REPOSITORY_BACKEND="memory",
        )
        # Deliberately omit all PG vars
        for var in ["PGHOST", "PGDATABASE", "PGPORT", "PGUSER", "PGSSLMODE", "LAKEBASE_ENDPOINT_NAME"]:
            env.pop(var, None)
        report = check_production_readiness(env)
        assert report.lakebase_configuration_ready
        assert not any("PG" in r for r in report.blocking_reasons)

    def test_40_lakebase_endpoint_invalid_format_is_blocked(self):
        """LAKEBASE_ENDPOINT_NAME with wrong format blocks when durable is enabled."""
        env = _base_env(
            ENABLE_DURABLE_GENIE_SESSION_ADAPTER="true",
            ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="true",
            CONVERSATION_OWNER_HMAC_SECRET=_VALID_HMAC_PLACEHOLDER,
            LAKEBASE_ENDPOINT_NAME="bad-endpoint-format",
            PGHOST="somehost",
            PGDATABASE="db",
            PGPORT="5432",
            PGUSER="user",
            PGSSLMODE="require",
        )
        report = check_production_readiness(env)
        assert not report.configuration_ready or not report.lakebase_configuration_ready

    def test_41_lakebase_insecure_ssl_is_blocked(self):
        env = _base_env(
            ENABLE_DURABLE_GENIE_SESSION_ADAPTER="true",
            ENABLE_LAKEBASE_CONVERSATION_REPOSITORY="true",
            ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="true",
            CONVERSATION_OWNER_HMAC_SECRET=_VALID_HMAC_PLACEHOLDER,
            CONVERSATION_REPOSITORY_BACKEND="lakebase",
            LAKEBASE_ENDPOINT_NAME=_VALID_ENDPOINT,
            PGHOST="somehost",
            PGDATABASE="db",
            PGPORT="5432",
            PGUSER="user",
            PGSSLMODE="disable",
        )
        report = check_production_readiness(env)
        assert not report.lakebase_configuration_ready
        assert any("TLS" in r or "sslmode" in r.lower() for r in report.blocking_reasons)

    def test_42_lakebase_valid_endpoint_passes(self):
        env = _base_env(
            ENABLE_DURABLE_GENIE_SESSION_ADAPTER="true",
            ENABLE_LAKEBASE_CONVERSATION_REPOSITORY="true",
            ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="true",
            CONVERSATION_REPOSITORY_BACKEND="lakebase",
            CONVERSATION_OWNER_HMAC_SECRET=_VALID_HMAC_PLACEHOLDER,
            LAKEBASE_ENDPOINT_NAME=_VALID_ENDPOINT,
            PGHOST="ep-withered-king-d257e0k1.database.us-east-1.cloud.databricks.com",
            PGDATABASE="databricks_postgres",
            PGPORT="5432",
            PGUSER="sp_role",
            PGSSLMODE="require",
        )
        report = check_production_readiness(env)
        assert report.lakebase_configuration_ready, (
            f"Blocking: {report.blocking_reasons}"
        )

    def test_43_missing_lakebase_endpoint_blocks_when_durable_enabled(self):
        env = _base_env(
            ENABLE_DURABLE_GENIE_SESSION_ADAPTER="true",
            ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="true",
            CONVERSATION_OWNER_HMAC_SECRET=_VALID_HMAC_PLACEHOLDER,
        )
        env.pop("LAKEBASE_ENDPOINT_NAME", None)
        report = check_production_readiness(env)
        assert not report.overall_ready


# ===========================================================================
# GROUP 8: ReadinessReport structure (Tests 44–47)
# ===========================================================================


class TestReadinessReportStructure:
    """ReadinessReport must be a frozen immutable dataclass."""

    def test_44_report_is_frozen(self):
        env = _base_env()
        report = check_production_readiness(env)
        with pytest.raises((AttributeError, TypeError)):
            report.overall_ready = False  # type: ignore

    def test_45_report_has_all_fields(self):
        env = _base_env()
        report = check_production_readiness(env)
        assert hasattr(report, "configuration_ready")
        assert hasattr(report, "feature_flags_ready")
        assert hasattr(report, "trusted_identity_ready")
        assert hasattr(report, "durable_state_ready")
        assert hasattr(report, "genie_configuration_ready")
        assert hasattr(report, "lakebase_configuration_ready")
        assert hasattr(report, "resource_bindings_ready")
        assert hasattr(report, "overall_ready")
        assert hasattr(report, "blocking_reasons")
        assert hasattr(report, "warnings")
        # Deployment-tier readiness fields (Phase 4D2 correction)
        assert hasattr(report, "connectivity_smoke_ready")
        assert hasattr(report, "controlled_test_deployment_ready")
        assert hasattr(report, "production_ready")
        assert hasattr(report, "deployment_sync_required")

    def test_46_no_secrets_in_report_fields(self):
        """Report fields must not contain any value that looks like a secret."""
        env = _base_env(
            ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY="true",
            CONVERSATION_OWNER_HMAC_SECRET="s3cr3t-hmac-val",
        )
        report = check_production_readiness(env)
        for reason in report.blocking_reasons + report.warnings:
            assert "s3cr3t-hmac-val" not in reason

    def test_47_counter_fields_are_positive_integers(self):
        env = _base_env()
        report = check_production_readiness(env)
        assert isinstance(report.configuration_items_checked, int)
        assert report.configuration_items_checked > 0
        assert isinstance(report.flags_checked, int)
        assert report.flags_checked > 0

# ===========================================================================
# GROUP 9: Deployment profile separation (Tests 48–58)
# ===========================================================================


_FULL_SUFFICIENT_SNAPSHOT: dict = {
    "genie_space_can_run": "PRESENT_AND_SUFFICIENT",
    "sql_warehouse_can_use": "PRESENT_AND_SUFFICIENT",
    "shipment_table_select": "PRESENT_AND_SUFFICIENT",
    "lakebase_can_connect": "PRESENT_AND_SUFFICIENT",
    "lakebase_app_conversation_dml": "PRESENT_AND_SUFFICIENT",
    "secret_scope_hmac_read": "PRESENT_AND_SUFFICIENT",
    "genie_space_end_user_access": "NOT_REQUIRED",
}


def _controlled_base_env() -> dict:
    """Full environment satisfying Profile B (controlled test deployment)."""
    env = _base_env(**CONTROLLED_TEST_DEPLOYMENT_FLAGS)
    env["CONVERSATION_OWNER_HMAC_SECRET"] = _VALID_HMAC_PLACEHOLDER
    env["LAKEBASE_ENDPOINT_NAME"] = _VALID_ENDPOINT
    env["PGHOST"] = "ep-withered-king-d257e0k1.database.us-east-1.cloud.databricks.com"
    env["PGDATABASE"] = "databricks_postgres"
    env["PGPORT"] = "5432"
    env["PGUSER"] = "sp_role"
    env["PGSSLMODE"] = "require"
    return env


class TestDeploymentProfileSeparation:
    """Profile A (smoke) must be distinct from Profile B (controlled deployment)."""

    def test_48_smoke_flags_produce_connectivity_smoke_ready(self):
        """Smoke flags must set connectivity_smoke_ready=True."""
        env = _base_env(**CONNECTIVITY_SMOKE_FLAGS)
        report = check_production_readiness(env)
        assert report.connectivity_smoke_ready, (
            f"Expected connectivity_smoke_ready=True. Blocking: {report.blocking_reasons}"
        )

    def test_49_smoke_flags_do_not_produce_controlled_deployment_ready(self):
        """Smoke profile must NOT authorise controlled test deployment."""
        env = _base_env(**CONNECTIVITY_SMOKE_FLAGS)
        report = check_production_readiness(env)
        assert not report.controlled_test_deployment_ready, (
            "Smoke profile must not produce controlled_test_deployment_ready=True. "
            "That would misclassify an incomplete architecture as deployment-safe."
        )

    def test_50_smoke_profile_has_mandatory_limitations(self):
        """Smoke profile must have trusted identity, durable state, and Lakebase disabled."""
        assert CONNECTIVITY_SMOKE_FLAGS["ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY"] == "false"
        assert CONNECTIVITY_SMOKE_FLAGS["ENABLE_DURABLE_GENIE_SESSION_ADAPTER"] == "false"
        assert CONNECTIVITY_SMOKE_FLAGS["CONVERSATION_REPOSITORY_BACKEND"] == "memory"
        assert CONNECTIVITY_SMOKE_FLAGS["ENABLE_LAKEBASE_CONVERSATION_REPOSITORY"] == "false"

    def test_51_controlled_profile_requires_no_fallback(self):
        """Controlled deployment must disable all legacy fallback paths."""
        assert CONTROLLED_TEST_DEPLOYMENT_FLAGS["GENIE_FALLBACK_TO_CUSTOM_PIPELINE"] == "false"
        assert CONTROLLED_TEST_DEPLOYMENT_FLAGS["NEW_PIPELINE_FALLBACK_TO_OLD"] == "false"

    def test_52_smoke_flags_produce_controlled_deployment_blockers(self):
        """Using smoke flags must produce CONTROLLED_DEPLOYMENT_BLOCKER reasons."""
        env = _base_env(**CONNECTIVITY_SMOKE_FLAGS)
        report = check_production_readiness(env)
        ctrl_blockers = [
            r for r in report.blocking_reasons
            if "CONTROLLED_DEPLOYMENT_BLOCKER" in r
        ]
        assert len(ctrl_blockers) > 0, (
            "Smoke flags must produce at least one CONTROLLED_DEPLOYMENT_BLOCKER reason."
        )

    def test_53_memory_repository_rejected_for_controlled_deployment(self):
        """Memory repository must produce a CONTROLLED_DEPLOYMENT_BLOCKER."""
        env = _base_env(**CONNECTIVITY_SMOKE_FLAGS)  # has memory backend
        report = check_production_readiness(env)
        assert not report.controlled_test_deployment_ready
        backend_blockers = [
            r for r in report.blocking_reasons
            if "CONTROLLED_DEPLOYMENT_BLOCKER" in r and "lakebase" in r.lower()
        ]
        assert len(backend_blockers) > 0, (
            "Memory repository must be explicitly blocked for controlled deployment."
        )

    def test_54_disabled_trusted_identity_rejected_for_controlled_deployment(self):
        """Disabled trusted identity must produce a CONTROLLED_DEPLOYMENT_BLOCKER."""
        env = _base_env(**CONNECTIVITY_SMOKE_FLAGS)  # has trusted=false
        report = check_production_readiness(env)
        assert not report.controlled_test_deployment_ready
        trusted_blockers = [
            r for r in report.blocking_reasons
            if "CONTROLLED_DEPLOYMENT_BLOCKER" in r and "trusted" in r.lower()
        ]
        assert len(trusted_blockers) > 0, (
            "Disabled trusted identity must be explicitly blocked for controlled deployment."
        )

    def test_55_disabled_durable_state_rejected_for_controlled_deployment(self):
        """Disabled durable state must produce a CONTROLLED_DEPLOYMENT_BLOCKER."""
        env = _base_env(**CONNECTIVITY_SMOKE_FLAGS)  # has durable=false
        report = check_production_readiness(env)
        assert not report.controlled_test_deployment_ready
        durable_blockers = [
            r for r in report.blocking_reasons
            if "CONTROLLED_DEPLOYMENT_BLOCKER" in r and "durable" in r.lower()
        ]
        assert len(durable_blockers) > 0, (
            "Disabled durable state must be explicitly blocked for controlled deployment."
        )

    def test_56_legacy_fallback_rejected_for_controlled_deployment(self):
        """Genie fallback=true must produce a CONTROLLED_DEPLOYMENT_BLOCKER."""
        env = _base_env(**CONNECTIVITY_SMOKE_FLAGS)  # has GENIE_FALLBACK=true
        report = check_production_readiness(env)
        assert not report.controlled_test_deployment_ready
        fallback_blockers = [
            r for r in report.blocking_reasons
            if "CONTROLLED_DEPLOYMENT_BLOCKER" in r and "fallback" in r.lower()
        ]
        assert len(fallback_blockers) > 0, (
            "Genie fallback=true must be explicitly blocked for controlled deployment."
        )

    def test_57_controlled_flags_with_sufficient_permissions_pass(self):
        """Controlled flags + full sufficient permission snapshot must authorise deployment."""
        env = _controlled_base_env()
        report = check_production_readiness(env, permission_snapshot=_FULL_SUFFICIENT_SNAPSHOT)
        assert report.controlled_test_deployment_ready, (
            f"Expected controlled_test_deployment_ready=True. "
            f"Blocking: {report.blocking_reasons}"
        )
        assert report.production_ready, (
            "production_ready must equal controlled_test_deployment_ready at this stage."
        )

    def test_58_no_permission_snapshot_blocks_controlled_deployment(self):
        """Without a permission snapshot, controlled_test_deployment_ready must be False."""
        env = _controlled_base_env()
        report = check_production_readiness(env)  # No snapshot
        assert not report.controlled_test_deployment_ready, (
            "Missing permission snapshot must block controlled_test_deployment_ready."
        )
        assert any(
            "PERMISSION_BLOCKER" in r for r in report.blocking_reasons
        ), "PERMISSION_BLOCKER must appear in blocking_reasons when no snapshot is provided."

    def test_59_cannot_verify_permissions_block_controlled_deployment(self):
        """CANNOT_VERIFY permissions must block controlled_test_deployment_ready."""
        env = _controlled_base_env()
        unverifiable_snapshot = {
            "genie_space_can_run": "CANNOT_VERIFY",
            "sql_warehouse_can_use": "CANNOT_VERIFY",
            "shipment_table_select": "PRESENT_AND_SUFFICIENT",
            "lakebase_can_connect": "PRESENT_AND_SUFFICIENT",
            "lakebase_app_conversation_dml": "PRESENT_AND_SUFFICIENT",
            "secret_scope_hmac_read": "PRESENT_AND_SUFFICIENT",
            "genie_space_end_user_access": "NOT_REQUIRED",
        }
        report = check_production_readiness(env, permission_snapshot=unverifiable_snapshot)
        assert not report.controlled_test_deployment_ready, (
            "CANNOT_VERIFY mandatory permissions must block controlled_test_deployment_ready."
        )

    def test_60_deployment_sync_required_is_always_true(self):
        """deployment_sync_required must always be True (DEPLOYMENT PREPARATION BLOCKER)."""
        env = _base_env(**CONNECTIVITY_SMOKE_FLAGS)
        report = check_production_readiness(env)
        assert report.deployment_sync_required is True, (
            "deployment_sync_required must always be True; "
            "git repo must be synced to app source before deployment."
        )
        # Must be flagged in warnings (not blocking_reasons — it is a prep action)
        assert any(
            "DEPLOYMENT_PREPARATION_BLOCKER" in w for w in report.warnings
        ), "DEPLOYMENT_PREPARATION_BLOCKER must appear in warnings."

    def test_61_production_flags_dict_matches_controlled_architecture(self):
        """CONTROLLED_TEST_DEPLOYMENT_FLAGS must have all mandatory architecture flags."""
        assert CONTROLLED_TEST_DEPLOYMENT_FLAGS["ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY"] == "true"
        assert CONTROLLED_TEST_DEPLOYMENT_FLAGS["ENABLE_DURABLE_GENIE_SESSION_ADAPTER"] == "true"
        assert CONTROLLED_TEST_DEPLOYMENT_FLAGS["ENABLE_LAKEBASE_CONVERSATION_REPOSITORY"] == "true"
        assert CONTROLLED_TEST_DEPLOYMENT_FLAGS["CONVERSATION_REPOSITORY_BACKEND"] == "lakebase"
