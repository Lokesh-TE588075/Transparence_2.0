"""Tests for the Phase 2C Lakebase OAuth connection provider.

These tests use only fakes and mocks. They never open sockets, never connect
to Lakebase, and never execute SQL.
"""

from __future__ import annotations

import ast
import importlib
import logging
import pathlib
import sys
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

from app.services.lakebase_connection_provider import (
    LakebaseConfigurationError,
    LakebaseConnectionProvider,
    LakebaseConnectionSettings,
    LakebaseCredentialError,
    LakebasePoolClosedError,
    LakebasePoolUnavailableError,
    _build_oauth_connection_class,
)
from app.services.lakebase_conversation_repository import (
    LakebaseConversationRepository,
)


VALID_ENV = {
    "PGHOST": "lakebase-host.example.com",
    "PGDATABASE": "databricks_postgres",
    "PGPORT": "5432",
    "PGUSER": "488a0acb-5804-42f0-98b1-a02cc13c4573",
    "PGSSLMODE": "require",
    "LAKEBASE_ENDPOINT_NAME": "projects/transparence-sessions/branches/production/endpoints/primary",
    "PGAPPNAME": "transparence-tests",
}


@dataclass
class FakeCredential:
    token: str


class FakePostgresAPI:
    def __init__(self, *, token_prefix: str = "token", exc: Optional[Exception] = None) -> None:
        self.token_prefix = token_prefix
        self.exc = exc
        self.calls: List[str] = []
        self.counter = 0

    def generate_database_credential(self, *, endpoint: str) -> FakeCredential:
        self.calls.append(endpoint)
        if self.exc is not None:
            raise self.exc
        self.counter += 1
        return FakeCredential(token=f"{self.token_prefix}-{self.counter}")


class FakeWorkspaceClient:
    instances_created = 0

    def __init__(self, postgres_api: Optional[FakePostgresAPI] = None) -> None:
        type(self).instances_created += 1
        self.postgres = postgres_api or FakePostgresAPI()


class FakeBaseConnection:
    connect_calls: List[Dict[str, Any]] = []

    @classmethod
    def reset(cls) -> None:
        cls.connect_calls = []

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs: Any) -> Dict[str, Any]:
        payload = {"conninfo": conninfo, "kwargs": dict(kwargs)}
        cls.connect_calls.append(payload)
        return payload


class FakePooledConnection:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def cursor(self) -> Any:
        raise AssertionError("SQL execution is out of scope for these tests")

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class FakeConnectionContext:
    def __init__(self, connection: FakePooledConnection) -> None:
        self.connection_obj = connection

    def __enter__(self) -> FakePooledConnection:
        self.connection_obj.entered = True
        return self.connection_obj

    def __exit__(self, *args: Any) -> None:
        self.connection_obj.exited = True


class FakePool:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)
        self.open_calls: List[bool] = []
        self.connection_timeouts: List[float] = []
        self.close_calls = 0
        self._closed = False
        self.connection_exc: Optional[Exception] = None
        self.connection_obj = FakePooledConnection()

    @staticmethod
    def check_connection(connection: Any) -> None:
        return None

    def open(self, wait: bool = False) -> None:
        self.open_calls.append(wait)

    def connection(self, timeout: Optional[float] = None) -> FakeConnectionContext:
        self.connection_timeouts.append(timeout)
        if self.connection_exc is not None:
            raise self.connection_exc
        return FakeConnectionContext(self.connection_obj)

    def close(self) -> None:
        self.close_calls += 1
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


class CapturingPoolFactory:
    def __init__(self, pool_cls: type = FakePool) -> None:
        self.pool_cls = pool_cls
        self.created_pools: List[FakePool] = []
        self.last_kwargs: Optional[Dict[str, Any]] = None
        self.calls = 0

    @staticmethod
    def check_connection(connection: Any) -> None:
        return None

    def __call__(
        self,
        conninfo: str = "",
        *,
        connection_class: type,
        kwargs: Optional[Dict[str, Any]] = None,
        min_size: int = 0,
        max_size: Optional[int] = None,
        open: Optional[bool] = None,
        configure: Any = None,
        check: Any = None,
        reset: Any = None,
        name: Optional[str] = None,
        close_returns: bool = False,
        timeout: float = 30.0,
        max_waiting: int = 0,
        max_lifetime: float = 3600.0,
        max_idle: float = 600.0,
        reconnect_timeout: float = 300.0,
        reconnect_failed: Any = None,
        num_workers: int = 3,
    ) -> FakePool:
        self.calls += 1
        self.last_kwargs = {
            "conninfo": conninfo,
            "connection_class": connection_class,
            "kwargs": dict(kwargs or {}),
            "min_size": min_size,
            "max_size": max_size,
            "open": open,
            "timeout": timeout,
            "max_lifetime": max_lifetime,
            "max_idle": max_idle,
            "check": check,
        }
        pool = self.pool_cls(**self.last_kwargs)
        self.created_pools.append(pool)
        return pool


class NoCheckPoolFactory:
    def __init__(self) -> None:
        self.last_kwargs: Optional[Dict[str, Any]] = None

    def __call__(
        self,
        conninfo: str = "",
        *,
        connection_class: type,
        kwargs: Optional[Dict[str, Any]] = None,
        min_size: int = 0,
        max_size: Optional[int] = None,
        open: Optional[bool] = None,
        timeout: float = 30.0,
        max_lifetime: float = 3600.0,
        max_idle: float = 600.0,
    ) -> FakePool:
        self.last_kwargs = {
            "conninfo": conninfo,
            "connection_class": connection_class,
            "kwargs": dict(kwargs or {}),
            "min_size": min_size,
            "max_size": max_size,
            "open": open,
            "timeout": timeout,
            "max_lifetime": max_lifetime,
            "max_idle": max_idle,
        }
        return FakePool(**self.last_kwargs)


class ExplodingEnviron(dict):
    def get(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("environment should not be read during import")


def make_settings(**overrides: Any) -> LakebaseConnectionSettings:
    env = dict(VALID_ENV)
    env.update(overrides)
    return LakebaseConnectionSettings.from_environment(env)


def provider_with_fakes(
    *,
    settings: Optional[LakebaseConnectionSettings] = None,
    postgres_api: Optional[FakePostgresAPI] = None,
    pool_factory: Optional[Any] = None,
) -> tuple[LakebaseConnectionProvider, FakePostgresAPI, Any]:
    settings = settings or make_settings()
    postgres_api = postgres_api or FakePostgresAPI()
    factory = pool_factory or CapturingPoolFactory()

    def wc_factory() -> FakeWorkspaceClient:
        return FakeWorkspaceClient(postgres_api=postgres_api)

    provider = LakebaseConnectionProvider(
        settings,
        workspace_client_factory=wc_factory,
        pool_factory=factory,
        base_connection_class=FakeBaseConnection,
    )
    return provider, postgres_api, factory


def module_source() -> str:
    return pathlib.Path(
        "app/services/lakebase_connection_provider.py"
    ).read_text(encoding="utf-8")


def clear_provider_module() -> None:
    sys.modules.pop("app.services.lakebase_connection_provider", None)


def test_valid_environment_produces_settings() -> None:
    settings = LakebaseConnectionSettings.from_environment(VALID_ENV)
    assert settings.host == VALID_ENV["PGHOST"]
    assert settings.database == VALID_ENV["PGDATABASE"]
    assert settings.port == 5432
    assert settings.user == VALID_ENV["PGUSER"]
    assert settings.sslmode == "require"
    assert settings.endpoint_name == VALID_ENV["LAKEBASE_ENDPOINT_NAME"]
    assert settings.application_name == "transparence-tests"


def test_missing_pghost_rejected() -> None:
    env = dict(VALID_ENV)
    env.pop("PGHOST")
    with pytest.raises(LakebaseConfigurationError):
        LakebaseConnectionSettings.from_environment(env)


def test_missing_pgdatabase_rejected() -> None:
    env = dict(VALID_ENV)
    env.pop("PGDATABASE")
    with pytest.raises(LakebaseConfigurationError):
        LakebaseConnectionSettings.from_environment(env)


def test_missing_pguser_rejected() -> None:
    env = dict(VALID_ENV)
    env.pop("PGUSER")
    with pytest.raises(LakebaseConfigurationError):
        LakebaseConnectionSettings.from_environment(env)


def test_missing_endpoint_rejected() -> None:
    env = dict(VALID_ENV)
    env.pop("LAKEBASE_ENDPOINT_NAME")
    with pytest.raises(LakebaseConfigurationError):
        LakebaseConnectionSettings.from_environment(env)


def test_invalid_port_rejected() -> None:
    env = dict(VALID_ENV)
    env["PGPORT"] = "not-an-int"
    with pytest.raises(LakebaseConfigurationError):
        LakebaseConnectionSettings.from_environment(env)


def test_empty_values_rejected() -> None:
    env = dict(VALID_ENV)
    env["PGHOST"] = "  "
    with pytest.raises(LakebaseConfigurationError):
        LakebaseConnectionSettings.from_environment(env)


def test_insecure_sslmode_rejected() -> None:
    env = dict(VALID_ENV)
    env["PGSSLMODE"] = "disable"
    with pytest.raises(LakebaseConfigurationError):
        LakebaseConnectionSettings.from_environment(env)


def test_malformed_endpoint_rejected() -> None:
    env = dict(VALID_ENV)
    env["LAKEBASE_ENDPOINT_NAME"] = "not-an-endpoint"
    with pytest.raises(LakebaseConfigurationError):
        LakebaseConnectionSettings.from_environment(env)


def test_approved_search_path_is_fixed() -> None:
    with pytest.raises(LakebaseConfigurationError):
        LakebaseConnectionSettings(
            host="h",
            database="d",
            port=5432,
            user="u",
            sslmode="require",
            endpoint_name=VALID_ENV["LAKEBASE_ENDPOINT_NAME"],
            application_name=None,
            search_path="public",
        )


def test_settings_repr_contains_no_credential() -> None:
    settings = make_settings()
    rendered = repr(settings)
    assert "token" not in rendered.lower()
    assert "password" not in rendered.lower()
    assert settings.host not in rendered
    assert settings.endpoint_name not in rendered


def test_import_reads_no_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_module()
    import os

    monkeypatch.setattr(os, "environ", ExplodingEnviron())
    mod = importlib.import_module("app.services.lakebase_connection_provider")
    assert hasattr(mod, "LakebaseConnectionProvider")


def test_construction_opens_no_connection() -> None:
    FakeBaseConnection.reset()
    provider, _postgres, _factory = provider_with_fakes()
    assert FakeBaseConnection.connect_calls == []
    assert provider.closed is False


def test_construction_creates_no_credential() -> None:
    provider, postgres, _factory = provider_with_fakes()
    assert postgres.calls == []
    assert provider is not None


def test_construction_creates_no_workspaceclient() -> None:
    FakeWorkspaceClient.instances_created = 0
    provider, _postgres, _factory = provider_with_fakes()
    assert provider is not None
    assert FakeWorkspaceClient.instances_created == 0


def test_construction_creates_no_pool() -> None:
    provider, _postgres, factory = provider_with_fakes()
    assert provider is not None
    assert factory.calls == 0


def test_provider_is_callable() -> None:
    provider, _postgres, _factory = provider_with_fakes()
    assert callable(provider)


def test_credential_requested_with_exact_endpoint() -> None:
    provider, postgres, factory = provider_with_fakes()
    with provider():
        pass
    connection_class = factory.last_kwargs["connection_class"]
    FakeBaseConnection.reset()
    connection_class.connect("", **factory.last_kwargs["kwargs"])
    assert postgres.calls == [VALID_ENV["LAKEBASE_ENDPOINT_NAME"]]


def test_fresh_credential_requested_for_each_physical_connection() -> None:
    provider, postgres, factory = provider_with_fakes()
    with provider():
        pass
    connection_class = factory.last_kwargs["connection_class"]
    FakeBaseConnection.reset()
    connection_class.connect("", **factory.last_kwargs["kwargs"])
    connection_class.connect("", **factory.last_kwargs["kwargs"])
    assert postgres.calls == [
        VALID_ENV["LAKEBASE_ENDPOINT_NAME"],
        VALID_ENV["LAKEBASE_ENDPOINT_NAME"],
    ]


def test_credential_is_supplied_as_password() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    connection_class = factory.last_kwargs["connection_class"]
    FakeBaseConnection.reset()
    result = connection_class.connect("", **factory.last_kwargs["kwargs"])
    assert result["kwargs"]["password"].startswith("token-")


def test_credential_is_not_retained() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    connection_class = factory.last_kwargs["connection_class"]
    FakeBaseConnection.reset()
    connection_class.connect("", **factory.last_kwargs["kwargs"])
    assert not hasattr(connection_class, "token")
    assert not hasattr(provider, "token")
    assert "password" not in repr(provider)


def test_sdk_failure_translated_safely() -> None:
    provider, _postgres, factory = provider_with_fakes(
        postgres_api=FakePostgresAPI(exc=RuntimeError("secret-token endpoint host spid"))
    )
    with provider():
        pass
    connection_class = factory.last_kwargs["connection_class"]
    with pytest.raises(LakebaseCredentialError) as excinfo:
        connection_class.connect("", **factory.last_kwargs["kwargs"])
    assert str(excinfo.value) == "Failed to obtain a database credential."


def test_token_absent_from_error() -> None:
    provider, _postgres, factory = provider_with_fakes(
        postgres_api=FakePostgresAPI(exc=RuntimeError("token-9999 should not leak"))
    )
    with provider():
        pass
    connection_class = factory.last_kwargs["connection_class"]
    with pytest.raises(LakebaseCredentialError) as excinfo:
        connection_class.connect("", **factory.last_kwargs["kwargs"])
    assert "token-9999" not in str(excinfo.value)


def test_token_absent_from_repr() -> None:
    provider, _postgres, _factory = provider_with_fakes()
    rendered = repr(provider)
    assert "token" not in rendered.lower()
    assert "password" not in rendered.lower()


def test_endpoint_absent_from_public_error() -> None:
    provider, _postgres, factory = provider_with_fakes(
        postgres_api=FakePostgresAPI(exc=RuntimeError(VALID_ENV["LAKEBASE_ENDPOINT_NAME"]))
    )
    with provider():
        pass
    connection_class = factory.last_kwargs["connection_class"]
    with pytest.raises(LakebaseCredentialError) as excinfo:
        connection_class.connect("", **factory.last_kwargs["kwargs"])
    assert VALID_ENV["LAKEBASE_ENDPOINT_NAME"] not in str(excinfo.value)


def test_lazy_pool_creation() -> None:
    provider, _postgres, factory = provider_with_fakes()
    assert factory.calls == 0
    with provider():
        pass
    assert factory.calls == 1


def test_min_size_zero() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    assert factory.last_kwargs["min_size"] == 0


def test_bounded_max_size() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    assert factory.last_kwargs["max_size"] == 5


def test_acquisition_timeout_configured() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    assert factory.last_kwargs["timeout"] == 10.0
    assert factory.created_pools[0].connection_timeouts == [10.0]


def test_connect_timeout_configured() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    assert factory.last_kwargs["kwargs"]["connect_timeout"] == 10


def test_sslmode_configured() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    assert factory.last_kwargs["kwargs"]["sslmode"] == "require"


def test_database_user_host_port_configured() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    pool_kwargs = factory.last_kwargs["kwargs"]
    assert pool_kwargs["dbname"] == VALID_ENV["PGDATABASE"]
    assert pool_kwargs["user"] == VALID_ENV["PGUSER"]
    assert pool_kwargs["host"] == VALID_ENV["PGHOST"]
    assert pool_kwargs["port"] == 5432


def test_no_static_password_in_conninfo() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    assert factory.last_kwargs["conninfo"] == ""
    assert "password" not in factory.last_kwargs["kwargs"]
    assert "token" not in factory.last_kwargs["conninfo"].lower()


def test_search_path_option_configured() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    assert (
        factory.last_kwargs["kwargs"]["options"]
        == "-c search_path=transparence_state,public"
    )


def test_application_name_configured_when_supplied() -> None:
    provider, _postgres, factory = provider_with_fakes(settings=make_settings())
    with provider():
        pass
    assert factory.last_kwargs["kwargs"]["application_name"] == "transparence-tests"


def test_only_one_pool_created_under_concurrent_first_access() -> None:
    provider, _postgres, factory = provider_with_fakes()
    errors: List[Exception] = []
    barrier = threading.Barrier(10)

    def worker() -> None:
        try:
            barrier.wait()
            with provider():
                pass
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert factory.calls == 1


def test_call_returns_pool_connection_context() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider() as connection:
        assert isinstance(connection, FakePooledConnection)
    assert factory.created_pools[0].connection_obj.entered is True


def test_connection_context_exits_correctly() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    assert factory.created_pools[0].connection_obj.exited is True


def test_close_closes_pool() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    provider.close()
    assert factory.created_pools[0].close_calls == 1
    assert provider.closed is True


def test_repeat_close_is_harmless() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    provider.close()
    provider.close()
    assert factory.created_pools[0].close_calls == 1


def test_use_after_close_rejected() -> None:
    provider, _postgres, _factory = provider_with_fakes()
    provider.close()
    with pytest.raises(LakebasePoolClosedError):
        with provider():
            pass


def test_pool_error_translated_safely() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    factory.created_pools[0].connection_exc = RuntimeError("host token spid dsn")
    with pytest.raises(LakebasePoolUnavailableError) as excinfo:
        with provider():
            pass
    assert str(excinfo.value) == "Could not obtain a database connection."


def test_close_before_first_use_succeeds() -> None:
    provider, _postgres, factory = provider_with_fakes()
    provider.close()
    assert provider.closed is True
    assert factory.calls == 0


def test_provider_context_manager_closes_pool() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider as managed:
        with managed():
            pass
    assert factory.created_pools[0].close_calls == 1


def test_no_token_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    provider, _postgres, factory = provider_with_fakes(
        postgres_api=FakePostgresAPI(exc=RuntimeError("token-12345"))
    )
    with provider():
        pass
    connection_class = factory.last_kwargs["connection_class"]
    with pytest.raises(LakebaseCredentialError):
        connection_class.connect("", **factory.last_kwargs["kwargs"])
    assert "token-12345" not in caplog.text


def test_no_dsn_logged(caplog: pytest.LogCaptureFixture) -> None:
    class FailingPoolFactory(CapturingPoolFactory):
        def __call__(self, *args: Any, **kwargs: Any) -> FakePool:
            raise RuntimeError("postgresql://user:pw@host/db")

    caplog.set_level(logging.WARNING)
    provider, _postgres, _factory = provider_with_fakes(pool_factory=FailingPoolFactory())
    with pytest.raises(LakebasePoolUnavailableError):
        with provider():
            pass
    assert "postgresql://" not in caplog.text


def test_no_host_in_public_exception() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    factory.created_pools[0].connection_exc = RuntimeError(VALID_ENV["PGHOST"])
    with pytest.raises(LakebasePoolUnavailableError) as excinfo:
        with provider():
            pass
    assert VALID_ENV["PGHOST"] not in str(excinfo.value)


def test_no_service_principal_id_in_public_exception() -> None:
    provider, _postgres, factory = provider_with_fakes()
    with provider():
        pass
    spid = VALID_ENV["PGUSER"]
    factory.created_pools[0].connection_exc = RuntimeError(spid)
    with pytest.raises(LakebasePoolUnavailableError) as excinfo:
        with provider():
            pass
    assert spid not in str(excinfo.value)


def test_no_environment_access_during_import(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_module()
    import os

    monkeypatch.setattr(os, "environ", ExplodingEnviron())
    mod = importlib.import_module("app.services.lakebase_connection_provider")
    assert hasattr(mod, "LakebaseConnectionSettings")


def test_no_network_access_during_import(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_module()
    real_import = __import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("databricks") or name.startswith("psycopg"):
            raise AssertionError(f"unexpected import during module import: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    mod = importlib.import_module("app.services.lakebase_connection_provider")
    assert hasattr(mod, "LakebaseConnectionProvider")


def test_no_network_access_during_construction() -> None:
    provider, postgres, factory = provider_with_fakes()
    assert provider is not None
    assert postgres.calls == []
    assert factory.calls == 0


def test_provider_return_contract_is_compatible_with_repository() -> None:
    provider, _postgres, _factory = provider_with_fakes()
    repo = LakebaseConversationRepository(connection_provider=provider)
    assert repo is not None
    with provider() as connection:
        assert hasattr(connection, "cursor")
        assert hasattr(connection, "commit")
        assert hasattr(connection, "rollback")


def test_module_uses_psycopg3_not_psycopg2() -> None:
    source = module_source()
    assert "psycopg2" not in source
    assert "import psycopg" in source


def test_module_uses_psycopg_pool() -> None:
    source = module_source()
    assert "psycopg_pool" in source


def test_no_sqlalchemy_import() -> None:
    source = module_source()
    assert "sqlalchemy" not in source.lower()


def test_no_runtime_module_imports_or_instantiates_provider_yet() -> None:
    root = pathlib.Path("app")
    offenders: List[str] = []
    for path in root.rglob("*.py"):
        if path.name == "lakebase_connection_provider.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "lakebase_connection_provider" in text or "LakebaseConnectionProvider(" in text:
            offenders.append(str(path))
    assert offenders == []


def test_check_callback_is_added_only_when_supported() -> None:
    provider, _postgres, factory = provider_with_fakes(pool_factory=CapturingPoolFactory())
    with provider():
        pass
    assert factory.last_kwargs["check"] is CapturingPoolFactory.check_connection

    provider2, _postgres2, factory2 = provider_with_fakes(pool_factory=NoCheckPoolFactory())
    with provider2():
        pass
    assert "check" not in factory2.last_kwargs


def test_provider_and_tests_parse_with_ast() -> None:
    for path in (
        pathlib.Path("app/services/lakebase_connection_provider.py"),
        pathlib.Path("tests/test_lakebase_connection_provider.py"),
    ):
        ast.parse(path.read_text(encoding="utf-8"))


def test_oauth_factory_uses_exact_password_path() -> None:
    calls: List[str] = []

    def supplier(endpoint_name: str) -> FakeCredential:
        calls.append(endpoint_name)
        return FakeCredential(token="token-special")

    FakeBaseConnection.reset()
    cls = _build_oauth_connection_class(
        base_connection_class=FakeBaseConnection,
        credential_supplier=supplier,
        endpoint_name=VALID_ENV["LAKEBASE_ENDPOINT_NAME"],
    )
    result = cls.connect("", host="h", user="u")
    assert calls == [VALID_ENV["LAKEBASE_ENDPOINT_NAME"]]
    assert result["kwargs"]["password"] == "token-special"
