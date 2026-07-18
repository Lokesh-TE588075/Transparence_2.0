"""Column dictionary - descriptions and metadata for all shipment table columns.

Used by the prompt builder to inject column context into the LLM system prompt.
Excludes actual_departure_at from the prompt (100% null in dataset).
"""

from typing import Dict


# =============================================================================
# COLUMN DESCRIPTIONS (for LLM system prompt)
# These are injected into the prompt to help the LLM understand column semantics.
# =============================================================================

COLUMN_DESCRIPTIONS: Dict[str, str] = {
    "shipment_number_id": "Unique identifier for the shipment (numeric or alphanumeric).",
    "delivery_document_id": "Delivery document number associated with the shipment.",
    "customer_purchase_order_id": "Customer purchase order number linked to this shipment.",
    "sales_order_number": "Sales order number that initiated this shipment.",
    "business_unit_id": "Identifier for the business unit handling the shipment (e.g., AUT, HES, SYS).",
    "segment_name": "Name of the business segment responsible for the shipment.",
    "shipment_quantity": "Total quantity of items or units shipped (numeric).",
    "sales_functional_currency_amount": (
        "Actual sales amount in the functional currency (numeric). "
        "THIS IS THE DEFAULT REVENUE COLUMN. Use this unless user explicitly asks for budget-rate."
    ),
    "sales_budget_rate_amount": (
        "Budgeted sales amount for the shipment (numeric). "
        "Use ONLY when user explicitly asks for 'budget-rate revenue', "
        "'budget converted amount', 'budget-rate sales', or 'sales using budget rate'."
    ),
    "currency_code": "Currency code for the sales amount (e.g., USD, EUR).",
    "special_process_indicator": "Indicator for special handling or process requirements.",
    "transportation_mode_desc": "Transportation mode (e.g., 'Air transport', 'Ocean Transport').",
    "intercompany_desc": "Indicator if the shipment is intercompany or external.",
    "waybill": "Waybill number for shipment tracking.",
    "hbl": "House Bill of Lading number.",
    "shipping_point": "Origin shipping point or facility code.",
    "ship_to_party": "Identifier for the recipient or destination customer.",
    "ffw": "Freight forwarder involved in the shipment.",
    "source_": "Origin location of the shipment (ISO country code).",
    "destination": "Destination location of the shipment (ISO country code).",
    "execution_status": (
        "Current status of the shipment. "
        "'Delivered' or 'Completed' mean shipment is finished; "
        "other values like 'In Transit', 'Goods Issued', 'Status 7', "
        "'Pick Up To Port', 'At Destination Port', 'Port To Delivery' indicate still in progress."
    ),
    "actual_pgi_date": "Actual Post Goods Issue date (TIMESTAMP). Marks the start of shipment initiation.",
    "actual_status_seven": "Timestamp for a key process milestone (status seven).",
    "actual_delivery_at": "Actual delivery date/time at the final destination.",
    "actual_delivery_at_port": (
        "Actual arrival date/time at the destination port. "
        "NOT for delay calculations - use the 'ata' column instead."
    ),
    # NOTE: actual_departure_at excluded from prompt (100% NULL in dataset)
    "actual_departure_at_port": "Actual departure date/time from the origin port.",
    "estimated_arrival_at": "Estimated arrival date/time at the final destination.",
    "actual_gr_date": "Actual Goods Receipt date (TIMESTAMP).",
    "eta": (
        "Updated Estimated Time of Arrival (DATE). "
        "Used for ETA and delay analyses. "
        "Live delay = GREATEST(DATEDIFF(CURRENT_DATE(), DATE(eta)), 0)."
    ),
    "etd": "Estimated Time of Departure (DATE).",
    "ata": (
        "Actual Time of Arrival at destination port from the freight forwarder (DATE). "
        "Used for historical delay calculation: GREATEST(DATEDIFF(DATE(ata), DATE(eta)), 0)."
    ),
    "load_date": "Date when the shipment was loaded.",
    "final_gr_date": "Final Goods Receipt date (TIMESTAMP). Marks the completion/delivery of the shipment.",
    "actual_weight": (
        "Net/physical weight of the shipment (numeric). "
        "Use ONLY when user explicitly asks for 'actual weight' or 'net weight'."
    ),
    "chargeable_weight": (
        "Total (gross) shipment weight including packaging (numeric). "
        "THIS IS THE DEFAULT WEIGHT COLUMN. Always use for weight-related queries unless user specifies otherwise."
    ),
    "unit_of_measure": "Unit of measure for weights (e.g., KG, LB).",
    "part_number": "Part/Product ID for the item(s) in the shipment.",
    "ffw_shipping_type": "Freight forwarder shipping type classification.",
    # Derived columns (added during Phase 1 ingestion)
    "is_completed": "Derived: TRUE if execution_status IN ('Delivered', 'Completed').",
    "delay_days_live": (
        "Derived: GREATEST(DATEDIFF(CURRENT_DATE(), DATE(eta)), 0) for active shipments. "
        "NULL for completed shipments."
    ),
    "delay_days_historical": (
        "Derived: GREATEST(DATEDIFF(DATE(ata), DATE(eta)), 0) for completed shipments. "
        "NULL for active shipments."
    ),
}

# Columns to EXCLUDE from the LLM prompt (useless or misleading)
COLUMNS_EXCLUDED_FROM_PROMPT = [
    "actual_departure_at",  # 100% NULL in dataset
]

# Default columns for broad/large queries (performance optimization)
BROAD_QUERY_COLUMNS = [
    "shipment_number_id",
    "part_number",
    "source_",
    "destination",
    "transportation_mode_desc",
    "execution_status",
    "actual_pgi_date",
    "eta",
    "ata",
    "final_gr_date",
    "sales_functional_currency_amount",
    "currency_code",
]
