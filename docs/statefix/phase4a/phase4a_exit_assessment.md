# Phase 4A Exit Assessment

## Verdict

Phase 4A is complete as an isolated derivation phase.
The repository now contains a standard-library-only owner identity module, a focused test suite, and supporting documentation.

## What Phase 4A achieved

* defined sanitized domain-specific exceptions
* defined immutable settings and identity dataclasses with safe repr behavior
* added explicit environment-based HMAC secret loading with no import-time reads
* added canonicalization and validation for the trusted `X-Forwarded-User` principal
* derived `owner_user_id_hash` via HMAC-SHA256 with domain separation
* preserved separation between ownership (`owner_user_id_hash`) and audit/display (`audit_principal`)
* added tests proving legacy fallback rejection and absence of request-path integration

## What Phase 4A did not do

* did not change `chat.py`
* did not change `main.py`
* did not change `genie_pipeline.py`
* did not change the durable adapter
* did not wire the provider into request processing
* did not configure the secret in `app.yaml`
* did not connect to Lakebase
* did not open a pool
* did not generate credentials
* did not execute SQL
* did not deploy or restart the app

## Test results (corrected after transient plugin installation)

`pytest-asyncio` was installed transiently for the validation rerun.
It is not in `requirements.txt` and was not added there.
All existing async lifecycle tests executed fully; none were skipped.

| Suite | Passed | Skipped | Failed |
|-------|--------|---------|--------|
| Focused Phase 4A | 84 | 0 | 0 |
| Combined persistence/composition/lifecycle/identity | 712 | 0 | 0 |
| Complete non-live | 1539 | 0 | 0 |

## Phase 4B gate

Phase 4B may begin only if it preserves the Phase 4A security boundary:

* use only `X-Forwarded-User` as the trusted request owner input
* keep `owner_user_id_hash` as the authorization boundary
* keep `audit_principal` informational only
* preserve the no-fallback policy
* configure secret injection deliberately rather than adding defaults
* plan for future secret rotation before any production rotation attempt

## Secret rotation warning

If the HMAC secret changes, all derived owner hashes change.
Before production secret rotation, a migration path or dual-key support is required so existing conversation ownership remains stable.
