# TransparencE — Controlled Dev Rollout Checklist

**Date:** 2026-07-09  
**Target Environment:** Dev (`te-ss-coe-dev.cloud.databricks.com`)  
**App:** `transparence` (https://transparence-4310366453016539.aws.databricksapps.com)  
**Service Principal:** `app-31pcl9 transparence` (ID: 78664835752275)  

---

## 1. Environment Variable Changes (`app.yaml`)

Add/update the following in the `env` section of `app.yaml`:

```yaml
env:
  # --- Existing (unchanged) ---
  - name: DATABRICKS_SQL_WAREHOUSE_PATH
    value: "/sql/1.0/warehouses/8e46614f7064d8fd"
  - name: SHIPMENT_TABLE_NAME
    value: "onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard"
  - name: LLM_ENDPOINT_PRIMARY
    value: "databricks-claude-sonnet-5"
  - name: LLM_ENDPOINT_FAST
    value: "databricks-gpt-5-4-mini"

  # --- NEW: Phase 7B/8 Pipeline Flags ---
  - name: USE_NEW_ACCURACY_PIPELINE
    value: "true"
  - name: USE_DELTA_CONVERSATION_STATE
    value: "true"
  - name: NEW_PIPELINE_FALLBACK_TO_OLD
    value: "true"
  - name: NEW_PIPELINE_DEBUG
    value: "false"
```

### Flag Behavior Summary

| Flag | Value | Effect |
|------|-------|--------|
| `USE_NEW_ACCURACY_PIPELINE` | `true` | Routes all chat requests through the new accuracy pipeline (input normalizer → query understanding → SQL template engine → result processor → response formatter → grounded summarizer) |
| `USE_DELTA_CONVERSATION_STATE` | `true` | Persists conversation context to Delta table `onedata_fn_ion_dev.ion_l0_raw.shipmate_conversation_state` (survives app restarts, enables cross-session recovery) |
| `NEW_PIPELINE_FALLBACK_TO_OLD` | `true` | On any unhandled exception in the new pipeline, falls back to the legacy LLM-only SQL generation path (safety net) |
| `NEW_PIPELINE_DEBUG` | `false` | Suppresses verbose debug logging (set `true` only during active debugging) |

---

## 2. Pre-Deployment Checks

### 2.1 Code & Tests
- [ ] Full test suite passes: **236 passed, 1 skipped, 0 failed**
  ```bash
  python -m pytest tests/ -q --tb=short -p no:cacheprovider --import-mode=importlib
  ```
- [ ] No lint errors in modified files (`app/config.py`, `app/services/chat_pipeline.py`, `app/routes/chat.py`)
- [ ] `CONVERSATION_STATE_CLEANUP_HARD_DELETE` appears exactly ONCE in `app/config.py`
- [ ] All 7 feature flags verified in config (no duplicates)

### 2.2 Infrastructure
- [ ] SQL Warehouse `8e46614f7064d8fd` is RUNNING and responsive
- [ ] Delta table `onedata_fn_ion_dev.ion_l0_raw.shipmate_conversation_state` exists and is accessible by the service principal
- [ ] LLM endpoints `databricks-claude-sonnet-5` and `databricks-gpt-5-4-mini` are READY
- [ ] Shipment table `onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard` is accessible (446K+ rows)
- [ ] Cleanup job `911175137558945` is UNPAUSED and last run was SUCCESS

### 2.3 Permissions
- [ ] Service principal has SELECT on `lbn_with_scorecard`
- [ ] Service principal has SELECT/INSERT/UPDATE on `shipmate_conversation_state`
- [ ] Service principal has INSERT on `shipmate_conversations`, `shipmate_messages`, `chatbot_feedback`, `query_audit_log`
- [ ] Service principal can call LLM serving endpoints

### 2.4 App State
- [ ] Current app deployment is healthy (no active errors)
- [ ] Note current app deployment version/commit for rollback reference

---

## 3. Deployment Steps

1. Update `app.yaml` with the env vars above
2. Deploy from Apps UI (CLI deploy is blocked by guardrails)
3. Wait for deployment status → RUNNING
4. Verify app health endpoint responds: `GET /health` → 200
5. Begin smoke tests

---

## 4. Post-Deployment Smoke Tests (Manual)

Run these 5 tests immediately after deployment:

| # | Test | Expected |
|---|------|----------|
| 1 | `GET /health` | 200, `{"status": "healthy", ...}` |
| 2 | Send: "How many shipments are there?" | Returns count ~446K, valid SQL with `COUNT(*)` |
| 3 | Send: "shipments from US via air" | SQL has `source_ = 'US'` AND `transportation_mode_desc = 'Air transport'` |
| 4 | Follow-up: "which are delayed?" | Preserves US + Air filters, adds delay logic |
| 5 | Send: "clear" or start new conversation | Context resets, Delta state marked inactive |

---

## 5. UAT Prompts (25 Scenarios)

### Category A: Basic Shipment Lookup (5 prompts)

| # | Prompt | Expected Behavior |
|---|--------|-------------------|
| A1 | "Show me all shipments from plant 1234" | SQL filters on `plant = '1234'`; returns results or helpful empty-result message |
| A2 | "How many active shipments do we have?" | SQL: `COUNT(*)` with `status NOT IN ('Delivered','Completed')`; returns numeric answer |
| A3 | "List shipments for material ABC-12345" | SQL filters on `material` column; displays tabular results |
| A4 | "What's the total revenue of completed shipments?" | SQL: `SUM(sales_functional_currency_amount)` with completed status filter |
| A5 | "Show me the top 10 heaviest shipments" | SQL: `ORDER BY chargeable_weight DESC LIMIT 10`; tabular response |

### Category B: Country & Mode Filters (5 prompts)

| # | Prompt | Expected Behavior |
|---|--------|-------------------|
| B1 | "Shipments from Germany to the US" | SQL: `source_ = 'DE'` AND `destination = 'US'` (ISO codes) |
| B2 | "All ocean shipments from China" | SQL: `source_ = 'CN'` AND `transportation_mode_desc = 'Ocean Transport'` |
| B3 | "Air freight to Japan" | SQL: `destination = 'JP'` AND `transportation_mode_desc = 'Air transport'` |
| B4 | "Shipments via sea from India to UK" | SQL: `source_ = 'IN'`, `destination = 'GB'`, mode = Ocean Transport |
| B5 | "How many shipments go from Mexico by truck?" | SQL: `source_ = 'MX'`, mode = road/truck variant; COUNT(*) |

### Category C: Follow-ups & Conversation Context (5 prompts)

| # | Prompt | Expected Behavior |
|---|--------|-------------------|
| C1 | First: "shipments from US" → Follow-up: "only via air" | Second query preserves `source_ = 'US'`, adds Air transport filter |
| C2 | First: "active shipments to Germany" → Follow-up: "which are delayed?" | Preserves destination=DE + active status, adds delay logic |
| C3 | First: "ocean shipments" → Follow-up: "how many?" | Preserves Ocean Transport filter, changes to COUNT(*) |
| C4 | First: "top 5 by revenue" → Follow-up: "now show me the bottom 5" | Flips ORDER BY from DESC to ASC, keeps revenue metric |
| C5 | First: "shipments from JP" → Follow-up: "can you summarize that?" | SUMMARIZE_LAST_RESULT intent; produces natural language summary of previous results |

### Category D: Summaries & Aggregations (3 prompts)

| # | Prompt | Expected Behavior |
|---|--------|-------------------|
| D1 | "Give me a breakdown of shipments by transportation mode" | SQL: GROUP BY `transportation_mode_desc`, COUNT(*); formatted summary |
| D2 | "What's the average delay for completed shipments?" | SQL: AVG of `GREATEST(DATEDIFF(DATE(ata), DATE(eta)), 0)`; single number answer |
| D3 | "Summarize shipment volume by source country, top 10" | SQL: GROUP BY `source_`, COUNT(*), ORDER BY DESC, LIMIT 10 |

### Category E: Empty Results & Edge Cases (3 prompts)

| # | Prompt | Expected Behavior |
|---|--------|-------------------|
| E1 | "Shipments from Antarctica" | Returns 0 rows; friendly message: "No shipments found matching..." with suggestions |
| E2 | "Show me shipments arriving tomorrow" | May return 0 rows depending on data freshness; graceful empty-result handling |
| E3 | "Shipments with weight > 999999999" | 0 rows; suggests broadening criteria |

### Category F: Delivery Date & ETA Logic (2 prompts)

| # | Prompt | Expected Behavior |
|---|--------|-------------------|
| F1 | "Which shipments are currently delayed?" | SQL: active status + `GREATEST(DATEDIFF(CURRENT_DATE(), DATE(eta)), 0) > 0`; live delay calc |
| F2 | "Average delay for delivered shipments from last month" | SQL: completed status + `GREATEST(DATEDIFF(DATE(ata), DATE(eta)), 0)` + date filter on delivery |

### Category G: Noisy Input (2 prompts)

| # | Prompt | Expected Behavior |
|---|--------|-------------------|
| G1 | "shw me shipmints frm US by aire" | Input normalizer corrects typos → same as "show me shipments from US by air" |
| G2 | "URGENT!!! how many shipments are LATE???" | Strips noise, interprets as delay query for active shipments |

---

## 6. Rollback Steps

If critical issues are found:

### Immediate Rollback (< 2 minutes)
1. Update `app.yaml`:
   ```yaml
   - name: USE_NEW_ACCURACY_PIPELINE
     value: "false"
   - name: USE_DELTA_CONVERSATION_STATE
     value: "false"
   ```
2. Redeploy from Apps UI
3. Verify app returns to legacy pipeline behavior

### Full Rollback (if env var change insufficient)
1. Revert `app.yaml` to previous version (remove all Phase 7B/8 env vars)
2. Redeploy
3. Pause cleanup job (ID: 911175137558945) to avoid unnecessary runs
4. Verify legacy chat flow works end-to-end

### Post-Rollback
- [ ] Document the failure scenario in a ticket
- [ ] Delta conversation state table data is harmless (will expire via TTL) — no cleanup needed
- [ ] In-memory fallback ensures no user-facing impact during rollback

---

## 7. Logs & Metrics to Monitor

### Application Logs
| Log Pattern | What It Means | Action if Excessive |
|-------------|---------------|--------------------|
| `[NEW_PIPELINE] Processing query` | New pipeline handling a request | Expected — confirms routing |
| `[NEW_PIPELINE] Falling back to old pipeline` | New pipeline failed, fallback triggered | Investigate if > 10% of requests |
| `[DELTA_STATE] Persisted context` | Conversation state saved to Delta | Expected |
| `[DELTA_STATE] Loaded from Delta` | Recovered state from Delta (cold start recovery) | Expected |
| `[DELTA_STATE] Failed to persist` | Delta write failed (in-memory still works) | Monitor — may indicate warehouse issue |
| `ERROR` + `LLM timeout` | LLM endpoint slow/unresponsive | Check endpoint health |
| `SQL execution error` | Generated SQL is invalid | Review query understanding + template engine |

### Key Metrics
| Metric | Where to Check | Healthy Range |
|--------|---------------|---------------|
| Response latency (p50) | App logs / monitoring | < 8s |
| Response latency (p95) | App logs / monitoring | < 15s |
| Fallback rate | Count of fallback log lines / total requests | < 5% |
| Empty result rate | Count of "No shipments found" / total queries | < 20% |
| SQL validation failures | `query_audit_log` table | < 2% |
| Delta state persist failures | App logs | < 1% |
| Conversation state table row count | `SELECT COUNT(*) FROM shipmate_conversation_state WHERE is_active=true` | Growing slowly, < 1000 active |

### Monitoring Queries
```sql
-- Audit log: recent query success rate
SELECT 
  DATE(timestamp) as day,
  COUNT(*) as total_queries,
  SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as successful,
  ROUND(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) as success_pct
FROM onedata_fn_ion_dev.ion_l0_raw.query_audit_log
WHERE timestamp > CURRENT_DATE() - INTERVAL 7 DAYS
GROUP BY 1 ORDER BY 1 DESC;

-- Active conversation state sessions
SELECT COUNT(*) as active_sessions,
       MIN(updated_at) as oldest_session,
       MAX(updated_at) as newest_session
FROM onedata_fn_ion_dev.ion_l0_raw.shipmate_conversation_state
WHERE is_active = true;

-- Feedback sentiment (if users rate responses)
SELECT rating, COUNT(*) as count
FROM onedata_fn_ion_dev.ion_l0_raw.chatbot_feedback
WHERE timestamp > CURRENT_DATE() - INTERVAL 7 DAYS
GROUP BY 1 ORDER BY 1;
```

---

## 8. Go / No-Go Criteria

### GO (proceed to wider rollout)
All of the following must be true:

- [ ] All 5 smoke tests pass
- [ ] At least 22/25 UAT prompts produce correct results
- [ ] Fallback rate < 5% over 50+ queries
- [ ] No unhandled exceptions in app logs
- [ ] Response latency p95 < 15 seconds
- [ ] Delta conversation state persists and recovers correctly
- [ ] Follow-up queries correctly preserve context in 4/5 tests
- [ ] Empty-result scenarios produce helpful (not cryptic) messages
- [ ] No SQL injection or unsafe query patterns in audit log
- [ ] Cleanup job continues running successfully every 6 hours

### NO-GO (rollback immediately)
Any ONE of the following triggers rollback:

- [ ] Fallback rate > 20% (new pipeline is unreliable)
- [ ] Any SQL injection or unvalidated SQL reaching the warehouse
- [ ] Response latency p95 > 30 seconds consistently
- [ ] App crashes or restarts repeatedly
- [ ] Conversation state causes data corruption or cross-user leakage
- [ ] LLM endpoint returns errors for > 5 consecutive requests
- [ ] Delta state table grows uncontrolled (cleanup job failing)

### CONDITIONAL (fix and re-test within 24h)
- [ ] 3-5 UAT prompts fail but are non-critical edge cases
- [ ] Fallback rate 5-20% (investigate specific failure patterns)
- [ ] Minor formatting issues in responses (not data accuracy)
- [ ] Delta persist fails intermittently but in-memory works fine

---

## 9. Sign-Off

| Role | Name | Date | Status |
|------|------|------|--------|
| Developer | | | |
| QA / Tester | | | |
| Product Owner | | | |

---

*Document generated: 2026-07-09 | Project: TransparencE Shipment Intelligence v2.0*
