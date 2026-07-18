# Startup and Shutdown Policy

## Startup Policy

**Lazy initialization only.** The Genie pipeline is created on the first
request that calls `get_genie_pipeline()` in `chat.py`. Neither module
import nor lifespan entry constructs the pipeline.

Guarantees during startup:
- No GeniePipeline constructed.
- No GenieSessionStore constructed.
- No DurableGenieSessionRuntimeFactory constructed.
- No WorkspaceClient constructed.
- No OAuth credential generated.
- No psycopg connection pool opened.
- No SQL executed.
- No network access for Genie/Lakebase.

## Shutdown Policy

**Unconditional cleanup.** `reset_genie_pipeline()` is called exactly once
in the `finally` block regardless of:
- Whether the pipeline was ever created (no-op if None).
- Whether the lifespan body raised an exception.
- Whether the bundle is enabled or disabled.

## Ordering

1. Existing shutdown log ("Shutting down").
2. Existing TODO placeholders (no-op currently).
3. `reset_genie_pipeline()` call.
4. If reset raises: log error, continue exiting.

## No Retry

If `reset_genie_pipeline()` fails, the error is logged once. No retry
loop exists. The application proceeds to terminate.
