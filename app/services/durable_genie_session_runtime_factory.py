"""Disabled-by-default runtime composition factory for durable Genie session persistence.

Phase 3C composes ConversationRepositoryFactory and DurableGenieSessionAdapter
behind an explicit ENABLE_DURABLE_GENIE_SESSION_ADAPTER feature flag that
defaults to false.

Design constraints:

* No environment access during module import.
* No module-level singletons of any kind.
* Disabled is the unconditional default; disabling reads no PG variables.
* Explicit enablement failure is always surfaced; never silently returns a
  disabled bundle after enablement was requested.
* No silent fallback to memory beyond what ConversationRepositoryFactory selects.
* Repr and error messages expose no host, endpoint, owner, Genie identifiers,
  tokens, credentials, or raw environment contents.
* The DurableGenieSessionAdapter owns the ConversationRepositoryBundle lifecycle;
  the runtime bundle must not close the repository bundle a second time.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.conversation_repository_factory import (
        ConversationRepositoryBackend,
        ConversationRepositoryBundle,
        ConversationRepositoryFactory,
    )
    from app.services.durable_genie_session_adapter import DurableGenieSessionAdapter

# ---------------------------------------------------------------------------
# Environment variable name
# ---------------------------------------------------------------------------

_ENABLE_ENV_VAR = "ENABLE_DURABLE_GENIE_SESSION_ADAPTER"
_TRUTHY_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSEY_VALUES = frozenset({"false", "0", "no", "off", ""})


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class DurableGenieSessionRuntimeFactoryError(Exception):
    """Base exception for runtime factory failures."""


class DurableGenieSessionRuntimeConfigurationError(
    DurableGenieSessionRuntimeFactoryError
):
    """Raised when runtime factory configuration is missing, invalid, or disallowed."""


class DurableGenieSessionRuntimeInitializationError(
    DurableGenieSessionRuntimeFactoryError
):
    """Raised when component construction fails after configuration is accepted."""


class DurableGenieSessionRuntimeClosedError(
    DurableGenieSessionRuntimeFactoryError
):
    """Raised when a closed runtime bundle is used in a way that requires it to be open."""


# ---------------------------------------------------------------------------
# Boolean helper
# ---------------------------------------------------------------------------

def _parse_bool(value: Optional[str], *, variable_name: str) -> bool:
    """Parse a strict boolean from an environment variable value.

    Supported true values:  true, 1, yes, on  (case-insensitive, stripped).
    Supported false values: false, 0, no, off, empty (case-insensitive, stripped).
    Unknown values raise a sanitized configuration error that does not echo
    the raw value.
    """
    normalized = (value or "").strip().lower()
    if normalized in _TRUTHY_VALUES:
        return True
    if normalized in _FALSEY_VALUES:
        return False
    raise DurableGenieSessionRuntimeConfigurationError(
        f"Invalid boolean value for '{variable_name}'. "
        "Supported values: true, false, 1, 0, yes, no, on, off."
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DurableGenieSessionRuntimeSettings:
    """Immutable runtime settings parsed from environment variables.

    Environment access occurs only inside from_environment(); importing this
    module does not read the environment.
    """

    enabled: bool

    @classmethod
    def from_environment(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "DurableGenieSessionRuntimeSettings":
        """Construct settings from environment variables.

        When environ is None, os.environ is used. The flag defaults to false.
        No PG-related variables are read here.
        """
        env: Mapping[str, str] = (
            environ if environ is not None else __import__("os").environ
        )
        raw = env.get(_ENABLE_ENV_VAR, "false")
        enabled = _parse_bool(raw, variable_name=_ENABLE_ENV_VAR)
        return cls(enabled=enabled)


# ---------------------------------------------------------------------------
# Cache store protocol
# ---------------------------------------------------------------------------

class CacheStoreProtocol:
    """Narrow structural contract for the optional compatibility cache store.

    Includes only the six methods actually called by
    _GenieSessionCompatibilityCache inside DurableGenieSessionAdapter.

    The caller must explicitly inject a GenieSessionStore-compatible instance.
    GenieSessionStore is never imported or constructed automatically by this
    module. Do not expose get_session() mutable objects through this layer.
    """

    def get_genie_conversation_id(self, cache_key: str) -> Optional[str]:
        ...

    def get_last_message_id(self, cache_key: str) -> Optional[str]:
        ...

    def set_genie_conversation_id(
        self, cache_key: str, genie_conversation_id: str
    ) -> None:
        ...

    def set_last_message_id(
        self, cache_key: str, last_genie_message_id: str
    ) -> None:
        ...

    def reset_genie_mapping(self, cache_key: str) -> None:
        ...

    def reset_session(self, cache_key: str) -> None:
        ...


# ---------------------------------------------------------------------------
# Runtime bundle
# ---------------------------------------------------------------------------

class DurableGenieSessionRuntimeBundle:
    """Lifecycle-managed bundle produced by DurableGenieSessionRuntimeFactory.

    Disabled bundle (enabled=False):
        adapter=None, backend=None, durable=False.
        close() is a safe no-op.

    Enabled bundle (enabled=True):
        adapter is the DurableGenieSessionAdapter instance.
        The adapter owns the ConversationRepositoryBundle lifecycle.
        close() closes the adapter exactly once.
        The runtime bundle must not separately close the repository bundle
        a second time.

    close() is idempotent. Context-manager support is provided.
    Repr exposes only safe structural metadata.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        adapter: Optional[Any],  # Optional[DurableGenieSessionAdapter]
        backend: Optional[Any],  # Optional[ConversationRepositoryBackend]
        durable: bool,
    ) -> None:
        self._enabled = enabled
        self._adapter = adapter
        self._backend = backend
        self._durable = durable
        self._closed = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def adapter(self) -> Optional[Any]:
        """The DurableGenieSessionAdapter, or None when disabled."""
        return self._adapter

    @property
    def backend(self) -> Optional[Any]:
        """The ConversationRepositoryBackend enum value, or None when disabled."""
        return self._backend

    @property
    def durable(self) -> bool:
        return self._durable

    def close(self) -> None:
        """Release owned resources. Idempotent.

        On the enabled path, closes the adapter exactly once. The adapter
        owns the repository bundle; the runtime bundle does not close the
        repository bundle directly.

        On the disabled path, this is a safe no-op.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._adapter is not None:
            self._adapter.close()

    def __enter__(self) -> "DurableGenieSessionRuntimeBundle":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        # Never expose: host, endpoint, owner identifiers, Genie identifiers,
        # credentials, tokens, or raw environment values.
        backend_val = (
            getattr(self._backend, "value", None)
            if self._backend is not None
            else None
        )
        return (
            "DurableGenieSessionRuntimeBundle("
            f"enabled={self._enabled!r}, "
            f"backend={backend_val!r}, "
            f"durable={self._durable!r}, "
            f"closed={self._closed!r}"
            ")"
        )


# ---------------------------------------------------------------------------
# Runtime factory
# ---------------------------------------------------------------------------

class DurableGenieSessionRuntimeFactory:
    """Compose ConversationRepositoryFactory and DurableGenieSessionAdapter
    behind the ENABLE_DURABLE_GENIE_SESSION_ADAPTER feature flag.

    Nothing is constructed during factory construction. Repository factory,
    repository bundle, and adapter are only instantiated inside create()
    when the flag is enabled.

    Constructor dependency injection:
        environ            — environment mapping (defaults to os.environ via settings).
        repository_factory_cls — ConversationRepositoryFactory class (or fake for tests).
        adapter_cls        — DurableGenieSessionAdapter class (or fake for tests).

    When repository_factory_cls or adapter_cls is None, the real class is
    imported lazily inside create() on the enabled path only.

    No module-level singleton is created.
    """

    def __init__(
        self,
        *,
        environ: Optional[Mapping[str, str]] = None,
        repository_factory_cls: Optional[Any] = None,
        adapter_cls: Optional[Any] = None,
    ) -> None:
        self._environ = environ
        self._repository_factory_cls = repository_factory_cls
        self._adapter_cls = adapter_cls

    def _resolve_repository_factory_cls(self) -> Any:
        """Return the injected class or lazily import the real one."""
        if self._repository_factory_cls is not None:
            return self._repository_factory_cls
        # Lazy runtime import — only reached on enabled path.
        from app.services.conversation_repository_factory import (  # noqa: PLC0415
            ConversationRepositoryFactory as _ConversationRepositoryFactory,
        )
        return _ConversationRepositoryFactory

    def _resolve_adapter_cls(self) -> Any:
        """Return the injected class or lazily import the real one."""
        if self._adapter_cls is not None:
            return self._adapter_cls
        # Lazy runtime import — only reached on enabled path.
        from app.services.durable_genie_session_adapter import (  # noqa: PLC0415
            DurableGenieSessionAdapter as _DurableGenieSessionAdapter,
        )
        return _DurableGenieSessionAdapter

    def create(
        self,
        *,
        cache_store: Optional[CacheStoreProtocol] = None,
    ) -> DurableGenieSessionRuntimeBundle:
        """Construct and return a runtime bundle.

        Disabled path:
            Returns a disabled bundle immediately. ConversationRepositoryFactory
            is not instantiated. Repository settings, PG environment variables,
            providers, pools, credentials, adapters, and SQL are never accessed.

        Enabled path:
            1. Instantiates ConversationRepositoryFactory with the same environ.
            2. Calls repository_factory.create() to obtain a repository bundle.
            3. Constructs DurableGenieSessionAdapter with the bundle and cache.
            4. Returns an enabled bundle with enabled=True.

        On repository-factory construction failure, raises a sanitized
        DurableGenieSessionRuntimeInitializationError.

        On adapter construction failure after the repository bundle exists,
        closes the repository bundle exactly once before raising.

        Never silently returns a disabled bundle after explicit enablement fails.
        Never silently falls back to memory beyond ConversationRepositoryFactory's
        own backend selection.
        """
        settings = DurableGenieSessionRuntimeSettings.from_environment(self._environ)

        # --- Disabled path ---------------------------------------------------
        if not settings.enabled:
            return DurableGenieSessionRuntimeBundle(
                enabled=False,
                adapter=None,
                backend=None,
                durable=False,
            )

        # --- Enabled path ----------------------------------------------------
        # Step 1: resolve and construct repository factory.
        repo_factory_cls = self._resolve_repository_factory_cls()
        try:
            repo_factory = repo_factory_cls(environ=self._environ)
        except DurableGenieSessionRuntimeFactoryError:
            raise
        except Exception as exc:
            raise DurableGenieSessionRuntimeInitializationError(
                "Failed to construct the conversation repository factory."
            ) from exc

        # Step 2: create repository bundle.
        try:
            repo_bundle = repo_factory.create()
        except DurableGenieSessionRuntimeFactoryError:
            raise
        except Exception as exc:
            raise DurableGenieSessionRuntimeInitializationError(
                "Failed to create the conversation repository bundle."
            ) from exc

        # Step 3: construct adapter. On failure, close the bundle exactly once.
        adapter_cls = self._resolve_adapter_cls()
        cache_enabled = cache_store is not None
        try:
            adapter = adapter_cls(
                repo_bundle,
                cache_store=cache_store,
                cache_enabled=cache_enabled,
            )
        except DurableGenieSessionRuntimeFactoryError:
            _safe_close(repo_bundle)
            raise
        except Exception as exc:
            _safe_close(repo_bundle)
            raise DurableGenieSessionRuntimeInitializationError(
                "Failed to construct the durable Genie session adapter."
            ) from exc

        # Step 4: return enabled bundle. Adapter owns repo_bundle lifecycle.
        return DurableGenieSessionRuntimeBundle(
            enabled=True,
            adapter=adapter,
            backend=repo_bundle.backend,
            durable=repo_bundle.durable,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def create_durable_genie_session_runtime(
    *,
    environ: Optional[Mapping[str, str]] = None,
    cache_store: Optional[CacheStoreProtocol] = None,
) -> DurableGenieSessionRuntimeBundle:
    """Create a fresh runtime factory and return a new bundle.

    No global singleton is created or modified. Disabled-by-default behaviour
    is preserved. No existing runtime module calls this function in Phase 3C.
    Each call creates an independent factory and bundle instance.
    """
    factory = DurableGenieSessionRuntimeFactory(environ=environ)
    return factory.create(cache_store=cache_store)


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _safe_close(resource: Any) -> None:
    """Best-effort close used during failed construction.

    Swallows all exceptions so that the original error is preserved.
    """
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass
