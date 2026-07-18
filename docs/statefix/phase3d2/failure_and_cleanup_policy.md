# Failure and Cleanup Policy

## Exception Handling in Shutdown

The `reset_genie_pipeline()` call is wrapped in its own `try/except`:

```python
try:
    reset_genie_pipeline()
except Exception:
    logger.error("Genie pipeline cleanup failed during shutdown")
```

## Sanitization

The logged error is a **static string only**:
"Genie pipeline cleanup failed during shutdown"

The call:
- Has exactly one positional argument (the static message).
- Does NOT pass `exc_info=True`.
- Does NOT pass `stack_info=True`.
- Does NOT use `logger.exception()`.
- Does NOT interpolate the exception class, message, or arguments.
- Does NOT format or print the traceback.

This prevents lower-layer connection strings, credentials, tokens,
database hosts, endpoint resource paths, or SDK internals from
appearing in application logs.

## Why exc_info Is Excluded

`exc_info=True` causes the logging framework to capture and format the
full traceback, including the original exception message. Since
`reset_genie_pipeline()` cascades through pool close, connection close,
and runtime bundle close, failure messages from those layers may contain:

- PostgreSQL DSNs (`postgresql://user:password@host/db`)
- Lakebase endpoint paths (`projects/<id>/branches/<name>`)
- OAuth tokens (`dapi-...`)
- Service-principal identifiers
- Database hostnames

Omitting `exc_info` ensures none of these reach the log output.

## Cascade Safety

- If `reset_genie_pipeline()` raises, it does not prevent the
  application from exiting.
- If the lifespan body raises (app crash), the finally block still
  executes reset.
- If both the body and reset raise, Python's standard exception
  chaining applies — the body exception propagates, the reset
  exception is suppressed.

## No Double-Close

The factory's `reset_genie_pipeline()` already guards against double
close: it sets `_genie_pipeline = None` under lock before calling
`_close_attached_runtime_bundle`, which deletes the bundle attribute
after close.

## No Retry

A single call. No exponential backoff. No circuit breaker. If cleanup
fails, the process is terminating anyway. Shutdown continues after the
logged error.
