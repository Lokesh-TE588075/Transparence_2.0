# Phase 3D2: FastAPI Lifecycle Wiring

## Overview

Phase 3D2 connects the existing `reset_genie_pipeline()` cleanup function
to the FastAPI application shutdown lifecycle. This is the only change to
`app/main.py`.

## Existing Lifespan Contract

The application uses a single `@asynccontextmanager` lifespan function
registered via `FastAPI(lifespan=lifespan)`. Prior to this phase:

- Startup: logs app name and version.
- Yield: serves requests.
- Shutdown: logs shutdown message.
- No try/finally: bare yield without exception safety.

## Change Applied

The yield is now wrapped in `try/finally`:

```python
try:
    yield
finally:
    logger.info("Shutting down %s", settings.APP_NAME)
    try:
        reset_genie_pipeline()
    except Exception:
        logger.error(
            "Genie pipeline cleanup failed during shutdown",
            exc_info=True,
        )
```

## What This Achieves

- On normal shutdown: pipeline singleton is cleared, attached durable
  runtime bundle is closed (pool drained, connections released).
- On crash/exception: same cleanup occurs via finally block.
- On no-pipeline startup: reset is a safe no-op (clears None).
- On disabled bundle: close is still called through the existing
  `_close_attached_runtime_bundle` path in the factory.

## What This Does NOT Do

- Does not initialize the pipeline during startup.
- Does not import or use `get_genie_pipeline`.
- Does not touch request processing.
- Does not connect to Lakebase.
- Does not generate credentials.
