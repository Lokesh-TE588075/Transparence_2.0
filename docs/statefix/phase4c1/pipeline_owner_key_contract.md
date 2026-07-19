# Phase 4C1: Pipeline Owner-Key Contract

## Overview

Phase 4C1 adds an optional `owner_key` keyword-only parameter to
`GeniePipeline.run()`.  The parameter accepts the trusted
`owner_user_id_hash` from `RequestOwnerIdentity` and validates its
structural contract without using it for any functional operation.

## Signature

```python
def run(
    self,
    user_message: str,
    app_conversation_id: str,
    execution_time_ms: Optional[int] = None,
    *,
    owner_key: Optional[str] = None,
) -> Dict[str, Any]:
```

## Backward Compatibility

- `owner_key` is keyword-only with a default of `None`.
- All existing callers that omit `owner_key` remain valid.
- Positional callers (`pl.run("msg", "conv-id")`) remain valid.
- Return contract is unchanged.
- No functional behaviour changes when `owner_key` is None.

## Structural Validation

When `owner_key` is not None, the following structural constraints are
enforced:

- Must be a `str` instance.
- Must be exactly 64 characters.
- Must contain only lowercase hexadecimal characters (`[0-9a-f]`).
- Must not contain `@`.
- Must not have surrounding whitespace.
- Must not be blank.

Validation errors raise `ValueError` with the static message:
"Internal error: request identity contract violation."

The supplied value is NEVER included in the error message.

## Request-Local Scope

The `owner_key` parameter:
- Is NOT stored on the pipeline instance.
- Is NOT stored globally.
- Is NOT cached.
- Is NOT included in session state.
- Is NOT included in response data.
- Is NOT logged.
- Is NOT passed to Genie.
- Is NOT passed to SQL.
- Is NOT used as the session key.
- Is NOT passed to the durable adapter.

## Phase 4C2 Integration Point

In Phase 4C2, `owner_key` will be passed to the
`DurableGenieSessionAdapter` for conversation ownership verification.
