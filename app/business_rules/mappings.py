"""Business unit codes, country mappings, keyword-column synonyms, and status groups.

Extracted from legacy main.py (lines 210-486). Preserved as standalone
reference data for the LLM prompt builder and query preprocessing.
"""

import re
from typing import Dict, List, Optional

from rapidfuzz import fuzz, process


# =============================================================================
# EXECUTION STATUS GROUPS
# =============================================================================

STATUS_COMPLETED: List[str] = ["Delivered", "Completed"]

STATUS_ACTIVE: List[str] = [
    "In Transit",
    "In-Transit",
    "Goods Issued",
    "Status 7",
    "Pick Up To Port",
    "At Destination Port",
    "Port To Delivery",
]


# =============================================================================
# BUSINESS UNIT NAME -> CODE (37 entries)
# =============================================================================

BU_NAME_TO_CODE: Dict[str, str] = {
    "Automotive Group Management": "AGM",
    "Automotive": "AUT",
    "E-Mobility": "HES",
    "Application Tooling": "ASG",
    "Industrial Commercial Transportation": "CIV",
    "Kissling": "KSL",
    "Sensors Center Led": "CTR",
    "Industrial & Medical Sensors": "IMS",
    "Selective Catalytic Reduction": "SCR",
    "Transportation Sensors": "TRS",
    "Transportation Group Management": "TSG",
    "Automation & Connected Living Sector": "ACL",
    "Appliances": "APL",
    "Precision Power": "PPS",
    "IND Board Connectivity & Sub Systems": "BCS",
    "IND Filters": "FLT",
    "Industrial Management": "IND",
    "Relay Products - Industrial": "RLY",
    "IND Systems": "SYS",
    "Precision Technology Solutions": "PTS",
    "Aerospace and Defense": "AND",
    "Deutsch Offshore": "DOS",
    "DRI Relays": "DRI",
    "Marine, Oil and Gas": "MOG",
    "Digital Data Networks": "DND",
    "Linx Antennas": "LNX",
    "Laird Antennas": "LRD",
    "Energy": "ENG",
    "Industrial Solutions Group": "ISG",
    "Medical - IVD": "IVD",
    "Medical - Interconnect": "MCT",
    "Medical - Devices": "MDV",
    "Medical Headquarters": "MHQ",
    "Medical - Metals": "MMT",
    "Corporate": "CRP",
    "Legal": "LGL",
    "Private Brand Labeling": "PBL",
}


# =============================================================================
# COUNTRY NAME -> ISO CODE (50+ entries with common misspellings)
# =============================================================================

COUNTRY_NAME_TO_ISO: Dict[str, str] = {
    "United States": "US", "USA": "US", "U.S.A.": "US", "US": "US",
    "America": "US", "U.S.": "US", "United States of America": "US",
    "Unites States": "US", "Amerika": "US",
    "Canada": "CA", "CA": "CA", "Can": "CA",
    "Mexico": "MX", "MX": "MX",
    "Brazil": "BR", "Brasil": "BR", "Br": "BR",
    "Argentina": "AR", "Argentine": "AR", "Argentinian": "AR",
    "United Kingdom": "GB", "UK": "GB", "U.K.": "GB", "Britain": "GB",
    "Great Britain": "GB", "England": "GB", "Scotland": "GB",
    "Wales": "GB", "N. Ireland": "GB", "Britian": "GB",
    "Germany": "DE", "Deutschland": "DE", "GR": "DE",
    "Germnay": "DE", "Ger": "DE",
    "France": "FR", "French Republic": "FR", "Fr": "FR", "Franc": "FR",
    "Italy": "IT", "Italia": "IT",
    "Spain": "ES", "Espana": "ES", "ES": "ES",
    "Netherlands": "NL", "Holland": "NL", "Dutch": "NL", "NL": "NL",
    "Belgium": "BE", "Bel": "BE",
    "Sweden": "SE", "SE": "SE", "Sverige": "SE",
    "Denmark": "DK", "Danmark": "DK", "DK": "DK",
    "Finland": "FI", "Suomi": "FI", "FI": "FI",
    "Switzerland": "CH", "Swiss": "CH", "CH": "CH",
    "Austria": "AT", "AT": "AT",
    "Poland": "PL", "Polska": "PL", "PL": "PL",
    "Czechia": "CZ", "Czech Republic": "CZ", "Czech": "CZ", "CZ": "CZ",
    "Hungary": "HU", "HU": "HU",
    "Russia": "RU", "Russian Federation": "RU", "RU": "RU",
    "Rossiya": "RU", "Russsia": "RU",
    "Turkey": "TR", "Turkiye": "TR", "TR": "TR",
    "China": "CN", "PRC": "CN", "People's Republic of China": "CN",
    "CHN": "CN", "Mainland China": "CN", "Chine": "CN",
    "Japan": "JP", "Nippon": "JP", "JP": "JP",
    "South Korea": "KR", "Korea, South": "KR",
    "Republic of Korea": "KR", "SK": "KR", "S. Korea": "KR",
    "Taiwan": "TW", "TW": "TW", "Republic of China": "TW",
    "India": "IN", "Bharat": "IN", "IN": "IN", "Hindustan": "IN",
    "Singapore": "SG", "Singapor": "SG", "S'pore": "SG", "SG": "SG",
    "Malaysia": "MY", "MY": "MY",
    "Indonesia": "ID", "ID": "ID",
    "Thailand": "TH", "TH": "TH",
    "Australia": "AU", "Aussie": "AU", "Down Under": "AU", "AUS": "AU",
    "New Zealand": "NZ", "NZ": "NZ",
    "United Arab Emirates": "AE", "UAE": "AE", "Emirates": "AE",
    "Saudi Arabia": "SA", "KSA": "SA", "Kingdom of Saudi Arabia": "SA",
    "South Africa": "ZA", "RSA": "ZA", "S. Africa": "ZA",
    "Israel": "IL", "IL": "IL",
}


# =============================================================================
# KEYWORD -> COLUMN MAPPING (100+ synonyms)
# =============================================================================

KEYWORD_COLUMN_MAPPING: Dict[str, List[str]] = {
    "shipment number": ["shipment_number_id"],
    "shipment id": ["shipment_number_id"],
    "tracking number": ["shipment_number_id"],
    "delivery document": ["delivery_document_id"],
    "delivery id": ["delivery_document_id"],
    "delivery note": ["delivery_document_id"],
    "purchase order": ["customer_purchase_order_id"],
    "customer order": ["customer_purchase_order_id"],
    "po number": ["customer_purchase_order_id"],
    "customer po": ["customer_purchase_order_id"],
    "purchase order number": ["customer_purchase_order_id"],
    "sales order": ["sales_order_number"],
    "order number": ["sales_order_number"],
    "sales order number": ["sales_order_number"],
    "business unit": ["business_unit_id"],
    "bu": ["business_unit_id"],
    "BU": ["business_unit_id"],
    "business segment": ["segment_name"],
    "segment": ["business_unit_id"],
    "unit name": ["business_unit_id"],
    "quantity": ["shipment_quantity"],
    "quantity shipped": ["shipment_quantity"],
    "number of items": ["shipment_quantity"],
    "sales amount": ["sales_functional_currency_amount"],
    "sales revenue": ["sales_functional_currency_amount"],
    "revenue": ["sales_functional_currency_amount"],
    "amount sold": ["sales_functional_currency_amount"],
    "budget amount": ["sales_budget_rate_amount"],
    "budgeted sales": ["sales_budget_rate_amount"],
    "planned sales": ["sales_budget_rate_amount"],
    "sales budget": ["sales_budget_rate_amount"],
    "budget-rate revenue": ["sales_budget_rate_amount"],
    "budget converted amount": ["sales_budget_rate_amount"],
    "budget-rate sales": ["sales_budget_rate_amount"],
    "sales using budget rate": ["sales_budget_rate_amount"],
    "currency": ["currency_code"],
    "currency code": ["currency_code"],
    "money type": ["currency_code"],
    "special process": ["special_process_indicator"],
    "special handling": ["special_process_indicator"],
    "priority shipment": ["special_process_indicator"],
    "expedited": ["special_process_indicator"],
    "transportation mode": ["transportation_mode_desc"],
    "transport mode": ["transportation_mode_desc"],
    "mode of transport": ["transportation_mode_desc"],
    "shipping mode": ["transportation_mode_desc"],
    "transport method": ["transportation_mode_desc"],
    "transport type": ["transportation_mode_desc"],
    "intercompany": ["intercompany_desc"],
    "internal shipment": ["intercompany_desc"],
    "intercompany shipment": ["intercompany_desc"],
    "intra company": ["intercompany_desc"],
    "waybill": ["waybill"],
    "air waybill": ["waybill"],
    "bill of lading": ["hbl"],
    "bol": ["hbl"],
    "house bill": ["hbl"],
    "hbl": ["hbl"],
    "shipping point": ["shipping_point"],
    "origin facility": ["shipping_point"],
    "ship from": ["shipping_point"],
    "dispatch location": ["shipping_point"],
    "ship to": ["ship_to_party"],
    "recipient": ["ship_to_party"],
    "delivery location": ["ship_to_party"],
    "destination party": ["ship_to_party"],
    "freight forwarder": ["ffw"],
    "carrier": ["ffw"],
    "logistics provider": ["ffw"],
    "ffw": ["ffw"],
    "origin": ["source_"],
    "from location": ["source_"],
    "shipping origin": ["source_"],
    "destination": ["destination"],
    "ship to location": ["destination"],
    "arrival location": ["destination"],
    "status": ["execution_status"],
    "shipment status": ["execution_status"],
    "order status": ["execution_status"],
    "delivery status": ["execution_status"],
    "current status": ["execution_status"],
    "pgi date": ["actual_pgi_date"],
    "post goods issue": ["actual_pgi_date"],
    "dispatch date": ["actual_pgi_date"],
    "status milestone": ["actual_status_seven"],
    "milestone date": ["actual_status_seven"],
    "delivery date": ["actual_delivery_at"],
    "date delivered": ["actual_delivery_at"],
    "actual delivery": ["actual_delivery_at"],
    "delivery at port": ["actual_delivery_at_port"],
    "port arrival": ["actual_delivery_at_port"],
    "arrival at port": ["actual_delivery_at_port"],
    "departure date": ["actual_departure_at_port"],
    "date of departure": ["actual_departure_at_port"],
    "left origin": ["actual_departure_at_port"],
    "departure at port": ["actual_departure_at_port"],
    "port departure": ["actual_departure_at_port"],
    "estimated arrival": ["estimated_arrival_at"],
    "expected delivery": ["estimated_arrival_at"],
    "estimated arrival date": ["estimated_arrival_at"],
    "eta": ["eta"],
    "estimated time of arrival": ["eta"],
    "goods receipt": ["final_gr_date"],
    "gr date": ["final_gr_date"],
    "receipt date": ["final_gr_date"],
    "etd": ["etd"],
    "estimated time of departure": ["etd"],
    "departure estimate": ["etd"],
    "all the information": ["shipment_number_id"],
    "complete details": ["shipment_number_id"],
    "provide complete details": ["shipment_number_id"],
    "get all information": ["shipment_number_id"],
    "weight": ["chargeable_weight", "actual_weight"],
    "cargo weight": ["chargeable_weight"],
    "net weight": ["actual_weight"],
    "gross weight": ["chargeable_weight"],
    "total weight": ["chargeable_weight"],
    "shipment weight": ["chargeable_weight"],
    "uom": ["unit_of_measure"],
    "unit of measure": ["unit_of_measure"],
    "measurement unit": ["unit_of_measure"],
    "unit": ["unit_of_measure"],
    "kg": ["unit_of_measure"],
    "kgs": ["unit_of_measure"],
    "kilogram": ["unit_of_measure"],
    "kilograms": ["unit_of_measure"],
    "lb": ["unit_of_measure"],
    "lbs": ["unit_of_measure"],
    "pound": ["unit_of_measure"],
    "pounds": ["unit_of_measure"],
    "part number": ["part_number"],
    "part id": ["part_number"],
    "product id": ["part_number"],
    "product number": ["part_number"],
    "material number": ["part_number"],
    "ata": ["ata"],
    "actual time of arrival": ["ata"],
    "arrival time at port": ["ata"],
    "port ata": ["ata"],
}


# =============================================================================
# DETAILED REQUEST PATTERNS (regex for structured lookup)
# =============================================================================

DETAILED_REQUEST_PATTERNS = [
    re.compile(r'give me more details about (?:shipment number|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'give me the details for (?:shipment number|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'provide details of (?:shipment number|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'show me the details for (?:shipment number|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'can you give me more details about (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'can you provide details for (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'details about (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'give me details for (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'provide information for (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'show all information for (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'retrieve all details for (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'full details of (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'can you provide all the information for (?:shipment number|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'i need the complete details of (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'show me all the details for (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'provide all information for (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'complete details for (?:shipment number|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'get all information about (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
    re.compile(r'retrieve complete details of (?:shipment|shipment id)\s+(\w+)', re.IGNORECASE),
]

# Fields to search when user provides a numeric identifier
NUMERIC_ID_SEARCH_FIELDS: List[str] = [
    "shipment_number_id",
    "waybill",
    "delivery_document_id",
    "customer_purchase_order_id",
    "sales_order_number",
    "hbl",
]

# Fallback message when query is not shipment-related
CUSTOM_FALLBACK_MESSAGE: str = (
    "Hello! I'm currently optimized for shipment-related queries only. "
    "Please try to be as specific as possible with your prompt for accurate results."
)


# =============================================================================
# QUERY INDICATOR KEYWORDS (used by fast greeting classifier)
# =============================================================================
# If ANY of these words/fragments appear in the user message, it is NOT a pure
# greeting — it contains a real query and must go through the full pipeline.

QUERY_INDICATOR_KEYWORDS: frozenset = frozenset({
    # Action verbs signaling a data request
    "show", "give", "get", "find", "list", "fetch", "pull", "display",
    "tell", "provide", "retrieve", "search", "look", "check", "query",
    "count", "total", "sum", "average", "compare", "analyze", "export",
    # Shipment domain terms
    "shipment", "shipments", "delivery", "deliveries", "tracking",
    "consignment", "cargo", "freight", "package", "parcel", "order",
    "orders", "waybill", "bol", "hbl",
    # Location / route
    "from", "to", "origin", "destination", "source", "route", "lane",
    "country", "port",
    # Time / dates
    "today", "yesterday", "week", "month", "year", "date", "since",
    "between", "last", "recent", "latest", "past", "jan", "feb", "mar",
    "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december", "quarter", "q1",
    "q2", "q3", "q4",
    # Status / logistics
    "delay", "delayed", "late", "overdue", "transit", "in-transit",
    "delivered", "completed", "active", "pending", "status",
    # Metrics / measures
    "revenue", "weight", "cost", "amount", "sales", "volume",
    # Business entities
    "bu", "business unit", "carrier", "forwarder", "ffw",
    # Identifiers (if user mentions a number, it's a query)
    "number", "id", "#",
    # Question words that signal a data query
    "how many", "how much", "which", "what", "where", "when",
})


# =============================================================================
# TRANSPORT MODE SYNONYMS (for input normalizer - Phase 2)
# =============================================================================

TRANSPORT_MODE_SYNONYMS: Dict[str, str] = {
    "air": "Air transport",
    "via air": "Air transport",
    "by air": "Air transport",
    "air shipment": "Air transport",
    "air shipments": "Air transport",
    "air transport": "Air transport",
    "flight": "Air transport",
    "ocean": "Ocean Transport",
    "via ocean": "Ocean Transport",
    "by ocean": "Ocean Transport",
    "sea": "Ocean Transport",
    "by sea": "Ocean Transport",
    "ocean shipment": "Ocean Transport",
    "ocean shipments": "Ocean Transport",
    "ocean transport": "Ocean Transport",
    "maritime": "Ocean Transport",
    "road": "Road Transport",
    "truck": "Road Transport",
    "trucking": "Road Transport",
    "by road": "Road Transport",
    "via road": "Road Transport",
    "rail": "Rail Transport",
    "train": "Rail Transport",
    "by rail": "Rail Transport",
    "via rail": "Rail Transport",
    "courier": "Courier",
    "express": "Courier",
    "parcel": "Courier",
}


# =============================================================================
# STATUS CATEGORY SYNONYMS (for input normalizer - Phase 2)
# =============================================================================

STATUS_CATEGORY_SYNONYMS: Dict[str, List[str]] = {
    "in_transit": [
        "in transit", "in-transit", "active", "not delivered",
        "not completed", "pending", "en route", "on the way",
        "in progress", "ongoing",
    ],
    "completed": [
        "delivered", "completed", "received", "arrived",
        "done", "finished",
    ],
    "delayed": [
        "delayed", "late", "overdue", "behind schedule",
        "past due", "missed eta",
    ],
}


# =============================================================================
# META-INTENT PATTERNS (for input normalizer - Phase 2)
# =============================================================================

META_INTENT_PATTERNS: Dict[str, List[str]] = {
    "summarize": [
        "quick summary", "summarize", "summary of", "summarise",
        "brief overview", "recap", "sum up", "give me a summary",
    ],
    "download": [
        "download", "export", "csv", "excel", "save to file",
        "get the file", "export this",
    ],
    "broaden": [
        "broader range", "expand range", "increase date",
        "wider period", "broader date", "expand the period",
        "broaden", "extend the range", "larger window",
    ],
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def convert_country_to_iso(text: str) -> str:
    """Replace country names/abbreviations/typos with ISO codes using exact + fuzzy match."""
    if not text:
        return text
    for country_name, iso_code in COUNTRY_NAME_TO_ISO.items():
        pattern = rf"(?<!\w){re.escape(country_name)}(?!\w)"
        text = re.sub(pattern, iso_code, text, flags=re.IGNORECASE)
    tokens = set(re.findall(r"\b[\w\s\-\']{3,}\b", text))
    tokens = sorted(tokens, key=len, reverse=True)
    for token in tokens:
        if token.upper() in COUNTRY_NAME_TO_ISO.values():
            continue
        match = process.extractOne(
            token, COUNTRY_NAME_TO_ISO.keys(), scorer=fuzz.token_sort_ratio
        )
        if match and match[1] >= 75:
            iso_code = COUNTRY_NAME_TO_ISO[match[0]]
            pattern = rf"(?<!\w){re.escape(token)}(?!\w)"
            text = re.sub(pattern, iso_code, text, flags=re.IGNORECASE)
    return text


def convert_bu_to_code(text: str) -> str:
    """Replace business unit names with standard codes using exact + fuzzy match."""
    if not text:
        return text
    for bu_name, code in BU_NAME_TO_CODE.items():
        text = re.sub(rf"\b{re.escape(bu_name)}\b", code, text, flags=re.IGNORECASE)
    tokens = re.findall(r"\b\w+\b", text)
    for tok in tokens:
        match = process.extractOne(tok, BU_NAME_TO_CODE.keys(), scorer=fuzz.ratio)
        if match and match[1] >= 80:
            text = re.sub(
                rf"\b{re.escape(tok)}\b",
                BU_NAME_TO_CODE[match[0]],
                text,
                flags=re.IGNORECASE,
            )
    return text


def match_detailed_request(user_message: str) -> Optional[str]:
    """Check if message matches 'show all details for shipment X' pattern.

    Returns the shipment ID if matched, None otherwise.
    """
    for pattern in DETAILED_REQUEST_PATTERNS:
        m = pattern.search(user_message)
        if m:
            return m.group(1)
    return None


def has_query_intent(message: str) -> bool:
    """Check whether a message contains any query-indicating keywords.

    Used by the fast greeting classifier to avoid short-circuiting
    messages like 'hi can you give me shipments from Mexico?'.
    """
    lower = message.lower()
    words = set(re.findall(r"\b\w+\b", lower))
    # Check single-word keywords
    if words & QUERY_INDICATOR_KEYWORDS:
        return True
    # Check multi-word phrases
    for phrase in ("how many", "how much", "business unit"):
        if phrase in lower:
            return True
    # Check if message contains a number (likely a shipment ID or quantity)
    if re.search(r"\d{3,}", message):
        return True
    return False
