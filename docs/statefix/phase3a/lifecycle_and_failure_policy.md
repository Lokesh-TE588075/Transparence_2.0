# Lifecycle and Failure Policy

`ConversationRepositoryBundle` wraps the constructed repository together with backend metadata and optional owned resources.

## Bundle fields

* `repository`
* `backend`
* `durable`

The bundle may also own an optional backend-resource closer.

## Close behavior

### Memory bundle

Memory bundles own no external resources.

* `close()` is a no-op
* repeated `close()` calls are safe

### Durable bundle

Durable bundles own the durable connection-provider lifecycle.

* `close()` calls the provider closer once
* repeated `close()` calls are safe
* context-manager use is supported

## Failure policy

If durable repository construction fails after the provider owner has been created:

* the provider owner is closed best-effort
* the factory raises a sanitized initialization error
* the factory does not fall back to memory

## Security policy

Public error messages and bundle representations must not expose:

* database hostnames
* endpoint resource paths
* user or service-principal identifiers
* credentials
* OAuth tokens
* raw environment contents
* raw SDK exception details

`ConversationRepositoryBundle.__repr__()` only exposes backend, durability, and closed state. It does not include repository or provider settings.
