# TransparencE Shipment Chatbot

Enterprise-grade natural language interface for shipment tracking intelligence. Built on Databricks Apps with Unity Catalog Delta tables and Databricks Model Serving.

## Architecture

```
transparence_app/
├── app.yaml                    # Databricks Apps deployment manifest
├── requirements.txt            # Python dependencies
├── app/
│   ├── main.py                 # FastAPI entry point, CORS, lifespan
│   ├── config.py               # pydantic-settings (all config via env vars)
│   ├── routes/                 # API route handlers
│   │   ├── chat.py             # POST /api/chat — main NL pipeline
│   │   ├── feedback.py         # POST /api/feedback
│   │   ├── export.py           # GET /api/download/{key}
│   │   └── health.py           # GET /api/health, /api/ping
│   ├── services/               # Business logic layer
│   │   ├── llm_service.py      # LLM abstraction (Model Serving)
│   │   ├── sql_service.py      # SQL execution (Databricks SQL Connector)
│   │   ├── conversation_manager.py  # Delta-backed sessions
│   │   └── audit_service.py    # Query audit logging
│   ├── guardrails/             # SQL safety layer
│   │   ├── sql_validator.py    # Injection prevention, schema validation
│   │   └── schema_registry.py  # Column metadata source of truth
│   ├── business_rules/         # Domain knowledge
│   │   ├── mappings.py         # BU codes, country ISO, keyword synonyms
│   │   ├── column_dict.py      # Column descriptions
│   │   └── prompt_builder.py   # System prompt assembly
│   ├── models/
│   │   └── schemas.py          # Pydantic request/response models
│   └── utils/
│       └── logging.py          # Structured logging setup
├── frontend/                   # React + Vite (professional chatbot UI)
└── tests/                      # Test suite
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABRICKS_SQL_WAREHOUSE_PATH` | Yes | HTTP path to serverless SQL warehouse |
| `LLM_ENDPOINT_PRIMARY` | Yes | Model Serving endpoint for SQL generation |
| `LLM_ENDPOINT_FAST` | Yes | Model Serving endpoint for intent classification |
| `SHIPMENT_TABLE_NAME` | No | Default: `onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard` |
| `EXPORT_VOLUME_PATH` | No | UC Volume path for CSV exports |

## Local Development

```bash
cd transparence_app
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Deployment

Deployed as a Databricks App. Configuration in `app.yaml`.

## Key Design Decisions

1. **No hardcoded LLM endpoints** — all via environment variables
2. **Databricks SQL syntax** — `DATEDIFF(endDate, startDate)`, not Redshift-style
3. **Liquid clustering** on shipment data (shipment_number_id, business_unit_id, execution_status, actual_pgi_date)
4. **Delta-backed sessions** — conversations and messages persisted in UC tables
5. **Targeted SQL injection prevention** — block `--`, `/* */`, multi-statement, info_schema; not blanket blocking
