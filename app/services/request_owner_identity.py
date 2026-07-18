"""Trusted request-owner identity derivation utilities.

This module is intentionally isolated from request handling. It derives a
stable owner identifier from the trusted ``X-Forwarded-User`` header using
HMAC-SHA256 and does not provide any fallback identity source.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from typing import ClassVar, Mapping, Optional
import unicodedata


_OWNER_SECRET_ENV_VAR = "CONVERSATION_OWNER_HMAC_SECRET"
_HEADER_NAME = "X-Forwarded-User"
_SOURCE_LABEL = "x-forwarded-user"
_DOMAIN_SEPARATION_PREFIX = b"transparence-owner-identity:v1\x00"
_MAX_CANONICAL_PRINCIPAL_LENGTH = 512
_MIN_SECRET_BYTES = 32


class RequestOwnerIdentityError(ValueError):
    """Base error for request owner identity derivation."""


class RequestOwnerIdentityConfigurationError(RequestOwnerIdentityError):
    """Raised when owner-identity configuration is missing or invalid."""


class RequestOwnerIdentityMissingError(RequestOwnerIdentityError):
    """Raised when the trusted request owner identity is missing."""


class RequestOwnerIdentityInvalidError(RequestOwnerIdentityError):
    """Raised when the trusted request owner identity is invalid."""


@dataclass(frozen=True, repr=False)
class RequestOwnerIdentity:
    """Derived request owner identity with safe representation."""

    owner_user_id_hash: str
    audit_principal: str
    source: str

    def __post_init__(self) -> None:
        if not self.owner_user_id_hash:
            raise ValueError("owner_user_id_hash must not be empty")
        if not self.audit_principal:
            raise ValueError("audit_principal must not be empty")
        if not self.source:
            raise ValueError("source must not be empty")

    def __repr__(self) -> str:
        return (
            "RequestOwnerIdentity("
            "owner_user_id_hash=<redacted>, "
            "audit_principal=<redacted>, "
            f"source={self.source!r}"
            ")"
        )


@dataclass(frozen=True, repr=False)
class RequestOwnerIdentitySettings:
    """Immutable settings for request owner identity derivation."""

    hmac_secret: bytes

    header_name: ClassVar[str] = _HEADER_NAME
    source_label: ClassVar[str] = _SOURCE_LABEL
    domain_separation_prefix: ClassVar[bytes] = _DOMAIN_SEPARATION_PREFIX
    secret_env_var: ClassVar[str] = _OWNER_SECRET_ENV_VAR
    min_secret_bytes: ClassVar[int] = _MIN_SECRET_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.hmac_secret, bytes):
            raise TypeError("hmac_secret must be bytes")
        if not self.hmac_secret:
            raise RequestOwnerIdentityConfigurationError(
                "Trusted owner identity secret is required."
            )
        if len(self.hmac_secret) < self.min_secret_bytes:
            raise RequestOwnerIdentityConfigurationError(
                "Trusted owner identity secret does not meet minimum security requirements."
            )

    @classmethod
    def from_environment(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "RequestOwnerIdentitySettings":
        source = os.environ if environ is None else environ
        raw_secret = source.get(cls.secret_env_var)
        if raw_secret is None:
            raise RequestOwnerIdentityConfigurationError(
                "Trusted owner identity secret is not configured."
            )
        if raw_secret == "":
            raise RequestOwnerIdentityConfigurationError(
                "Trusted owner identity secret must not be blank."
            )
        if raw_secret != raw_secret.strip():
            raise RequestOwnerIdentityConfigurationError(
                "Trusted owner identity secret must not contain surrounding whitespace."
            )
        secret_bytes = raw_secret.encode("utf-8")
        if not secret_bytes:
            raise RequestOwnerIdentityConfigurationError(
                "Trusted owner identity secret must not be blank."
            )
        if len(secret_bytes) < cls.min_secret_bytes:
            raise RequestOwnerIdentityConfigurationError(
                "Trusted owner identity secret does not meet minimum security requirements."
            )
        return cls(hmac_secret=secret_bytes)

    def __repr__(self) -> str:
        return "RequestOwnerIdentitySettings(hmac_secret=<redacted>)"


class RequestOwnerIdentityProvider:
    """Derives stable owner identifiers from trusted request headers."""

    def __init__(self, settings: RequestOwnerIdentitySettings) -> None:
        self._settings = settings

    def __repr__(self) -> str:
        return "RequestOwnerIdentityProvider(settings=RequestOwnerIdentitySettings(hmac_secret=<redacted>))"

    def derive_from_header_value(
        self,
        forwarded_user: Optional[str],
    ) -> RequestOwnerIdentity:
        canonical_principal = _canonicalize_forwarded_user(forwarded_user)
        digest = hmac.new(
            self._settings.hmac_secret,
            self._settings.domain_separation_prefix + canonical_principal.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return RequestOwnerIdentity(
            owner_user_id_hash=digest,
            audit_principal=canonical_principal,
            source=self._settings.source_label,
        )

    def derive_from_headers(
        self,
        headers: Mapping[str, str],
    ) -> RequestOwnerIdentity:
        matching_keys = [
            key for key in headers.keys() if key.lower() == self._settings.header_name.lower()
        ]
        if not matching_keys:
            raise RequestOwnerIdentityMissingError(
                "Trusted request owner identity header is missing."
            )
        if len(matching_keys) > 1:
            raise RequestOwnerIdentityInvalidError(
                "Trusted request owner identity header is ambiguous."
            )
        header_value = headers[matching_keys[0]]
        return self.derive_from_header_value(header_value)


def _canonicalize_forwarded_user(forwarded_user: Optional[str]) -> str:
    if forwarded_user is None:
        raise RequestOwnerIdentityMissingError(
            "Trusted request owner identity is missing."
        )

    stripped = forwarded_user.strip()
    if not stripped:
        raise RequestOwnerIdentityInvalidError(
            "Trusted request owner identity is empty or invalid."
        )

    canonical = unicodedata.normalize("NFC", stripped)
    if not canonical:
        raise RequestOwnerIdentityInvalidError(
            "Trusted request owner identity is empty or invalid."
        )
    if len(canonical) > _MAX_CANONICAL_PRINCIPAL_LENGTH:
        raise RequestOwnerIdentityInvalidError(
            "Trusted request owner identity exceeds the maximum supported length."
        )
    if "," in canonical:
        raise RequestOwnerIdentityInvalidError(
            "Trusted request owner identity is ambiguous."
        )
    for char in canonical:
        if char in {"\r", "\n", "\x00"} or unicodedata.category(char) == "Cc":
            raise RequestOwnerIdentityInvalidError(
                "Trusted request owner identity contains unsupported characters."
            )
    return canonical


def create_request_owner_identity_provider(
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> RequestOwnerIdentityProvider:
    """Construct a fresh owner identity provider from the environment."""

    settings = RequestOwnerIdentitySettings.from_environment(environ=environ)
    return RequestOwnerIdentityProvider(settings=settings)


__all__ = [
    "RequestOwnerIdentity",
    "RequestOwnerIdentityError",
    "RequestOwnerIdentityConfigurationError",
    "RequestOwnerIdentityInvalidError",
    "RequestOwnerIdentityMissingError",
    "RequestOwnerIdentityProvider",
    "RequestOwnerIdentitySettings",
    "create_request_owner_identity_provider",
]
