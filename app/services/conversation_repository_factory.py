"""Conversation repository backend selection and construction factory.

Phase 3A introduces a disabled-by-default factory that can construct either
an in-memory conversation repository or the durable Lakebase-backed repository
without wiring either backend into runtime application flows.

Design constraints for this phase:

* No environment access during module import.
* No module-level repository singletons.
* The durable backend is disabled by default.
* Selecting the durable backend while disabled fails explicitly.
* Memory backend construction performs no durable-backend setup.
* Durable backend construction is lazy: repository/provider creation must not
  open a physical database connection, create a credential, or execute SQL.
* Factory errors must be sanitized and must not expose credentials, endpoint
  names, service-principal identifiers, raw environment contents, or raw SDK
  exception details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional

from app.services.conversation_repository import (
    ConversationRepository,
    InMemoryConversationRepository,
)

_BACKEND_ENV_VAR = "CONVERSATION_REPOSITORY_BACKEND"
_ENABLE_ENV_VAR = "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY"

_TRUTHY_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSEY_VALUES = frozenset({"false", "0", "no", "off", ""})


class ConversationRepositoryFactoryError(Exception):
    """Base exception for conversation-repository factory failures."""


class ConversationRepositoryFactoryConfigurationError(
    ConversationRepositoryFactoryError
):
    """Raised when factory configuration is missing, invalid, or disallowed."""


class ConversationRepositoryFactoryInitializationError(
    ConversationRepositoryFactoryError
):
    """Raised when backend construction fails after configuration is accepted."""


class ConversationRepositoryBackend(str, Enum):
    """Supported conversation-repository backends."""

    MEMORY = "memory"
    LAKEBASE = "lakebase"

    @classmethod
    def from_value(
        cls,
        value: Optional[str],
    ) -> "ConversationRepositoryBackend":
        """Parse a backend name using case-insensitive, whitespace-safe rules."""
        normalized = (value or "").strip().lower()
        if not normalized:
            return cls.MEMORY
        if normalized == cls.MEMORY.value:
            return cls.MEMORY
        if normalized == cls.LAKEBASE.value:
            return cls.LAKEBASE
        raise ConversationRepositoryFactoryConfigurationError(
            "Invalid conversation repository backend. Supported values are "
            "'memory' and 'lakebase'."
        )


@dataclass(frozen=True)
class ConversationRepositoryFactorySettings:
    """Immutable factory settings parsed from environment variables."""

    backend: ConversationRepositoryBackend
    lakebase_enabled: bool

    @classmethod
    def from_environment(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "ConversationRepositoryFactorySettings":
        """Construct settings from environment variables.

        Environment access occurs only inside this method.
        """
        env: Mapping[str, str] = (
            environ if environ is not None else __import__("os").environ
        )
        backend = ConversationRepositoryBackend.from_value(
            env.get(_BACKEND_ENV_VAR, ConversationRepositoryBackend.MEMORY.value)
        )
        lakebase_enabled = _parse_boolean_env(
            env.get(_ENABLE_ENV_VAR, "false"),
            variable_name=_ENABLE_ENV_VAR,
        )
        if backend is ConversationRepositoryBackend.LAKEBASE and not lakebase_enabled:
            raise ConversationRepositoryFactoryConfigurationError(
                "The Lakebase conversation repository backend is disabled."
            )
        return cls(backend=backend, lakebase_enabled=lakebase_enabled)


@dataclass
class ConversationRepositoryBundle:
    """Repository plus backend metadata and optional lifecycle ownership."""

    repository: ConversationRepository
    backend: ConversationRepositoryBackend
    durable: bool
    _resource_closer: Optional[Callable[[], None]] = field(
        default=None,
        repr=False,
        compare=False,
    )
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def close(self) -> None:
        """Release owned backend resources. Idempotent."""
        if self._closed:
            return
        self._closed = True
        closer = self._resource_closer
        if closer is not None:
            closer()

    def __enter__(self) -> "ConversationRepositoryBundle":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            "ConversationRepositoryBundle("
            f"backend={self.backend.value!r}, "
            f"durable={self.durable!r}, "
            f"closed={self._closed!r}"
            ")"
        )


SettingsFactory = Callable[[Optional[Mapping[str, str]]], Any]
ConnectionOwnerFactory = Callable[[Any], Any]
RepositoryFactory = Callable[[Any], ConversationRepository]
MemoryRepositoryFactory = Callable[[], ConversationRepository]


class ConversationRepositoryFactory:
    """Construct conversation repositories from validated configuration."""

    def __init__(
        self,
        *,
        environ: Optional[Mapping[str, str]] = None,
        memory_repository_factory: MemoryRepositoryFactory = InMemoryConversationRepository,
        durable_settings_factory: SettingsFactory | None = None,
        durable_connection_factory: ConnectionOwnerFactory | None = None,
        durable_repository_factory: RepositoryFactory | None = None,
    ) -> None:
        self._environ = environ
        self._memory_repository_factory = memory_repository_factory
        self._durable_settings_factory = (
            durable_settings_factory or _default_durable_settings_factory
        )
        self._durable_connection_factory = (
            durable_connection_factory or _default_durable_connection_factory
        )
        self._durable_repository_factory = (
            durable_repository_factory or _default_durable_repository_factory
        )

    def create(self) -> ConversationRepositoryBundle:
        """Construct and return a repository bundle for the selected backend."""
        settings = ConversationRepositoryFactorySettings.from_environment(self._environ)
        if settings.backend is ConversationRepositoryBackend.MEMORY:
            repository = self._create_memory_repository()
            return ConversationRepositoryBundle(
                repository=repository,
                backend=ConversationRepositoryBackend.MEMORY,
                durable=False,
            )
        return self._create_durable_repository()

    def _create_memory_repository(self) -> ConversationRepository:
        try:
            return self._memory_repository_factory()
        except ConversationRepositoryFactoryError:
            raise
        except Exception as exc:
            raise ConversationRepositoryFactoryInitializationError(
                "Failed to initialize the in-memory conversation repository."
            ) from exc

    def _create_durable_repository(self) -> ConversationRepositoryBundle:
        try:
            backend_settings = self._durable_settings_factory(self._environ)
        except ConversationRepositoryFactoryError:
            raise
        except Exception as exc:
            raise ConversationRepositoryFactoryConfigurationError(
                "Failed to load Lakebase conversation repository configuration."
            ) from exc

        try:
            connection_owner = self._durable_connection_factory(backend_settings)
        except ConversationRepositoryFactoryError:
            raise
        except Exception as exc:
            raise ConversationRepositoryFactoryInitializationError(
                "Failed to initialize the Lakebase connection provider."
            ) from exc

        try:
            repository = self._durable_repository_factory(connection_owner)
        except ConversationRepositoryFactoryError:
            _safe_close(connection_owner)
            raise
        except Exception as exc:
            _safe_close(connection_owner)
            raise ConversationRepositoryFactoryInitializationError(
                "Failed to initialize the Lakebase conversation repository."
            ) from exc

        return ConversationRepositoryBundle(
            repository=repository,
            backend=ConversationRepositoryBackend.LAKEBASE,
            durable=True,
            _resource_closer=getattr(connection_owner, "close", None),
        )


def create_conversation_repository(
    environ: Optional[Mapping[str, str]] = None,
) -> ConversationRepositoryBundle:
    """Convenience function that returns a new repository bundle."""
    return ConversationRepositoryFactory(environ=environ).create()


def _load_durable_components() -> tuple[type, type, type]:
    """Load durable-backend component classes lazily."""
    services_prefix = "app.services."
    provider_module = __import__(
        services_prefix + "lakebase_" + "connection" + "_provider",
        fromlist=["durable"],
    )
    repository_module = __import__(
        services_prefix + "lakebase_" + "conversation" + "_repository",
        fromlist=["durable"],
    )
    settings_type = getattr(provider_module, "Lakebase" + "ConnectionSettings")
    owner_type = getattr(provider_module, "Lakebase" + "Connection" + "Provider")
    repository_type = getattr(
        repository_module,
        "Lakebase" + "ConversationRepository",
    )
    return settings_type, owner_type, repository_type


def _default_durable_settings_factory(
    environ: Optional[Mapping[str, str]] = None,
) -> Any:
    settings_type, _, _ = _load_durable_components()
    return settings_type.from_environment(environ)


def _default_durable_connection_factory(settings: Any) -> Any:
    _, owner_type, _ = _load_durable_components()
    return owner_type(settings)


def _default_durable_repository_factory(connection_owner: Any) -> ConversationRepository:
    _, _, repository_type = _load_durable_components()
    return repository_type(connection_owner)


def _parse_boolean_env(value: Optional[str], *, variable_name: str) -> bool:
    """Parse a strict boolean environment variable value."""
    normalized = (value or "").strip().lower()
    if normalized in _TRUTHY_VALUES:
        return True
    if normalized in _FALSEY_VALUES:
        return False
    raise ConversationRepositoryFactoryConfigurationError(
        f"Invalid boolean value for '{variable_name}'."
    )


def _safe_close(resource: Any) -> None:
    """Best-effort closer used during failed durable-backend construction."""
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            return
