# Test Baseline

## Phase 0 — TransparencE Statefix Refactoring

Recorded: 2026-07-18

---

## Targeted Test Suite

Command:
```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. pytest -q \
  tests/test_pre_genie_router.py \
  tests/test_genie_prompt_enricher.py \
  tests/test_genie_pipeline.py \
  tests/test_genie_backend_feature_flag.py \
  tests/test_genie_session_store.py \
  tests/test_genie_session_store_context.py \
  tests/test_genie_response_mapper.py \
  tests/test_genie_table_summarizer.py \
  tests/test_shape_validator_p1_regression.py \
  tests/test_export_job_manager.py \
  tests/test_e5_enterprise_contracts.py \
  tests/test_multi_user_session_isolation.py
```

| Metric | Result |
|---|---|
| Files specified | 12 |
| Files present | 12 / 12 |
| Files missing | 0 |
| Tests collected | 643 |
| Passed | 643 |
| Failed | 0 |
| Errors | 0 |
| Duration | 6.00s |

## Complete Non-Live Test Suite

Command:
```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. pytest -q \
  --ignore=tests/test_genie_live_smoke.py \
  --ignore=tests/test_genie_integration_smoke.py \
  --ignore=tests/test_delta_state_live_smoke.py \
  --ignore=tests/test_new_pipeline_live_smoke.py \
  tests/
```

| Metric | Result |
|---|---|
| Total test files | 31 |
| Non-live test files run | 27 |
| Live suites excluded | 4 |
| Tests collected | 911 |
| Passed | 911 |
| Failed | 0 |
| Skipped | 0 |
| Warnings | 1 (Pydantic V2 deprecation — pre-existing, benign) |
| Duration | 6.38s |
| Matches previous verified baseline | YES (911/911) |

## Excluded Live Suites

| File | Reason for exclusion |
|---|---|
| tests/test_genie_live_smoke.py | Requires live Databricks infrastructure (Genie API, SQL warehouse) |
| tests/test_genie_integration_smoke.py | Requires live Databricks infrastructure (end-to-end Genie pipeline) |
| tests/test_delta_state_live_smoke.py | Requires live Delta table write access |
| tests/test_new_pipeline_live_smoke.py | Requires live pipeline execution environment |

All 4 live suite files are present in the development copy but are excluded from routine runs.
They are NOT claimed to pass. They require live infrastructure credentials.

## Notes

- `rapidfuzz` is not pre-installed in the Databricks serverless notebook environment.
  It must be `pip install`ed before running tests in this environment.
  It IS included in `requirements.txt` and is automatically installed during app deployment.
- This test baseline must be reproduced at the start of every Phase 1 implementation session
  before any code changes are made.
