"""Lakebase OAuth connection provider for TransparencE.

This module implements the connection-provider callable required by
LakebaseConversationRepository.  It manages a lazy psycopg_pool.ConnectionPool
whose physical connections authenticate via fresh OAuth tokens minted through
the Databricks SDK on every new physical PostgreSQL connection.

Design constraints (Phase 2C):

- Zero network I/O during module import.
- Zero network I/O during settings creation (from_environment()).
- Zero network I/O during provider construction (LakebaseConnectionProvider()).
- First real connection happens only when __call__() is invoked.
- A fresh OAuth database credential is requested for every new physical
  PostgreSQL connection created by the pool (not cached globally).
- search_path=transparence_state,public is set on every physical connection
  via the PostgreSQL options parameter.  It cannot be changed by callers.
- No password, token or DSN appears in logs, repr(), or exception messages.
- The provider is compatible with ConnectionContextProvider as defined in
  app/services/lakebase_conversation_repository.py.

Phase 2C -- TransparencE Genie State Persistence
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Generator, Mapping, Optional


__all__ = [
    # Settings
    "LakebaseConnectionSettings",
    # Errors
    "LakebaseConnectionProviderError",
    "LakebaseConfigurationError",
    "LakebaseCredentialError",
    "LakebasePoolUnavailableError",
    "LakebasePoolClosedError",
    # Provider
    "LakebaseConnectionProvider",
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The only permitted search_path value.  Cannot be supplied by callers.
_REQUIRED_SEARCH_PATH: str = "transparence_state,public"

# Structural pattern for a Lakebase endpoint resource name.
# Must match: projects/<x>/branches/<y>/endpoints/<z>
_ENDPOINT_RE = re.compile(
    r"^projects/[a-zA-Z0-9._-]+"
    r"/branches/[a-zA-Z0-9._-]+"
    r"/endpoints/[a-zA-Z0-9._-]+$"
)

# SSL modes that would disable encryption.
_INSECURE_SSL_MODES = frozenset({"disable", "allow", "prefer"})

# Pool configuration defaults.
_DEFAULT_POOL_MAX_SIZE: int = 5
_DEFAULT_ACQUIRE_TIMEOUT: float = 10.0     # seconds
_DEFAULT_CONNECT_TIMEOUT: int = 10         # seconds (passed as PostgreSQL GUC)

# Token lifetime for Lakebase OAuth credentials is 1 hour (3600 s).
# Recycle connections well below that ceiling to guarantee the next
# connect() call always mints a fresh token.
_DEFAULT_MAX_LIFETIME: float = 1800.0      # 30 minutes

# Idle lifetime appropriate for a scale-to-zero endpoint.
_DEFAULT_MAX_IDLE: float = 300.0           # 5 minutes


# ---------------------------------------------------------------------------
# Error Hierarchy
# ---------------------------------------------------------------------------


class LakebaseConnectionProviderError(Exception):
    """Base exception for all Lakebase connection-provider errors.

    Public message never exposes OAuth tokens, passwords, DSNs, database
    hosts, service-principal IDs, or endpoint resource names.
    """


class LakebaseConfigurationError(LakebaseConnectionProviderError):
    """Raised when required environment variables are missing or invalid."""


class LakebaseCredentialError(LakebaseConnectionProviderError):
    """Raised when OAuth credential generation fails."""


class LakebasePoolUnavailableError(LakebaseConnectionProviderError):
    """Raised when the connection pool cannot be opened or a connection
    cannot be obtained from it."""


class LakebasePoolClosedError(LakebaseConnectionProviderError):
    """Raised when the provider has been explicitly closed and further
    use is attempted."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LakebaseConnectionSettings:
    """Immutable connection settings for a Lakebase endpoint.

    All fields are validated on construction.  No password or OAuth token
    is stored here.  Credentials are minted at connection time by the
    provider.

    Do not construct this directly in application code; use
    ``from_environment()`` to read from the process environment.
    """

    host: str
    database: str
    port: int
    user: str
    sslmode: str
    endpoint_name: str
    application_name: Optional[str]
    search_path: str = field(default=_REQUIRED_SEARCH_PATH)

    def __post_init__(self) -> None:
        # Non-empty string checks
        for attr in ("host", "database", "user", "sslmode", "endpoint_name"):
            val = getattr(self, attr)
            if not isinstance(val, str) or not val.strip():
                raise LakebaseConfigurationError(
                    f"Connection setting '{attr}' must be a non-empty string."
                )

        # Port range
        if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise LakebaseConfigurationError(
                f"Connection setting 'port' must be an integer in [1, 65535]."
            )

        # SSL must not silently downgrade
        if self.sslmode.lower() in _INSECURE_SSL_MODES:
            raise LakebaseConfigurationError(
                "Connection setting 'sslmode' must not disable or weaken TLS. "
                "Use 'require', 'verify-ca', or 'verify-full'."
            )

        # Endpoint structural check
        if not _ENDPOINT_RE.match(self.endpoint_name):
            raise LakebaseConfigurationError(
                "Connection setting 'endpoint_name' does not match the expected "
                "'projects/.../branches/.../endpoints/...' pattern."
            )

        # search_path is fixed — no user-supplied arbitrary value
        if self.search_path != _REQUIRED_SEARCH_PATH:
            raise LakebaseConfigurationError(
                f"Connection setting 'search_path' must be "
                f"'{_REQUIRED_SEARCH_PATH}'."
            )

    def __repr__(self) -> str:
        # Intentionally omits password/token (there is none stored here)
        # but also avoids exposing host or endpoint in repr to reduce the
        # blast radius of accidental logging.
        return (
            f"LakebaseConnectionSettings("
            f"database={self.database!r}, "
            f"port={self.port!r}, "
            f"sslmode={self.sslmode!r}, "
            f"search_path={self.search_path!r}"
            f")"
        )

    @classmethod
    def from_environment(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "LakebaseConnectionSettings":
        """Construct settings from the process environment.

        Environment access occurs only here, not during module import or
        during provider construction.

        Required variables
        ------------------
        PGHOST            PostgreSQL host.
        PGDATABASE        PostgreSQL database name.
        PGPORT            PostgreSQL port (integer string).
        PGUSER            PostgreSQL user (the SP role name).
        PGSSLMODE         SSL mode; must be 'require', 'verify-ca', or
                          'verify-full'.
        LAKEBASE_ENDPOINT_NAME
                          Lakebase endpoint resource name
                          (projects/.../branches/.../endpoints/...).

        Optional variables
        ------------------
        PGAPPNAME         Application name (visible in pg_stat_activity).

        Raises
        ------
        LakebaseConfigurationError
            If any required variable is missing or invalid.
        """
        env: Mapping[str, str] = environ if environ is not None else __import__("os").environ

        def _require(key: str) -> str:
            val = env.get(key, "").strip()
            if not val:
                raise LakebaseConfigurationError(
                    f"Required environment variable '{key}' is missing or empty."
                )
            return val

        host = _require("PGHOST")
        database = _require("PGDATABASE")
        user = _require("PGUSER")
        sslmode = _require("PGSSLMODE")
        endpoint_name = _require("LAKEBASE_ENDPOINT_NAME")

        port_str = _require("PGPORT")
        try:
            port = int(port_str)
        except ValueError:
            raise LakebaseConfigurationError(
                f"Environment variable 'PGPORT' must be an integer; got: "
                f"{port_str!r}"
            )

        application_name: Optional[str] = env.get("PGAPPNAME", "").strip() or None

        return cls(
            host=host,
            database=database,
            port=port,
            user=user,
            sslmode=sslmode,
            endpoint_name=endpoint_name,
            application_name=application_name,
        )


# ---------------------------------------------------------------------------
# OAuth-aware connection class factory
# ---------------------------------------------------------------------------


def _build_oauth_connection_class(
    *,
    base_connection_class: type,
    credential_supplier: Callable[[str], Any],
    endpoint_name: str,
) -> type:
    """Return a connection subclass that injects a fresh OAuth credential.

    One fresh credential is requested for each new physical connection. Only
    ``credential.token`` is passed onward as the password argument, and neither
    the credential nor token is retained after connect() returns.
    """

    class OAuthConnection(base_connection_class):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs: Any) -> Any:
            try:
                credential = credential_supplier(endpoint_name)
                token = credential.token
            except LakebaseConnectionProviderError:
                raise
            except Exception as exc:
                log.warning("Lakebase: credential request failed.")
                raise LakebaseCredentialError(
                    "Failed to obtain a database credential."
                ) from exc

            if not isinstance(token, str) or not token:
                raise LakebaseCredentialError(
                    "Failed to obtain a database credential."
                )

            kwargs = dict(kwargs)
            kwargs["password"] = token
            try:
                return super().connect(conninfo, **kwargs)
            except LakebaseConnectionProviderError:
                raise
            except Exception as exc:
                raise LakebasePoolUnavailableError(
                    "Could not establish a database connection."
                ) from exc
            finally:
                credential = None
                token = None

    OAuthConnection.__name__ = f"OAuth{base_connection_class.__name__}"
    return OAuthConnection


# ---------------------------------------------------------------------------
# Connection Provider
# ---------------------------------------------------------------------------


class LakebaseConnectionProvider:
    """Callable connection provider for LakebaseConversationRepository.

    Compatible with the ``ConnectionContextProvider`` type alias::

        Callable[[], ContextManager[ConnectionLike]]

    Usage
    -----
    ::

        settings = LakebaseConnectionSettings.from_environment()
        provider = LakebaseConnectionProvider(settings)

        with provider() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

        # Shutdown
        provider.close()

    The pool is opened lazily on the first call to __call__().  Construction
    makes no network requests, opens no connections, and creates no
    WorkspaceClient.

    Parameters
    ----------
    settings:
        Validated connection settings.
    workspace_client_factory:
        Zero-argument callable that returns a WorkspaceClient.  Defaults to
        ``WorkspaceClient`` from ``databricks.sdk``.  Override for unit tests.
    pool_factory:
        Callable used to create the pool.  Defaults to
        ``psycopg_pool.ConnectionPool``.  Override for unit tests.
    max_size:
        Maximum number of pooled connections.
    acquisition_timeout:
        Seconds to wait for an available connection before raising
        LakebasePoolUnavailableError.
    """

    def __init__(
        self,
        settings: LakebaseConnectionSettings,
        *,
        workspace_client_factory: Optional[Callable[[], Any]] = None,
        pool_factory: Optional[Callable[..., Any]] = None,
        base_connection_class: Optional[type] = None,
        max_size: int = _DEFAULT_POOL_MAX_SIZE,
        acquisition_timeout: float = _DEFAULT_ACQUIRE_TIMEOUT,
        connect_timeout: int = _DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        if max_size < 1:
            raise LakebaseConfigurationError("Pool max_size must be at least 1.")
        if acquisition_timeout <= 0:
            raise LakebaseConfigurationError(
                "Pool acquisition timeout must be positive."
            )
        if connect_timeout <= 0:
            raise LakebaseConfigurationError(
                "PostgreSQL connect_timeout must be positive."
            )

        self._settings = settings
        self._wc_factory = workspace_client_factory  # None means use real SDK
        self._pool_factory = pool_factory             # None means use real pool
        self._base_connection_class = base_connection_class
        self._max_size = max_size
        self._acquisition_timeout = acquisition_timeout
        self._connect_timeout = connect_timeout

        # Pool state — all protected by _lock
        self._pool: Optional[Any] = None
        self._closed: bool = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _credential_supplier(self, endpoint_name: str) -> Any:
        """Generate one fresh database credential for the given endpoint."""
        try:
            if self._wc_factory is not None:
                client = self._wc_factory()
            else:
                from databricks.sdk import WorkspaceClient  # noqa: PLC0415

                client = WorkspaceClient()
            return client.postgres.generate_database_credential(endpoint=endpoint_name)
        except LakebaseConnectionProviderError:
            raise
        except Exception as exc:
            log.warning("Lakebase: credential request failed.")
            raise LakebaseCredentialError(
                "Failed to obtain a database credential."
            ) from exc

    def _build_conninfo(self) -> str:
        """Return the static conninfo string without credentials."""
        return ""

    def _build_connect_kwargs(self) -> dict[str, Any]:
        """Return libpq connection kwargs with no password/token."""
        s = self._settings
        kwargs: dict[str, Any] = {
            "host": s.host,
            "dbname": s.database,
            "port": s.port,
            "user": s.user,
            "sslmode": s.sslmode,
            "connect_timeout": self._connect_timeout,
            "options": f"-c search_path={s.search_path}",
        }
        if s.application_name:
            kwargs["application_name"] = s.application_name
        return kwargs

    def _open_pool(self) -> Any:
        """Create and open the connection pool. Called at most once."""
        if self._pool_factory is not None:
            pool_factory = self._pool_factory
        else:
            from psycopg_pool import ConnectionPool  # noqa: PLC0415

            pool_factory = ConnectionPool

        if self._base_connection_class is not None:
            base_connection_class = self._base_connection_class
        else:
            import psycopg  # noqa: PLC0415

            base_connection_class = psycopg.Connection

        oauth_connection_class = _build_oauth_connection_class(
            base_connection_class=base_connection_class,
            credential_supplier=self._credential_supplier,
            endpoint_name=self._settings.endpoint_name,
        )

        kwargs: dict[str, Any] = {
            "connection_class": oauth_connection_class,
            "kwargs": self._build_connect_kwargs(),
            "min_size": 0,
            "max_size": self._max_size,
            "timeout": self._acquisition_timeout,
            "max_lifetime": _DEFAULT_MAX_LIFETIME,
            "max_idle": _DEFAULT_MAX_IDLE,
            "open": False,
        }

        pool_params = set(inspect.signature(pool_factory).parameters)
        if "conninfo" in pool_params:
            kwargs["conninfo"] = self._build_conninfo()
        if "check" in pool_params and hasattr(pool_factory, "check_connection"):
            kwargs["check"] = pool_factory.check_connection

        try:
            pool = pool_factory(**kwargs)
            pool.open(wait=False)
            log.info("Lakebase: pool initialized.")
            return pool
        except LakebaseConnectionProviderError:
            raise
        except Exception as exc:
            log.warning("Lakebase: pool initialization failed.")
            raise LakebasePoolUnavailableError(
                "The connection pool could not be initialized."
            ) from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True after close() has been called."""
        return self._closed

    @contextmanager
    def __call__(self) -> Generator[Any, None, None]:
        """Obtain a pooled connection as a context manager.

        Compatible with ConnectionContextProvider.

        Yields
        ------
        A psycopg.Connection-like object with cursor(), commit(), rollback().

        Raises
        ------
        LakebasePoolClosedError
            If the provider has been explicitly closed.
        LakebasePoolUnavailableError
            If the pool cannot be opened or a connection cannot be obtained.
        """
        if self._closed:
            raise LakebasePoolClosedError(
                "The connection provider has been closed and cannot be used."
            )

        pool = self._get_or_create_pool()

        try:
            with pool.connection(timeout=self._acquisition_timeout) as conn:
                yield conn
        except LakebaseConnectionProviderError:
            raise
        except Exception as exc:
            log.warning("Lakebase: connection acquisition failed.")
            raise LakebasePoolUnavailableError(
                "Could not obtain a database connection."
            ) from exc

    def _get_or_create_pool(self) -> Any:
        """Thread-safe lazy pool initialisation.  At most one pool is ever
        created; concurrent first callers wait on the lock."""
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._closed:
                raise LakebasePoolClosedError(
                    "The connection provider has been closed and cannot be used."
                )
            if self._pool is None:
                try:
                    self._pool = self._open_pool()
                except LakebaseConnectionProviderError:
                    raise
                except Exception as exc:
                    log.warning("Lakebase: pool initialization failed.")
                    raise LakebasePoolUnavailableError(
                        "The connection pool could not be initialized."
                    ) from exc
        return self._pool

    def close(self) -> None:
        """Close the pool and release all connections.  Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pool = self._pool
            self._pool = None

        if pool is not None:
            try:
                pool.close()
                log.info("Lakebase: connection pool closed.")
            except Exception:
                # Swallow close errors — the pool is being torn down.
                log.warning("Lakebase: error while closing pool (ignored).")

    # Context-manager support for the provider itself (optional).
    def __enter__(self) -> "LakebaseConnectionProvider":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        # No token, no host, no endpoint name in repr.
        return (
            f"LakebaseConnectionProvider("
            f"closed={self._closed!r}, "
            f"max_size={self._max_size!r}"
            f")"
        )
