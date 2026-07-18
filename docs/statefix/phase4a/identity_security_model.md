# Phase 4A Identity Security Model

Phase 4A introduces an isolated identity-derivation component at `app/services/request_owner_identity.py`.
It does not modify request handling, routing, conversation creation, durable persistence wiring, or any runtime integration path.

## Trusted boundary

The trusted boundary is the Databricks Apps platform header `X-Forwarded-User`.
The provider treats the header value as a single opaque platform principal and derives a stable owner identifier from it.

Accepted ownership identifier:

* `owner_user_id_hash`

This derived hash is the ownership and authorization boundary for future durable conversation isolation.
The raw principal is never acceptable as the durable owner key.

## Explicitly rejected identity sources

The new provider does not accept or fall back to any of the following:

* `X-Forwarded-Email`
* `X-User-Email`
* `Authorization`
* cookies
* query parameters
* request-body email fields
* browser `conversation_id`
* frontend conversation identifiers
* anonymous/shared identities

Existing route-level email and `anonymous` fallbacks are legacy behavior in the current application and are not accepted by the new provider.
Browser `conversation_id` is a client correlation value, not an ownership identity.

## Domain model

The provider returns `RequestOwnerIdentity` with:

* `owner_user_id_hash`: the only ownership identifier
* `audit_principal`: the validated canonical trusted principal for informational/audit use only
* `source`: the label `x-forwarded-user`

`audit_principal` is informational only.
It must not be used for authorization, ownership, or repository scoping.

## Current integration state

Phase 4A intentionally leaves request integration out of scope.
No request path currently imports or uses the new provider.
This preserves the controlled sequencing for a later phase that will extract the trusted header and wire the result into durable runtime boundaries.
