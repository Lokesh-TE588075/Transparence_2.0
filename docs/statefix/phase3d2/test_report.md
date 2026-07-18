# Phase 3D2 Test Report

## Test File

`tests/test_main_durable_runtime_lifecycle.py`

## Test Classes and Coverage

### TestApplicationCompatibility (7 tests)
- App is FastAPI instance
- Title/version unchanged
- Routes registered
- Health endpoint present
- Middleware registered
- Import does not call get_genie_pipeline
- Import does not create runtime bundle

### TestStartupSafety (9 tests)
- Startup does not call get_genie_pipeline
- No session store constructed
- No runtime factory constructed
- No repository constructed
- No WorkspaceClient constructed
- No credential generated
- No pool opened
- No SQL executed
- Existing startup behaviour preserved

### TestShutdownBehaviour (10 tests)
- Reset called exactly once on normal exit
- Reset occurs after yield
- Reset safe when no pipeline
- Reset called when pipeline exists
- Disabled bundle closed through reset
- Enabled bundle closed through reset
- Bundle closes exactly once
- Repository bundle not closed directly by main
- Repeated lifespan runs safe
- Existing shutdown operations preserved

### TestExceptionPaths (7 tests)
- Reset attempted when body raises
- Existing shutdown preserved on exception
- Reset failure handled gracefully
- Error output: no host
- Error output: no endpoint
- Error output: no credential
- No retry loop

### TestRuntimeBoundaries (7 tests)
- chat.py unchanged
- genie_pipeline.py unchanged
- GenieSessionStore unchanged
- genie_backend_factory.py unchanged
- No request-path adapter call
- No deployment config changed
- No global runtime bundle in main

### TestAdditionalLifecycleSafety (10 tests)
- main.py parses cleanly
- No get_genie_pipeline import
- Only reset imported from factory
- No LAKEBASE/PG env var read
- Lifespan uses try/finally
- Yield inside try block
- No DurableGenieSessionRuntimeFactory import
- No lakebase_connection_provider import
- No WorkspaceClient import
- Concurrent lifespan isolation

## Results

| Suite | Passed | Failed | Errors |
|-------|--------|--------|--------|
| Focused Phase 3D2 | 51 | 0 | 0 |
| Phase 3D1 + 3D2 | 71 | 0 | 0 |
| Combined persistence/composition/lifecycle | 621 | 0 | 0 |
| Complete non-live | 1448 | 0 | 0 |

## Baseline Comparison

| Metric | Phase 3D1 Exit | Phase 3D2 Exit | Delta |
|--------|---------------|----------------|-------|
| Focused tests | 20 | 51 | +31 |
| Combined | 486 | 621 | +135 |
| Full non-live | 1397 | 1448 | +51 |
