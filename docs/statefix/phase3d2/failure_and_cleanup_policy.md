# Failure and Cleanup Policy

## Exception Handling in Shutdown

The `reset_genie_pipeline()` call is wrapped in its own `try/except`:

```python
try:
    reset_genie_pipeline()
except Exception:
    logger.error(
        "Genie pipeline cleanup failed during shutdown",
        exc_info=True,
    )
```

## Sanitization

The logged error message is a static string:
"Genie pipeline cleanup failed during shutdown"

It does NOT include:
- Host names or URLs.
- Endpoint names or project paths.
- Credentials, tokens, or API keys.
- Connection strings.

The `exc_info=True` parameter logs the traceback at ERROR level, which
is appropriate for structured logging backends. The traceback itself
may contain exception message text from lower layers, but the format
string remains safe.

## Cascade Safety

- If `reset_genie_pipeline()` raises, it does not prevent the
  application from exiting.
- If the lifespan body raises (app crash), the finally block still
  executes reset.
- If both the body and reset raise, Python's standard exception
  chaining applies — the body exception is the primary, reset
  exception is suppressed in the finally.

## No Double-Close

The factory's `reset_genie_pipeline()` already guards against double
close: it sets `_genie_pipeline = None` under lock before calling
`_close_attached_runtime_bundle`, which deletes the bundle attribute
after close.

## No Retry

A single call. No exponential backoff. No circuit breaker. If cleanup
fails, the process is terminating anyway.
