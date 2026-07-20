"""Production readiness checker — Phase 4D2.

Provides a structured, non-destructive readiness assessment for controlled
test deployment of TransparencE. Designed for DevOps validation and automated
test gating.

Security invariants:
  - Never returns secret values, owner identifiers, tokens, or credentials.
  - Never returns raw exception messages that may contain sensitive data.
  - Never makes network I/O or opens database connections.
  - Never reads os.environ at module import time.
  - All blocking reasons are sanitized plain-English descriptions.

Public surface:
  ReadinessReport      — immutable structured result dataclass.
  ReadinessStatus      — enum: READY | BLOCKED | UNKNOWN.
  check_production_readiness(environ=None) -> ReadinessReport
  check_permission_snapshot(snapshot: dict) -> PermissionReadinessReport

Do not expose this report via a public HTTP endpoint; it is for
controlled deployment validation and automated tests only.

Phase 4D2 — TransparencE Genie State Persistence
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ReadinessStatus(str, Enum):
    """Tri-state readiness status."""
    READY = "READY"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class PermissionStatus(str, Enum):
    """Permission check result classification."""
    PRESENT_AND_SUFFICIENT = "PRESENT_AND_SUFFICIENT"
    PRESENT_BUT_INSUFFICIENT = "PRESENT_BUT_INSUFFICIENT"
    MISSING = "MISSING"
    CANNOT_VERIFY = "CANNOT_VERIFY"
    NOT_REQUIRED = "NOT_REQUIRED"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionEntry:
    """A single permission check result. Sensitive details are never stored."""
    resource: str
    resource_id: str          # Non-sensitive identifier (e.g. genie space ID)
    principal: str            # Non-sensitive principal label
    required_permission: str
    status: PermissionStatus
    blocking: bool            # Whether this blocks readiness
    notes: str = ""           # Human-readable context; no secrets


@dataclass(frozen=True)
class PermissionReadinessReport:
    """Structured permission readiness result."""
    entries: List[PermissionEntry]
    overall_ready: bool
    blocking_entries: List[PermissionEntry]
    unverifiable_entries: List[PermissionEntry]

    def __post_init__(self) -> None:
        # Validate consistency
        computed_blocking = [e for e in self.entries if e.blocking and e.status != PermissionStatus.PRESENT_AND_SUFFICIENT]
        # Allow caller to pass pre-computed blocking_entries for clarity
        # (they may differ if additional business logic is applied)


@dataclass(frozen=True)
class ReadinessReport:
    """Structured production readiness report.

    All fields are safe to log and return to DevOps — no secrets,
    tokens, raw exceptions, owner hashes, or PII.
    """
    configuration_ready: bool
    feature_flags_ready: bool
    trusted_identity_ready: bool
    durable_state_ready: bool
    genie_configuration_ready: bool
    lakebase_configuration_ready: bool
    resource_bindings_ready: bool
    overall_ready: bool
    blocking_reasons: List[str]
    warnings: List[str]
    # Informational counts only — no secret values
    configuration_items_checked: int
    flags_checked: int


# ---------------------------------------------------------------------------
# Boolean parser (shared — avoids duplicating logic from service modules)
# ---------------------------------------------------------------------------

_TRUTHY: frozenset = frozenset({"true", "1", "yes", "on"})
_FALSEY: frozenset = frozenset({"false", "0", "no", "off", ""})


def _parse_bool_flag(value: Optional[str]) -> Optional[bool]:
    """Parse a feature flag string to bool. Returns None for unrecognised values."""
    normalised = (value or "").strip().lower()
    if normalised in _TRUTHY:
        return True
    if normalised in _FALSEY:
        return False
    return None  # Unrecognised


# ---------------------------------------------------------------------------
# Individual check helpers
# ---------------------------------------------------------------------------


def _check_configuration(environ: Mapping[str, str]) -> tuple[bool, List[str], List[str], int]:
    """Validate required non-secret configuration variables.

    Returns (ok, blocking_reasons, warnings, items_checked).
    Validates presence and format only — never reads or logs secret values.
    """
    reasons: List[str] = []
    warnings: List[str] = []
    checked = 0

    # --- Workspace host ---
    checked += 1
    host = environ.get("DATABRICKS_HOST", "").strip()
    if not host:
        reasons.append("DATABRICKS_HOST is not configured.")
    elif not (host.startswith("https://") or host.startswith("http://")):
        reasons.append("DATABRICKS_HOST must start with https://.")

    # --- Genie Space ID ---
    checked += 1
    genie_space_id = environ.get("GENIE_SPACE_ID", "").strip()
    if not genie_space_id:
        reasons.append("GENIE_SPACE_ID is not configured.")
    else:
        # Must be a non-empty hex string (typical UUID/UUID-like format)
        clean_id = genie_space_id.replace("-", "").lower()
        if not re.match(r"^[0-9a-f]{32}$", clean_id):
            reasons.append("GENIE_SPACE_ID does not appear to be a valid space identifier.")

    # --- SQL Warehouse Path ---
    checked += 1
    warehouse_path = environ.get("DATABRICKS_SQL_WAREHOUSE_PATH", "").strip()
    if not warehouse_path:
        reasons.append("DATABRICKS_SQL_WAREHOUSE_PATH is not configured.")
    elif not warehouse_path.startswith("/sql/1.0/warehouses/"):
        reasons.append("DATABRICKS_SQL_WAREHOUSE_PATH must start with /sql/1.0/warehouses/.")

    # --- Shipment table ---
    checked += 1
    shipment_table = environ.get("SHIPMENT_TABLE_NAME", "").strip()
    if not shipment_table:
        reasons.append("SHIPMENT_TABLE_NAME is not configured.")
    else:
        parts = shipment_table.split(".")
        if len(parts) != 3 or not all(parts):
            reasons.append("SHIPMENT_TABLE_NAME must be a fully qualified catalog.schema.table name.")

    # --- LLM endpoints (required when Genie backend is enabled) ---
    checked += 1
    llm_primary = environ.get("LLM_ENDPOINT_PRIMARY", "").strip()
    if not llm_primary:
        warnings.append("LLM_ENDPOINT_PRIMARY is not set; custom pipeline fallback will be degraded.")

    # --- Lakebase endpoint name (when durable adapter is enabled) ---
    checked += 1
    durable_enabled_raw = environ.get("ENABLE_DURABLE_GENIE_SESSION_ADAPTER", "false")
    durable_enabled = _parse_bool_flag(durable_enabled_raw)
    lakebase_enabled_raw = environ.get("ENABLE_LAKEBASE_CONVERSATION_REPOSITORY", "false")
    lakebase_backend = environ.get("CONVERSATION_REPOSITORY_BACKEND", "memory").strip().lower()

    if durable_enabled is True:
        endpoint_name = environ.get("LAKEBASE_ENDPOINT_NAME", "").strip()
        if not endpoint_name:
            reasons.append(
                "LAKEBASE_ENDPOINT_NAME is required when ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true "
                "but is not configured. Verify the 'postgres' app resource binding is present."
            )
        else:
            # Structural pattern check (no network I/O)
            _ENDPOINT_RE = re.compile(
                r"^projects/[a-zA-Z0-9._-]+"
                r"/branches/[a-zA-Z0-9._-]+"
                r"/endpoints/[a-zA-Z0-9._-]+$"
            )
            if not _ENDPOINT_RE.match(endpoint_name):
                reasons.append(
                    "LAKEBASE_ENDPOINT_NAME does not match "
                    "'projects/.../branches/.../endpoints/...' pattern."
                )

    # --- HMAC secret reference readiness (presence of env var, not value) ---
    checked += 1
    trusted_enabled_raw = environ.get("ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY", "false")
    trusted_enabled = _parse_bool_flag(trusted_enabled_raw)
    if trusted_enabled is True:
        # Only validate presence of the env var — never read or log its value
        hmac_present = "CONVERSATION_OWNER_HMAC_SECRET" in environ
        if not hmac_present:
            reasons.append(
                "CONVERSATION_OWNER_HMAC_SECRET is required when "
                "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true but is not present in the environment."
            )
        # Do not validate the secret value here; length/quality checks happen in
        # request_owner_identity.py at first-use time.

    # --- Production debug mode check ---
    checked += 1
    genie_debug = _parse_bool_flag(environ.get("GENIE_DEBUG", "false"))
    new_pipeline_debug = _parse_bool_flag(environ.get("NEW_PIPELINE_DEBUG", "false"))
    diag_tracing = _parse_bool_flag(environ.get("TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED", "false"))
    diag_log_sql = _parse_bool_flag(environ.get("TRANSPARENCE_DIAGNOSTIC_LOG_SQL", "false"))
    if genie_debug is True:
        warnings.append("GENIE_DEBUG=true is set; verbose debug logging is NOT safe for production.")
    if new_pipeline_debug is True:
        warnings.append("NEW_PIPELINE_DEBUG=true is set; verbose debug logging is NOT safe for production.")
    if diag_tracing is True:
        warnings.append(
            "TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED=true: diagnostic tracing is active. "
            "This is acceptable for a soak test but must not run indefinitely in production."
        )
    if diag_log_sql is True:
        warnings.append(
            "TRANSPARENCE_DIAGNOSTIC_LOG_SQL=true: SQL will be persisted in traces. "
            "Only enable for isolated diagnostic deployments."
        )

    ok = len(reasons) == 0
    return ok, reasons, warnings, checked


def _check_feature_flags(environ: Mapping[str, str]) -> tuple[bool, List[str], List[str], int]:
    """Validate feature-flag consistency and dependency invariants.

    Returns (ok, blocking_reasons, warnings, flags_checked).
    """
    reasons: List[str] = []
    warnings: List[str] = []
    checked = 0

    def _get_bool(name: str, default: str = "false") -> Optional[bool]:
        nonlocal checked
        checked += 1
        raw = environ.get(name, default)
        val = _parse_bool_flag(raw)
        if val is None:
            reasons.append(
                f"Feature flag '{name}' has an unrecognised value. "
                "Valid values: true, false, 1, 0, yes, no, on, off."
            )
        return val

    use_genie = _get_bool("USE_GENIE_BACKEND", "false")
    genie_fallback = _get_bool("GENIE_FALLBACK_TO_CUSTOM_PIPELINE", "true")
    use_new_acc = _get_bool("USE_NEW_ACCURACY_PIPELINE", "false")
    pipeline_fallback = _get_bool("NEW_PIPELINE_FALLBACK_TO_OLD", "true")
    use_delta_state = _get_bool("USE_DELTA_CONVERSATION_STATE", "false")
    enable_durable = _get_bool("ENABLE_DURABLE_GENIE_SESSION_ADAPTER", "false")
    enable_lakebase_repo = _get_bool("ENABLE_LAKEBASE_CONVERSATION_REPOSITORY", "false")
    enable_trusted_id = _get_bool("ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY", "false")

    # Hard-delete must remain false by default in all configurations
    hard_delete_raw = environ.get("CONVERSATION_STATE_CLEANUP_HARD_DELETE", "false")
    hard_delete = _parse_bool_flag(hard_delete_raw)
    checked += 1
    if hard_delete is True:
        reasons.append(
            "CONVERSATION_STATE_CLEANUP_HARD_DELETE=true: hard delete is enabled. "
            "This must not be set true without explicit approval."
        )
    if hard_delete is None:
        reasons.append(
            "CONVERSATION_STATE_CLEANUP_HARD_DELETE has an invalid boolean value."
        )

    # INVARIANT 1: Durable state requires trusted owner identity
    if enable_durable is True and enable_trusted_id is not True:
        reasons.append(
            "INVARIANT VIOLATION: ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true requires "
            "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true. "
            "Durable state cannot establish ownership without verified identity."
        )

    # INVARIANT 2: Lakebase backend must be explicitly enabled before selection
    lakebase_backend = environ.get("CONVERSATION_REPOSITORY_BACKEND", "memory").strip().lower()
    if lakebase_backend == "lakebase" and enable_lakebase_repo is not True:
        reasons.append(
            "INVARIANT VIOLATION: CONVERSATION_REPOSITORY_BACKEND=lakebase requires "
            "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY=true."
        )

    # INVARIANT 3: Durable adapter without Lakebase backend is inconsistent
    if enable_durable is True and lakebase_backend != "lakebase":
        warnings.append(
            "ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true but "
            "CONVERSATION_REPOSITORY_BACKEND is not 'lakebase'. "
            "The durable adapter will use the memory backend which does not survive restarts."
        )

    # INVARIANT 4: Genie backend requires a valid Space ID
    if use_genie is True:
        space_id = environ.get("GENIE_SPACE_ID", "").strip()
        if not space_id:
            reasons.append(
                "INVARIANT VIOLATION: USE_GENIE_BACKEND=true requires GENIE_SPACE_ID to be set."
            )

    # INVARIANT 5: Production fallback after durable failure must be explicit
    # GENIE_FALLBACK_TO_CUSTOM_PIPELINE=true is a safety net; warn if enabled in
    # full durable mode so the operator is aware.
    if enable_durable is True and genie_fallback is True:
        warnings.append(
            "GENIE_FALLBACK_TO_CUSTOM_PIPELINE=true with ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true: "
            "Genie failures will fall back to the custom pipeline. "
            "Confirm this is the intended behaviour for the test deployment."
        )

    # INVARIANT 6: Debug logging must be explicitly acknowledged as unsafe in production
    # (warnings already added in _check_configuration; no need to add reasons here)

    # INVARIANT 7: Genie export mode must be a known value
    export_mode = environ.get("GENIE_EXPORT_MODE", "returned_rows_only").strip().lower()
    checked += 1
    if export_mode not in ("returned_rows_only", "async_full_query"):
        reasons.append(
            f"GENIE_EXPORT_MODE has unrecognised value. "
            "Supported values: 'returned_rows_only', 'async_full_query'."
        )

    # INVARIANT 8: Diagnostic store must be a known value when tracing is enabled
    diag_tracing = _parse_bool_flag(environ.get("TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED", "false"))
    checked += 1
    if diag_tracing is True:
        diag_store = environ.get("TRANSPARENCE_DIAGNOSTIC_STORE", "delta").strip().lower()
        if diag_store not in ("delta", "none"):
            reasons.append(
                "TRANSPARENCE_DIAGNOSTIC_STORE has an unrecognised value. "
                "Supported values: 'delta', 'none'."
            )

    ok = len(reasons) == 0
    return ok, reasons, warnings, checked


def _check_trusted_identity(environ: Mapping[str, str]) -> tuple[bool, List[str], List[str]]:
    """Validate trusted identity readiness (does not read secret value).

    Returns (ok, blocking_reasons, warnings).
    """
    reasons: List[str] = []
    warnings: List[str] = []

    enabled_raw = environ.get("ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY", "false")
    enabled = _parse_bool_flag(enabled_raw)

    if enabled is None:
        reasons.append(
            "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY has an unrecognised value."
        )
        return False, reasons, warnings

    if not enabled:
        # Disabled is acceptable for initial test deployment
        warnings.append(
            "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=false: trusted owner identity is disabled. "
            "The reset endpoint will return 503 for all reset requests. "
            "This is safe for initial Genie smoke testing but must be enabled before full "
            "durable state is activated."
        )
        return True, reasons, warnings

    # Enabled path: confirm the secret env var is present (do not read value)
    if "CONVERSATION_OWNER_HMAC_SECRET" not in environ:
        reasons.append(
            "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true but CONVERSATION_OWNER_HMAC_SECRET "
            "is absent from the environment. The app resource binding "
            "'conversation-owner-hmac-secret' must be configured."
        )
        return False, reasons, warnings

    # Presence confirmed; do not inspect, log, or return the value
    return True, reasons, warnings


def _check_durable_state(environ: Mapping[str, str]) -> tuple[bool, List[str], List[str]]:
    """Validate durable Genie session state readiness.

    Returns (ok, blocking_reasons, warnings).
    """
    reasons: List[str] = []
    warnings: List[str] = []

    durable_raw = environ.get("ENABLE_DURABLE_GENIE_SESSION_ADAPTER", "false")
    durable_enabled = _parse_bool_flag(durable_raw)

    if durable_enabled is None:
        reasons.append("ENABLE_DURABLE_GENIE_SESSION_ADAPTER has an unrecognised value.")
        return False, reasons, warnings

    if not durable_enabled:
        warnings.append(
            "ENABLE_DURABLE_GENIE_SESSION_ADAPTER=false: Genie session state is in-memory only. "
            "Sessions will be lost on app container cold-start. "
            "This is safe for initial smoke testing but must be enabled before production."
        )
        return True, reasons, warnings

    # Enabled path: cross-check dependencies
    trusted_raw = environ.get("ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY", "false")
    trusted_enabled = _parse_bool_flag(trusted_raw)
    if trusted_enabled is not True:
        reasons.append(
            "ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true requires "
            "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true."
        )

    lakebase_enabled_raw = environ.get("ENABLE_LAKEBASE_CONVERSATION_REPOSITORY", "false")
    lakebase_enabled = _parse_bool_flag(lakebase_enabled_raw)
    backend = environ.get("CONVERSATION_REPOSITORY_BACKEND", "memory").strip().lower()
    if backend != "lakebase" or lakebase_enabled is not True:
        warnings.append(
            "ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true but durable repository is not fully "
            "enabled (CONVERSATION_REPOSITORY_BACKEND=lakebase AND "
            "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY=true are both required for full persistence)."
        )

    ok = len(reasons) == 0
    return ok, reasons, warnings


def _check_genie_configuration(environ: Mapping[str, str]) -> tuple[bool, List[str], List[str]]:
    """Validate Genie backend configuration.

    Returns (ok, blocking_reasons, warnings).
    """
    reasons: List[str] = []
    warnings: List[str] = []

    use_genie_raw = environ.get("USE_GENIE_BACKEND", "false")
    use_genie = _parse_bool_flag(use_genie_raw)

    if use_genie is None:
        reasons.append("USE_GENIE_BACKEND has an unrecognised value.")
        return False, reasons, warnings

    if not use_genie:
        warnings.append(
            "USE_GENIE_BACKEND=false: the Genie backend is disabled. "
            "Set USE_GENIE_BACKEND=true before the Genie integration test deployment."
        )
        return True, reasons, warnings

    # Enabled path
    space_id = environ.get("GENIE_SPACE_ID", "").strip()
    if not space_id:
        reasons.append("USE_GENIE_BACKEND=true requires GENIE_SPACE_ID to be set.")

    # Validate timeout is positive
    timeout_str = environ.get("GENIE_RESPONSE_TIMEOUT_SECONDS", "120").strip()
    try:
        timeout = int(timeout_str)
        if timeout <= 0:
            reasons.append("GENIE_RESPONSE_TIMEOUT_SECONDS must be a positive integer.")
    except ValueError:
        reasons.append("GENIE_RESPONSE_TIMEOUT_SECONDS must be a positive integer.")

    ok = len(reasons) == 0
    return ok, reasons, warnings


def _check_lakebase_configuration(environ: Mapping[str, str]) -> tuple[bool, List[str], List[str]]:
    """Validate Lakebase configuration presence (no network I/O).

    Returns (ok, blocking_reasons, warnings).
    """
    reasons: List[str] = []
    warnings: List[str] = []

    durable_raw = environ.get("ENABLE_DURABLE_GENIE_SESSION_ADAPTER", "false")
    durable_enabled = _parse_bool_flag(durable_raw)
    lakebase_backend = environ.get("CONVERSATION_REPOSITORY_BACKEND", "memory").strip().lower()

    if durable_enabled is not True and lakebase_backend != "lakebase":
        # Lakebase not required in current config
        return True, reasons, warnings

    # When Lakebase is needed, all PG vars must be present
    required_pg_vars = ["PGHOST", "PGDATABASE", "PGPORT", "PGUSER", "PGSSLMODE", "LAKEBASE_ENDPOINT_NAME"]
    missing_pg = [v for v in required_pg_vars if not environ.get(v, "").strip()]
    if missing_pg:
        # Report count only — no partial values that could reveal host/credentials
        reasons.append(
            f"{len(missing_pg)} required Lakebase environment variable(s) are missing. "
            "These are injected by the 'postgres' Databricks Apps resource binding. "
            "Confirm the app resource binding is configured and the app has been restarted."
        )

    # SSL mode must be secure
    sslmode = environ.get("PGSSLMODE", "").strip().lower()
    insecure_modes = {"disable", "allow", "prefer"}
    if sslmode and sslmode in insecure_modes:
        reasons.append(
            "PGSSLMODE must not weaken TLS. Use 'require', 'verify-ca', or 'verify-full'."
        )

    ok = len(reasons) == 0
    return ok, reasons, warnings


def _check_resource_bindings(environ: Mapping[str, str]) -> tuple[bool, List[str], List[str]]:
    """Validate that required app resource bindings appear to be present.

    This is a structural check only — no network calls.
    Returns (ok, blocking_reasons, warnings).
    """
    reasons: List[str] = []
    warnings: List[str] = []

    # LAKEBASE_ENDPOINT_NAME via 'valueFrom: postgres' binding
    endpoint = environ.get("LAKEBASE_ENDPOINT_NAME", "").strip()
    durable_raw = environ.get("ENABLE_DURABLE_GENIE_SESSION_ADAPTER", "false")
    durable_enabled = _parse_bool_flag(durable_raw)

    if durable_enabled is True and not endpoint:
        reasons.append(
            "'postgres' resource binding appears absent: LAKEBASE_ENDPOINT_NAME is empty. "
            "Add the postgres resource in the Databricks App configuration before deployment."
        )

    # CONVERSATION_OWNER_HMAC_SECRET via 'valueFrom: conversation-owner-hmac-secret' binding
    trusted_raw = environ.get("ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY", "false")
    trusted_enabled = _parse_bool_flag(trusted_raw)
    hmac_present = "CONVERSATION_OWNER_HMAC_SECRET" in environ

    if trusted_enabled is True and not hmac_present:
        reasons.append(
            "'conversation-owner-hmac-secret' resource binding appears absent: "
            "CONVERSATION_OWNER_HMAC_SECRET is not in the environment. "
            "Add the secret resource binding before enabling trusted owner identity."
        )

    ok = len(reasons) == 0
    return ok, reasons, warnings


# ---------------------------------------------------------------------------
# Main readiness check
# ---------------------------------------------------------------------------


def check_production_readiness(
    environ: Optional[Mapping[str, str]] = None,
) -> ReadinessReport:
    """Perform a complete, non-destructive production readiness check.

    Args:
        environ: Environment variable mapping. Defaults to os.environ when None.
                 Pass a dict for testing or CI validation.

    Returns:
        ReadinessReport with all fields populated. No network I/O is performed.
        No secret values appear in the returned object.

    The check never raises; all errors are captured in blocking_reasons.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ

    all_blocking: List[str] = []
    all_warnings: List[str] = []

    # Run each domain check and collect results
    config_ok, config_reasons, config_warnings, items_checked = _check_configuration(env)
    all_blocking.extend(config_reasons)
    all_warnings.extend(config_warnings)

    flags_ok, flags_reasons, flags_warnings, flags_checked = _check_feature_flags(env)
    all_blocking.extend(flags_reasons)
    all_warnings.extend(flags_warnings)

    identity_ok, identity_reasons, identity_warnings = _check_trusted_identity(env)
    all_blocking.extend(identity_reasons)
    all_warnings.extend(identity_warnings)

    durable_ok, durable_reasons, durable_warnings = _check_durable_state(env)
    all_blocking.extend(durable_reasons)
    all_warnings.extend(durable_warnings)

    genie_ok, genie_reasons, genie_warnings = _check_genie_configuration(env)
    all_blocking.extend(genie_reasons)
    all_warnings.extend(genie_warnings)

    lakebase_ok, lakebase_reasons, lakebase_warnings = _check_lakebase_configuration(env)
    all_blocking.extend(lakebase_reasons)
    all_warnings.extend(lakebase_warnings)

    bindings_ok, bindings_reasons, bindings_warnings = _check_resource_bindings(env)
    all_blocking.extend(bindings_reasons)
    all_warnings.extend(bindings_warnings)

    overall = all([
        config_ok, flags_ok, identity_ok, durable_ok,
        genie_ok, lakebase_ok, bindings_ok,
    ])

    return ReadinessReport(
        configuration_ready=config_ok,
        feature_flags_ready=flags_ok,
        trusted_identity_ready=identity_ok,
        durable_state_ready=durable_ok,
        genie_configuration_ready=genie_ok,
        lakebase_configuration_ready=lakebase_ok,
        resource_bindings_ready=bindings_ok,
        overall_ready=overall,
        blocking_reasons=all_blocking,
        warnings=all_warnings,
        configuration_items_checked=items_checked,
        flags_checked=flags_checked,
    )


# ---------------------------------------------------------------------------
# Permission readiness check (snapshot-based, no live API calls)
# ---------------------------------------------------------------------------


def check_permission_snapshot(
    snapshot: Dict[str, str],
) -> PermissionReadinessReport:
    """Validate a permission snapshot and return a structured report.

    Args:
        snapshot: Dict mapping resource keys to their permission status strings.
                  Keys must match the _KNOWN_PERMISSIONS dict below.
                  Status strings: 'PRESENT_AND_SUFFICIENT', 'PRESENT_BUT_INSUFFICIENT',
                  'MISSING', 'CANNOT_VERIFY', 'NOT_REQUIRED'.

    Returns:
        PermissionReadinessReport. No network I/O.
    """
    # Define the full permission inventory with metadata
    _PERMISSION_META: List[dict] = [
        {
            "key": "genie_space_can_run",
            "resource": "Genie Space",
            "resource_id": "01f17a93e6aa1b97a9da7ef329e15e46",
            "principal": "app-31pcl9 transparence (SP 78664835752275)",
            "required_permission": "CAN_RUN",
            "blocking": True,
            "notes": "SP must be able to start and continue Genie conversations.",
        },
        {
            "key": "sql_warehouse_can_use",
            "resource": "SQL Warehouse",
            "resource_id": "8e46614f7064d8fd",
            "principal": "app-31pcl9 transparence (SP 78664835752275)",
            "required_permission": "CAN_USE",
            "blocking": True,
            "notes": "SP must be able to execute queries via Genie Space.",
        },
        {
            "key": "shipment_table_select",
            "resource": "Shipment Table",
            "resource_id": "onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard",
            "principal": "app-31pcl9 transparence (SP 78664835752275)",
            "required_permission": "SELECT",
            "blocking": True,
            "notes": "SP must be able to read shipment data for Genie queries.",
        },
        {
            "key": "lakebase_can_connect",
            "resource": "Lakebase Project",
            "resource_id": "projects/transparence-sessions/branches/production",
            "principal": "app-31pcl9 transparence (SP 78664835752275)",
            "required_permission": "CAN_CONNECT_AND_CREATE",
            "blocking": True,
            "notes": "SP must be able to connect via app resource binding 'postgres'.",
        },
        {
            "key": "lakebase_app_conversation_dml",
            "resource": "Lakebase app_conversation table",
            "resource_id": "transparence_state.app_conversation",
            "principal": "app-31pcl9 transparence (SP role)",
            "required_permission": "SELECT, INSERT, UPDATE, DELETE",
            "blocking": True,
            "notes": "SP PG role needs DML on app_conversation for durable session state.",
        },
        {
            "key": "secret_scope_hmac_read",
            "resource": "Databricks Secret",
            "resource_id": "transparence-owner-identity/conversation-owner-hmac-v1",
            "principal": "app-31pcl9 transparence (SP 78664835752275)",
            "required_permission": "READ",
            "blocking": True,
            "notes": "SP must be able to read HMAC secret via app resource binding.",
        },
        {
            "key": "genie_space_end_user_access",
            "resource": "Genie Space (end-user direct access)",
            "resource_id": "01f17a93e6aa1b97a9da7ef329e15e46",
            "principal": "Authenticated end users",
            "required_permission": "CAN_VIEW (optional — not used in SP-backend mode)",
            "blocking": False,
            "notes": "End users do not need direct Genie Space access; SP executes all Genie calls.",
        },
    ]

    entries: List[PermissionEntry] = []
    for meta in _PERMISSION_META:
        key = meta["key"]
        raw_status = snapshot.get(key, "CANNOT_VERIFY")
        try:
            status = PermissionStatus(raw_status)
        except ValueError:
            status = PermissionStatus.CANNOT_VERIFY

        entries.append(PermissionEntry(
            resource=meta["resource"],
            resource_id=meta["resource_id"],
            principal=meta["principal"],
            required_permission=meta["required_permission"],
            status=status,
            blocking=meta["blocking"],
            notes=meta["notes"],
        ))

    blocking = [
        e for e in entries
        if e.blocking and e.status not in (
            PermissionStatus.PRESENT_AND_SUFFICIENT,
            PermissionStatus.NOT_REQUIRED,
        )
    ]
    unverifiable = [
        e for e in entries
        if e.status == PermissionStatus.CANNOT_VERIFY
    ]
    overall = len(blocking) == 0

    return PermissionReadinessReport(
        entries=entries,
        overall_ready=overall,
        blocking_entries=blocking,
        unverifiable_entries=unverifiable,
    )


# ---------------------------------------------------------------------------
# Test-deployment safe flag combination
# ---------------------------------------------------------------------------

#: Safe feature-flag values for a controlled test deployment (Genie smoke test,
#: durable state disabled, trusted identity disabled).
TEST_DEPLOYMENT_FLAGS: Dict[str, str] = {
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
    "USE_NEW_ACCURACY_PIPELINE": "true",
    "NEW_PIPELINE_FALLBACK_TO_OLD": "true",
    "GENIE_EXPORT_MODE": "returned_rows_only",
}

#: Safe feature-flag values for a full production deployment (all durable state
#: and trusted identity enabled, no fallback to legacy pipeline after durable failure).
PRODUCTION_FLAGS: Dict[str, str] = {
    "USE_GENIE_BACKEND": "true",
    "GENIE_FALLBACK_TO_CUSTOM_PIPELINE": "false",
    "ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "true",
    "CONVERSATION_REPOSITORY_BACKEND": "lakebase",
    "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": "true",
    "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY": "true",
    "GENIE_DEBUG": "false",
    "NEW_PIPELINE_DEBUG": "false",
    "CONVERSATION_STATE_CLEANUP_HARD_DELETE": "false",
    "TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED": "false",
    "TRANSPARENCE_DIAGNOSTIC_LOG_SQL": "false",
    "USE_NEW_ACCURACY_PIPELINE": "true",
    "NEW_PIPELINE_FALLBACK_TO_OLD": "false",
    "GENIE_EXPORT_MODE": "returned_rows_only",
}


__all__ = [
    "ReadinessStatus",
    "ReadinessReport",
    "PermissionStatus",
    "PermissionEntry",
    "PermissionReadinessReport",
    "check_production_readiness",
    "check_permission_snapshot",
    "TEST_DEPLOYMENT_FLAGS",
    "PRODUCTION_FLAGS",
]
