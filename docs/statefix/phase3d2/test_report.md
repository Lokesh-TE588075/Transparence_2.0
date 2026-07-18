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

### TestExceptionPaths (16 tests)
- Reset attempted when body raises
- Existing shutdown preserved on exception
- Reset failure handled gracefully (static message verified)
- Error output: no host leaked
- Error output: no endpoint leaked
- Error output: no credential leaked
- Error output: no password/DSN leaked
- No exc_info=True in error call
- No stack_info=True in error call
- No logger.exception used
- No positional exception argument (only static message)
- No retry loop
- Shutdown completes after reset failure
- Body exception propagates despite reset failure

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

## Sanitization Tests (Key Addition)

Tests 29-32f specifically verify that when `reset_genie_pipeline()`
raises an exception containing sensitive values:

- `host=prod.internal`
- `endpoint=projects/private/branches/production`
- `dapi-secret-token-12345`
- `password=secret`
- `postgresql://user:password@host/database`

The logger.error call:
- Contains only the static message "Genie pipeline cleanup failed during shutdown"
- Has exactly one positional argument (no exception interpolation)
- Has no `exc_info=True`
- Has no `stack_info=True`
- Does not use `logger.exception`
- Contains none of the sensitive values in any position

## Results

| Suite | Passed | Failed | Errors |
|-------|--------|--------|--------|
| Focused Phase 3D2 | 57 | 0 | 0 |
| Phase 3D1 + 3D2 | 77 | 0 | 0 |
| Combined persistence/composition/lifecycle | 627 | 0 | 0 |
| Complete non-live | 1454 | 0 | 0 |
