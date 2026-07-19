"""Phase 4B2 — Trusted request-owner identity runtime.

Provides request-scoped resolution of RequestOwnerIdentity from request headers,
controlled by the ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY feature flag.

This module is the sole approved Phase 4B2 boundary between app/routes/chat.py
and the core identity derivation logic in request_owner_identity.py.

Security properties guaranteed by this module:
  - No environment read at module import time.
  - Disabled by default; disabled path never reads CONVERSATION_OWNER_HMAC_SECRET.
  - Disabled path never inspects X-Forwarded-User or any legacy identity header.
  - No global provider instance; no global identity; no cross-request state.
  - Error messages never contain raw principal, HMAC secret, or tracebacks.
  - Module does not import FastAPI or Starlette.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

# Core identity module is NOT imported at module level.
# All imports from app.services.request_owner_identity are deferred to the
# enabled path of resolve_from_headers to guarantee that module-load never
# reads CONVERSATION_OWNER_HMAC_SECRET.

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Feature flag environment variable name.
_ENV_VAR_FLAG = "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY"

#: Accepted values that enable the feature (case-insensitive, whitespace stripped).
_TRUE_VALUES: frozenset = frozenset({"true", "1", "yes", "on"})

#: Accepted values that disable the feature (case-insensitive, whitespace stripped).
#: Empty string is the missing/unset sentinel and maps to the safe disabled default.
_FALSE_VALUES: frozenset = frozenset({"false", "0", "no", "off", ""})

#: Attribute name used to attach the identity object to request.state.
REQUEST_OWNER_IDENTITY_STATE_ATTRIBUTE: str = "request_owner_identity"


# ---------------------------------------------------------------------------
# Sanitized error hierarchy
# ---------------------------------------------------------------------------


class RequestOwnerIdentityRuntimeError(Exception):
    """Base error for the trusted request-owner identity runtime."""


class RequestOwnerIdentityRuntimeConfigurationError(RequestOwnerIdentityRuntimeError):
    """Raised when the identity runtime configuration is absent or invalid.

    The public message must never contain the secret name, secret value,
    environment variable contents, or an internal traceback.
    """


class RequestOwnerIdentityRuntimeResolutionError(RequestOwnerIdentityRuntimeError):
    """Raised when trusted identity extraction fails for a specific request.

    The public message must never contain the raw principal, HMAC hash,
    or header value.
    """


# ---------------------------------------------------------------------------
# Immutable settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestOwnerIdentityRuntimeSettings:
    """Immutable runtime settings parsed from an environment mapping.

    Construction never reads os.environ at module import time; the caller
    must supply an explicit environ mapping or rely on from_environment().
    """

    enabled: bool

    @classmethod
    def from_environment(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "RequestOwnerIdentityRuntimeSettings":
        """Parse the feature flag from the supplied environment mapping.

        Defaults to disabled when the variable is absent or blank.
        Raises RequestOwnerIdentityRuntimeConfigurationError for invalid values.
        """
        source: Mapping[str, str] = os.environ if environ is None else environ
        # Read only the feature flag — never CONVERSATION_OWNER_HMAC_SECRET.
        raw: str = source.get(_ENV_VAR_FLAG, "")
        normalized: str = raw.strip().lower()
        if normalized in _TRUE_VALUES:
            return cls(enabled=True)
        if normalized in _FALSE_VALUES:
            return cls(enabled=False)
        # Invalid value — report generically; never echo the raw env value.
        raise RequestOwnerIdentityRuntimeConfigurationError(
            "Trusted request identity feature flag has an invalid value."
        )

    def __repr__(self) -> str:  # pragma: no cover — safe by construction
        return f"RequestOwnerIdentityRuntimeSettings(enabled={self.enabled!r})"


# ---------------------------------------------------------------------------
# Default provider factory (deferred import, no module-level side effects)
# ---------------------------------------------------------------------------


def _default_provider_factory(
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> object:  # Returns RequestOwnerIdentityProvider
    """Construct a fresh RequestOwnerIdentityProvider from the environment.

    This function defers the import of the core identity module so that
    CONVERSATION_OWNER_HMAC_SECRET is never read before the enabled path
    is confirmed.
    """
    from app.services.request_owner_identity import (
        create_request_owner_identity_provider,
    )

    return create_request_owner_identity_provider(environ=environ)


# ---------------------------------------------------------------------------
# Request-scoped resolver
# ---------------------------------------------------------------------------


class RequestOwnerIdentityRuntime:
    """Request-scoped resolver for the trusted owner identity.

    Constructor accepts an environment mapping and a provider-factory callable
    for full testability.  The provider is constructed at most once per
    resolve_from_headers() call; no provider or identity is held as instance
    state after the call returns.

    There is deliberately no process-global singleton of this class.
    """

    def __init__(
        self,
        *,
        environment: Optional[Mapping[str, str]] = None,
        provider_factory: Optional[Callable] = None,
    ) -> None:
        # Store the environment mapping.  No env var is read here.
        self._environment: Mapping[str, str] = (
            environment if environment is not None else os.environ
        )
        # Allow test injection of a fake provider factory.
        self._provider_factory: Callable = (
            provider_factory
            if provider_factory is not None
            else _default_provider_factory
        )

    def resolve_from_headers(
        self,
        headers: Mapping[str, str],
    ) -> Optional[object]:  # Returns Optional[RequestOwnerIdentity]
        """Derive the trusted request owner identity from headers.

        Disabled path:
          Returns None immediately.  Does not read CONVERSATION_OWNER_HMAC_SECRET,
          does not construct a provider, does not inspect X-Forwarded-User or
          any legacy identity header.

        Enabled path:
          Constructs a RequestOwnerIdentityProvider once per call, derives the
          identity from the supplied headers, and returns the immutable
          RequestOwnerIdentity.  No result is cached.

        Raises:
          RequestOwnerIdentityRuntimeConfigurationError — flag is invalid, or
              HMAC secret is missing/invalid.
          RequestOwnerIdentityRuntimeResolutionError — X-Forwarded-User header
              is missing or its value is invalid.
        """
        # Parse the feature flag.  This is the only environment read on both paths.
        try:
            settings = RequestOwnerIdentityRuntimeSettings.from_environment(
                self._environment
            )
        except RequestOwnerIdentityRuntimeConfigurationError:
            # Re-raise: already sanitized.
            raise
        except Exception:
            raise RequestOwnerIdentityRuntimeConfigurationError(
                "Trusted request identity is unavailable."
            ) from None

        if not settings.enabled:
            # Disabled: return immediately without touching the HMAC secret,
            # without constructing a provider, and without inspecting any header.
            return None

        # ----------------------------------------------------------------
        # Enabled path — all imports are deferred to here.
        # ----------------------------------------------------------------
        from app.services.request_owner_identity import (
            RequestOwnerIdentityConfigurationError,
            RequestOwnerIdentityInvalidError,
            RequestOwnerIdentityMissingError,
        )

        # Construct provider once per request.
        try:
            provider = self._provider_factory(environ=self._environment)
        except RequestOwnerIdentityConfigurationError:
            raise RequestOwnerIdentityRuntimeConfigurationError(
                "Trusted request identity is unavailable."
            ) from None
        except Exception:
            raise RequestOwnerIdentityRuntimeConfigurationError(
                "Trusted request identity is unavailable."
            ) from None

        # Derive identity from the headers supplied to this call.
        try:
            return provider.derive_from_headers(headers)
        except RequestOwnerIdentityConfigurationError:
            raise RequestOwnerIdentityRuntimeConfigurationError(
                "Trusted request identity is unavailable."
            ) from None
        except (RequestOwnerIdentityMissingError, RequestOwnerIdentityInvalidError):
            raise RequestOwnerIdentityRuntimeResolutionError(
                "Trusted request identity is required."
            ) from None
        except Exception:
            # Catch-all: never surface internal details.
            raise RequestOwnerIdentityRuntimeResolutionError(
                "Trusted request identity is required."
            ) from None


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def resolve_request_owner_identity(
    *,
    headers: Mapping[str, str],
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[object]:  # Returns Optional[RequestOwnerIdentity]
    """Resolve the trusted request owner identity once per request.

    Convenience wrapper that constructs a RequestOwnerIdentityRuntime with the
    supplied environment mapping (defaults to os.environ) and calls
    resolve_from_headers.  No module-level environment read.

    Args:
        headers: The request headers mapping.
        environ: Optional explicit environment mapping; defaults to os.environ.

    Returns:
        RequestOwnerIdentity when the feature is enabled and the header is valid.
        None when the feature is disabled.

    Raises:
        RequestOwnerIdentityRuntimeConfigurationError: Configuration unavailable.
        RequestOwnerIdentityRuntimeResolutionError: Header missing or invalid.
    """
    runtime = RequestOwnerIdentityRuntime(environment=environ)
    return runtime.resolve_from_headers(headers)


__all__ = [
    "REQUEST_OWNER_IDENTITY_STATE_ATTRIBUTE",
    "RequestOwnerIdentityRuntimeError",
    "RequestOwnerIdentityRuntimeConfigurationError",
    "RequestOwnerIdentityRuntimeResolutionError",
    "RequestOwnerIdentityRuntimeSettings",
    "RequestOwnerIdentityRuntime",
    "resolve_request_owner_identity",
]
