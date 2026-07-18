# Header Validation and Canonicalization

## Header matching

`RequestOwnerIdentityProvider.derive_from_headers()` performs case-insensitive matching on `X-Forwarded-User`.

Behavior:

* accepts exact, lowercase, or mixed-case header names
* ignores unrelated headers
* rejects a missing trusted header
* rejects multiple differently-cased `X-Forwarded-User` entries as ambiguous
* does not inspect `X-Forwarded-Email`, `X-User-Email`, `Authorization`, cookies, body fields, query-like keys, or frontend identifiers

## Canonicalization rules

The trusted header value is treated as a single opaque principal.
The provider does not parse display names, does not extract email addresses from angle brackets, and does not reinterpret the value.

Canonicalization steps:

1. reject `None`
2. strip surrounding whitespace
3. reject empty or whitespace-only results
4. normalize the stripped value with Unicode NFC
5. preserve case exactly as supplied after normalization
6. reject comma-separated or otherwise ambiguous multi-value content
7. reject CR, LF, NUL, and all control characters
8. reject canonical values longer than 512 characters

## Important consequences

* Case is preserved intentionally. Distinct opaque principals that differ only by case remain distinct.
* Unicode-equivalent forms normalize to the same canonical principal and therefore the same owner hash.
* The platform is responsible for supplying a stable canonical principal in `X-Forwarded-User`.

## Non-goals for Phase 4A

This phase does not read request headers from `chat.py` or any other route.
It provides only an isolated derivation component plus tests and documentation.
