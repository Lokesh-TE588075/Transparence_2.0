"""Schema registry for SQL validation.

Provides the canonical column list, data types, and table whitelist
used by the SQL validator to prevent hallucinated column references.

This is the single source of truth for what the LLM is allowed to reference.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set


# =============================================================================
# COLUMN TYPE ENUM
# =============================================================================


class ColumnType(Enum):
    """Data type categories for validation."""
    STRING = "string"
    NUMERIC = "numeric"
    TIMESTAMP = "timestamp"
    DATE = "date"
    BOOLEAN = "boolean"
    INTEGER = "integer"


# =============================================================================
# COLUMN METADATA
# =============================================================================


@dataclass(frozen=True)
class ColumnInfo:
    """Metadata for a single column."""
    name: str
    col_type: ColumnType
    nullable: bool = True
    excluded_from_prompt: bool = False


# =============================================================================
# SCHEMA REGISTRY
# =============================================================================

# The ONE allowed table (fully qualified)
ALLOWED_TABLES: FrozenSet[str] = frozenset({
    "onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard",
})

# Also allow unqualified and partially qualified references
ALLOWED_TABLE_ALIASES: FrozenSet[str] = frozenset({
    "lbn_with_scorecard",
    "ion_l0_raw.lbn_with_scorecard",
    "onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard",
})

# Canonical column definitions (42 columns)
COLUMNS: Dict[str, ColumnInfo] = {
    # Identifiers
    "shipment_number_id": ColumnInfo("shipment_number_id", ColumnType.STRING),
    "delivery_document_id": ColumnInfo("delivery_document_id", ColumnType.STRING),
    "customer_purchase_order_id": ColumnInfo("customer_purchase_order_id", ColumnType.STRING),
    "sales_order_number": ColumnInfo("sales_order_number", ColumnType.STRING),
    "business_unit_id": ColumnInfo("business_unit_id", ColumnType.STRING),
    "segment_name": ColumnInfo("segment_name", ColumnType.STRING),

    # Financial
    "shipment_quantity": ColumnInfo("shipment_quantity", ColumnType.NUMERIC),
    "sales_functional_currency_amount": ColumnInfo("sales_functional_currency_amount", ColumnType.NUMERIC),
    "sales_budget_rate_amount": ColumnInfo("sales_budget_rate_amount", ColumnType.NUMERIC),
    "currency_code": ColumnInfo("currency_code", ColumnType.STRING),

    # Transport
    "special_process_indicator": ColumnInfo("special_process_indicator", ColumnType.STRING),
    "transportation_mode_desc": ColumnInfo("transportation_mode_desc", ColumnType.STRING),
    "intercompany_desc": ColumnInfo("intercompany_desc", ColumnType.STRING),
    "waybill": ColumnInfo("waybill", ColumnType.STRING),
    "hbl": ColumnInfo("hbl", ColumnType.STRING),

    # Parties
    "shipping_point": ColumnInfo("shipping_point", ColumnType.STRING),
    "ship_to_party": ColumnInfo("ship_to_party", ColumnType.STRING),
    "ffw": ColumnInfo("ffw", ColumnType.STRING),

    # Geography
    "source_": ColumnInfo("source_", ColumnType.STRING),
    "destination": ColumnInfo("destination", ColumnType.STRING),

    # Status
    "execution_status": ColumnInfo("execution_status", ColumnType.STRING),

    # Timestamps
    "actual_pgi_date": ColumnInfo("actual_pgi_date", ColumnType.TIMESTAMP),
    "actual_status_seven": ColumnInfo("actual_status_seven", ColumnType.TIMESTAMP),
    "actual_delivery_at": ColumnInfo("actual_delivery_at", ColumnType.TIMESTAMP),
    "actual_delivery_at_port": ColumnInfo("actual_delivery_at_port", ColumnType.TIMESTAMP),
    "actual_departure_at": ColumnInfo("actual_departure_at", ColumnType.TIMESTAMP, excluded_from_prompt=True),
    "actual_departure_at_port": ColumnInfo("actual_departure_at_port", ColumnType.TIMESTAMP),
    "estimated_arrival_at": ColumnInfo("estimated_arrival_at", ColumnType.TIMESTAMP),
    "actual_gr_date": ColumnInfo("actual_gr_date", ColumnType.TIMESTAMP),

    # Dates
    "eta": ColumnInfo("eta", ColumnType.DATE),
    "etd": ColumnInfo("etd", ColumnType.DATE),
    "ata": ColumnInfo("ata", ColumnType.DATE),

    # System dates
    "load_date": ColumnInfo("load_date", ColumnType.TIMESTAMP),
    "final_gr_date": ColumnInfo("final_gr_date", ColumnType.TIMESTAMP),

    # Weight
    "actual_weight": ColumnInfo("actual_weight", ColumnType.NUMERIC),
    "chargeable_weight": ColumnInfo("chargeable_weight", ColumnType.NUMERIC),
    "unit_of_measure": ColumnInfo("unit_of_measure", ColumnType.STRING),

    # Product
    "part_number": ColumnInfo("part_number", ColumnType.STRING),
    "ffw_shipping_type": ColumnInfo("ffw_shipping_type", ColumnType.STRING),

    # Derived columns (computed during ingestion)
    "is_completed": ColumnInfo("is_completed", ColumnType.BOOLEAN),
    "delay_days_live": ColumnInfo("delay_days_live", ColumnType.INTEGER),
    "delay_days_historical": ColumnInfo("delay_days_historical", ColumnType.INTEGER),
}

# Pre-computed sets for fast lookups
VALID_COLUMN_NAMES: FrozenSet[str] = frozenset(COLUMNS.keys())

NUMERIC_COLUMNS: FrozenSet[str] = frozenset(
    name for name, info in COLUMNS.items()
    if info.col_type in (ColumnType.NUMERIC, ColumnType.INTEGER)
)

DATE_COLUMNS: FrozenSet[str] = frozenset(
    name for name, info in COLUMNS.items()
    if info.col_type in (ColumnType.DATE, ColumnType.TIMESTAMP)
)

STRING_COLUMNS: FrozenSet[str] = frozenset(
    name for name, info in COLUMNS.items()
    if info.col_type == ColumnType.STRING
)


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def is_valid_column(name: str) -> bool:
    """Check if a column name exists in the schema."""
    return name.lower().strip("`").strip('"') in VALID_COLUMN_NAMES


def get_column_type(name: str) -> Optional[ColumnType]:
    """Get the type of a column, or None if not found."""
    clean = name.lower().strip("`").strip('"')
    info = COLUMNS.get(clean)
    return info.col_type if info else None


def is_valid_table(table_ref: str) -> bool:
    """Check if a table reference is allowed."""
    clean = table_ref.lower().strip("`").strip('"').strip()
    return clean in ALLOWED_TABLE_ALIASES


def get_all_column_names() -> List[str]:
    """Get all valid column names as a sorted list."""
    return sorted(VALID_COLUMN_NAMES)
