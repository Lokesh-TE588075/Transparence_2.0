# HMAC and Secret Policy

## Derivation algorithm

Phase 4A derives the durable owner identifier with HMAC-SHA256:

`HMAC(secret, b"transparence-owner-identity:v1\x00" + canonical_principal_bytes)`

The output is the lowercase hexadecimal digest stored as `owner_user_id_hash`.

Properties:

* deterministic for the same secret and canonical principal
* different principals produce different hashes
* different secrets produce different hashes
* no plain SHA-256 of the principal
* no secret-concatenation hash pattern
* no encryption step
* no random salt that would destabilize ownership

## Secret loading policy

Settings load only from `CONVERSATION_OWNER_HMAC_SECRET` when `RequestOwnerIdentitySettings.from_environment()` is called.
The module does not read the environment on import.

Requirements enforced by Phase 4A:

* missing secret fails explicitly
* blank secret fails explicitly
* surrounding whitespace is rejected, not trimmed
* minimum size is 32 UTF-8 bytes
* no default secret
* no local-development fallback
* no generated per-process secret

## Safe handling

The secret is not exposed through:

* dataclass repr output
* provider repr output
* public exception messages
* logging

## Rotation risk

Changing the HMAC secret changes every derived `owner_user_id_hash` value.
That would break owner continuity for durable conversation records unless a migration or dual-key lookup strategy is introduced first.

Phase 4B or later phases must not rotate the secret casually.
Before any rotation, the application needs an explicit migration plan or temporary dual-key support so existing stored conversation records remain attributable to the correct owner.
