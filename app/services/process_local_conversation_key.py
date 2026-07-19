"""Process-local conversation key helper — Phase 4C4B3A.

Provides a deterministic, opaque, trusted-owner-scoped process-local key
that binds a Genie conversation entry in GenieSessionStore to the triple
(owner_user_id_hash, session_id, frontend_conversation_id).

The output is a domain-separated SHA-256 digest prefixed with ``plc_v1_``.
It is a namespace / isolation key, not an authentication credential.

Security properties:
  - Output contains none of the raw inputs.
  - Deterministic for identical inputs.
  - Different owner, session, or frontend ID → different output.
  - Exception messages and repr values never expose raw input values.
  - No logging of raw inputs anywhere in this module.
  - Safe for use as a dictionary key and safe if accidentally returned,
    although callers should avoid returning it in API responses.

Canonicalization:
  frontend_conversation_id is canonicalized via strip() before hashing,
  matching the DurableGenieSessionKey contract that strips and stores the
  canonical value.  This ensures that leading/trailing whitespace variants
  of the same frontend conversation ID map to the same process-local key
  and therefore to the same durable logical conversation.

Domain separator (change here or in tests will break the fixed digest vectors):
  b"transparence-process-local-conversation:v1\x00"
"""

from __future__ import annotations

import hashlib
import re

# ---------------------------------------------------------------------------
# Domain separator — intentionally hard-coded; any change alters all digests
# ---------------------------------------------------------------------------

_DOMAIN_SEP: bytes = b"transparence-process-local-conversation:v1\x00"

# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------

#: Fixed prefix that makes the format instantly recognisable in storage/logs.
KEY_PREFIX: str = "plc_v1_"

#: Pattern that every output value of build_process_local_conversation_key()
#: must match.  Used by is_valid_process_local_key() and by coordinators.
_KEY_PATTERN: re.Pattern = re.compile(r"^plc_v1_[0-9a-f]{64}$")

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

# Owner hash: mirrors coordinator / DurableGenieSessionKey contract.
_OWNER_HASH_PATTERN: re.Pattern = re.compile(r"^[0-9a-f]{64}$")

# Session ID: ASCII control characters including CR, LF, NUL, and all other
# C0 controls and DEL.  Does not reject non-ASCII Unicode — the session
# middleware may produce UTF-8 safe identifiers.
_SESSION_ID_CONTROL_RE: re.Pattern = re.compile(
    r"[\x00-\x1f\x7f]"
)

# Reasonable upper bound on session ID length (matching realistic cookie sizes).
_SESSION_ID_MAX_BYTES: int = 512

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ProcessLocalConversationKeyError(ValueError):
    """Raised when process-local conversation key construction fails.

    The public string representation never exposes raw input values.
    """

    def __repr__(self) -> str:  # never expose inputs
        return "ProcessLocalConversationKeyError()"

    def __str__(self) -> str:
        return "Process-local conversation key input validation failed."


# ---------------------------------------------------------------------------
# Internal validators / canonicalizers
# ---------------------------------------------------------------------------


def _validate_owner_hash(owner_user_id_hash: str) -> None:
    """Validate owner hash: exactly 64 lowercase hexadecimal characters."""
    if not isinstance(owner_user_id_hash, str) or not owner_user_id_hash:
        raise ProcessLocalConversationKeyError()
    if not _OWNER_HASH_PATTERN.match(owner_user_id_hash):
        raise ProcessLocalConversationKeyError()


def _validate_session_id(session_id: str) -> None:
    """Validate session ID: non-empty, no control characters, max 512 bytes."""
    if not isinstance(session_id, str) or not session_id:
        raise ProcessLocalConversationKeyError()
    if _SESSION_ID_CONTROL_RE.search(session_id):
        raise ProcessLocalConversationKeyError()
    try:
        encoded = session_id.encode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        raise ProcessLocalConversationKeyError()
    if len(encoded) > _SESSION_ID_MAX_BYTES:
        raise ProcessLocalConversationKeyError()


def _canonicalize_frontend_conversation_id(frontend_conversation_id: str) -> str:
    """Canonicalize and validate the frontend conversation ID.

    Applies the same rules as DurableGenieSessionKey (non-empty after strip,
    no @ character) without importing the adapter module.  Inlined here to
    preserve adapter isolation while keeping the contract identical.

    Returns the canonical (stripped) value, which is used in the SHA-256
    digest.  This ensures that leading/trailing whitespace variants of the
    same frontend conversation ID produce the same process-local key and
    therefore refer to the same durable logical conversation.
    """
    canonical = frontend_conversation_id.strip()
    if not canonical:
        raise ProcessLocalConversationKeyError()
    if "@" in canonical:
        raise ProcessLocalConversationKeyError()
    return canonical


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_process_local_conversation_key(
    *,
    owner_user_id_hash: str,
    session_id: str,
    frontend_conversation_id: str,
) -> str:
    """Derive an opaque, deterministic, owner-scoped process-local key.

    Parameters
    ----------
    owner_user_id_hash
        Exactly 64 lowercase hexadecimal characters (trusted owner identity
        hash derived by the request identity runtime).
    session_id
        Non-empty session value from the httponly cookie.  Must not contain
        CR, LF, NUL, or any other ASCII control character (0x00\u20131F, 0x7F).
        Maximum 512 UTF-8 bytes.
    frontend_conversation_id
        Raw conversation ID supplied by the frontend browser.  Validated and
        canonicalized against the DurableGenieSessionKey contract: non-empty
        after strip, no ``@`` character.  Leading/trailing whitespace is
        stripped before hashing so that whitespace variants of the same ID
        produce the same process-local key.

    Returns
    -------
    str
        Opaque key of the form ``plc_v1_<64 lowercase hex chars>``.
        Total length: 71 characters.

    Raises
    ------
    ProcessLocalConversationKeyError
        When any input fails validation.  The exception message and repr
        never expose raw input values.
    """
    _validate_owner_hash(owner_user_id_hash)
    _validate_session_id(session_id)
    canonical_frontend = _canonicalize_frontend_conversation_id(
        frontend_conversation_id
    )

    digest: str = hashlib.sha256(
        _DOMAIN_SEP
        + owner_user_id_hash.encode("ascii")
        + b"\x00"
        + session_id.encode("utf-8")
        + b"\x00"
        + canonical_frontend.encode("utf-8")
    ).hexdigest()

    return KEY_PREFIX + digest


def is_valid_process_local_key(value: object) -> bool:
    """Return True if *value* matches the exact output format.

    Used by coordinators and routes to validate that a supplied key was
    produced by this module rather than being a raw frontend ID, session ID,
    or owner hash.
    """
    if not isinstance(value, str):
        return False
    return bool(_KEY_PATTERN.match(value))
