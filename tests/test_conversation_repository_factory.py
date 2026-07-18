"""Tests for Phase 3A conversation repository factory.

These tests use fakes only. They do not connect to Lakebase, create real
credentials, open real pools, or execute SQL.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Import-isolation preamble
# ---------------------------------------------------------------------------
import os as _os
import sys as _sys

_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_EXPECTED_APP = _os.path.realpath(_os.path.join(_REPO_ROOT, "app", "__init__.py"))

if "app" in _sys.modules:
    _loaded = _os.path.realpath(getattr(_sys.modules["app"], "__file__", "") or "")
    if _loaded != _EXPECTED_APP:
        for _k in [k for k in _sys.modules if k == "app" or k.startswith("app.")]:
            del _sys.modules[_k]

if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)

del _os, _sys, _REPO_ROOT, _EXPECTED_APP
# ---------------------------------------------------------------------------

import ast
import importlib
import os
import pathlib
import socket
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import pytest

from app.services.conversation_repository import (
    ConversationRecord,
    ConversationRepository,
    ConversationStatus,
    InMemoryConversationRepository,
)
from app.services.conversation_repository_factory import (
    ConversationRepositoryBackend,
    ConversationRepositoryBundle,
    ConversationRepositoryFactory,
    ConversationRepositoryFactoryConfigurationError,
    ConversationRepositoryFactoryInitializationError,
    ConversationRepositoryFactorySettings,
    create_conversation_repository,
)
from app.services.lakebase_conversation_repository import LakebaseConversationRepository


class ExplodingEnviron(dict):
    def get(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("environment should not be read during import")


class GuardedEnvironment(Mapping[str, str]):
    def __init__(
        self,
        values: Optional[Dict[str, str]] = None,
        *,
        forbidden_keys: Optional[set[str]] = None,
    ) -> None:
        self._values = dict(values or {})
        self._forbidden_keys = set(forbidden_keys or set())
        self.accessed_keys: List[str] = []

    def __getitem__(self, key: str) -> str:
        self.accessed_keys.append(key)
        if key in self._forbidden_keys:
            raise AssertionError(f"forbidden environment access: {key}")
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        self.accessed_keys.append(key)
        if key in self._forbidden_keys:
            raise AssertionError(f"forbidden environment access: {key}")
        return self._values.get(key, default)


@dataclass(frozen=True)
class FakeLakebaseSettings:
    host: str = "secret-host.internal"
    endpoint_name: str = "projects/secret/branches/main/endpoints/primary"
    service_principal_id: str = "sp-1234567890"

    def __repr__(self) -> str:
        return (
            "FakeLakebaseSettings("
            f"host={self.host!r}, endpoint_name={self.endpoint_name!r}, "
            f"service_principal_id={self.service_principal_id!r})"
        )


class FakeLakebaseProvider:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.close_calls = 0
        self.pool_open_calls = 0
        self.credential_calls = 0
        self.sql_calls = 0
        self.connection_calls = 0

    def __call__(self):  # pragma: no cover - should never be called in these tests
        self.connection_calls += 1
        raise AssertionError("factory construction must not open a connection")

    def close(self) -> None:
        self.close_calls += 1

    def __repr__(self) -> str:
        return (
            "FakeLakebaseProvider("
            "host='secret-host.internal', "
            "endpoint='projects/secret/branches/main/endpoints/primary', "
            "service_principal_id='sp-1234567890')"
        )


class FakeRepository:
    def __init__(self, provider: Any | None = None) -> None:
        self.provider = provider

    def get_by_id(self, owner_user_id_hash: str, conversation_id: str):
        return None

    def get_by_frontend_id(self, owner_user_id_hash: str, frontend_conversation_id: str):
        return None

    def create_conversation(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        *,
        conversation_id: Optional[str] = None,
        now: Any = None,
    ):
        ts = now
        if ts is None:
            raise AssertionError("tests should not call create_conversation without now")
        return ConversationRecord(
            conversation_id=conversation_id or "fake-conv",
            owner_user_id_hash=owner_user_id_hash,
            frontend_conversation_id=frontend_conversation_id,
            genie_conversation_id=None,
            last_genie_message_id=None,
            status=ConversationStatus.ACTIVE,
            version=1,
            created_at=ts,
            updated_at=ts,
            last_active_at=ts,
        )

    def bind_genie_conversation(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        genie_conversation_id: str,
        *,
        expected_version: int,
        now: Any = None,
    ):
        return None

    def update_last_genie_message(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        last_genie_message_id: str,
        *,
        expected_version: int,
        now: Any = None,
    ):
        return None

    def touch(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        *,
        expected_version: int,
        now: Any = None,
    ):
        return None

    def set_status(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        status: ConversationStatus,
        *,
        expected_version: int,
        now: Any = None,
    ):
        return None

    def compare_and_update(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        *,
        expected_version: int,
        genie_conversation_id: Optional[str] = None,
        last_genie_message_id: Optional[str] = None,
        status: Optional[ConversationStatus] = None,
        touch_last_active: bool = True,
        now: Any = None,
    ):
        return None

    def list_for_owner(self, owner_user_id_hash: str, *, statuses=None, limit: int = 50):
        return []

    def delete_conversation(self, owner_user_id_hash: str, conversation_id: str) -> bool:
        return False

    def __repr__(self) -> str:
        return (
            "FakeRepository("
            "host='secret-host.internal', "
            "endpoint='projects/secret/branches/main/endpoints/primary', "
            "service_principal_id='sp-1234567890')"
        )


class CountingSettingsFactory:
    def __init__(self, settings: Optional[Any] = None) -> None:
        self.settings = settings or FakeLakebaseSettings()
        self.calls = 0
        self.last_environ: Optional[Mapping[str, str]] = None

    def __call__(self, environ: Optional[Mapping[str, str]] = None) -> Any:
        self.calls += 1
        self.last_environ = environ
        return self.settings


class CountingProviderFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.providers: List[FakeLakebaseProvider] = []
        self.last_settings: Any = None

    def __call__(self, settings: Any) -> FakeLakebaseProvider:
        self.calls += 1
        self.last_settings = settings
        provider = FakeLakebaseProvider(settings)
        self.providers.append(provider)
        return provider


class CountingRepositoryFactory:
    def __init__(self, *, fail: bool = False, exc_message: str = "boom") -> None:
        self.calls = 0
        self.providers: List[Any] = []
        self.fail = fail
        self.exc_message = exc_message

    def __call__(self, provider: Any) -> FakeRepository:
        self.calls += 1
        self.providers.append(provider)
        if self.fail:
            raise RuntimeError(self.exc_message)
        return FakeRepository(provider=provider)


class ExplodingMemoryFactory:
    def __call__(self) -> InMemoryConversationRepository:  # pragma: no cover
        raise AssertionError("memory factory should not have been called")


class CountingMemoryFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.repositories: List[InMemoryConversationRepository] = []

    def __call__(self) -> InMemoryConversationRepository:
        self.calls += 1
        repo = InMemoryConversationRepository()
        self.repositories.append(repo)
        return repo


def clear_factory_module() -> None:
    sys.modules.pop("app.services.conversation_repository_factory", None)


@contextmanager
def no_network() -> Any:
    original_create_connection = socket.create_connection
    original_socket = socket.socket

    def _explode(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("network access is not allowed in this test")

    socket.create_connection = _explode
    socket.socket = _explode
    try:
        yield
    finally:
        socket.create_connection = original_create_connection
        socket.socket = original_socket


def make_lakebase_env(**overrides: str) -> Dict[str, str]:
    env = {
        "CONVERSATION_REPOSITORY_BACKEND": "lakebase",
        "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": "true",
    }
    env.update(overrides)
    return env


def test_default_backend_is_memory() -> None:
    settings = ConversationRepositoryFactorySettings.from_environment({})
    assert settings.backend is ConversationRepositoryBackend.MEMORY


def test_default_lakebase_flag_is_false() -> None:
    settings = ConversationRepositoryFactorySettings.from_environment({})
    assert settings.lakebase_enabled is False


def test_explicit_memory_selection() -> None:
    settings = ConversationRepositoryFactorySettings.from_environment(
        {"CONVERSATION_REPOSITORY_BACKEND": "memory"}
    )
    assert settings.backend is ConversationRepositoryBackend.MEMORY
    assert settings.lakebase_enabled is False


def test_lakebase_selection_with_flag_true() -> None:
    settings = ConversationRepositoryFactorySettings.from_environment(make_lakebase_env())
    assert settings.backend is ConversationRepositoryBackend.LAKEBASE
    assert settings.lakebase_enabled is True


def test_lakebase_selection_with_flag_false_rejected() -> None:
    with pytest.raises(ConversationRepositoryFactoryConfigurationError) as excinfo:
        ConversationRepositoryFactorySettings.from_environment(
            {
                "CONVERSATION_REPOSITORY_BACKEND": "lakebase",
                "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": "false",
            }
        )
    assert "disabled" in str(excinfo.value).lower()


def test_invalid_backend_rejected() -> None:
    with pytest.raises(ConversationRepositoryFactoryConfigurationError) as excinfo:
        ConversationRepositoryFactorySettings.from_environment(
            {"CONVERSATION_REPOSITORY_BACKEND": "postgres-primary"}
        )
    assert "postgres-primary" not in str(excinfo.value)


def test_backend_parsing_is_case_insensitive() -> None:
    settings = ConversationRepositoryFactorySettings.from_environment(
        {
            "CONVERSATION_REPOSITORY_BACKEND": "LaKeBaSe",
            "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": "TrUe",
        }
    )
    assert settings.backend is ConversationRepositoryBackend.LAKEBASE
    assert settings.lakebase_enabled is True


def test_backend_whitespace_is_stripped() -> None:
    settings = ConversationRepositoryFactorySettings.from_environment(
        {
            "CONVERSATION_REPOSITORY_BACKEND": "  memory  ",
            "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": "  false  ",
        }
    )
    assert settings.backend is ConversationRepositoryBackend.MEMORY
    assert settings.lakebase_enabled is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " On "])
def test_all_supported_true_values(value: str) -> None:
    settings = ConversationRepositoryFactorySettings.from_environment(
        {
            "CONVERSATION_REPOSITORY_BACKEND": "memory",
            "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": value,
        }
    )
    assert settings.lakebase_enabled is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", "", " off "])
def test_all_supported_false_values(value: str) -> None:
    settings = ConversationRepositoryFactorySettings.from_environment(
        {
            "CONVERSATION_REPOSITORY_BACKEND": "memory",
            "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": value,
        }
    )
    assert settings.lakebase_enabled is False


def test_invalid_boolean_rejected() -> None:
    with pytest.raises(ConversationRepositoryFactoryConfigurationError) as excinfo:
        ConversationRepositoryFactorySettings.from_environment(
            {"ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": "definitely"}
        )
    assert "definitely" not in str(excinfo.value)


def test_import_reads_no_environment() -> None:
    clear_factory_module()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "environ", ExplodingEnviron())
        module = importlib.import_module("app.services.conversation_repository_factory")
    assert hasattr(module, "ConversationRepositoryFactory")


def test_memory_returns_inmemory_repository() -> None:
    bundle = ConversationRepositoryFactory(environ={}).create()
    assert isinstance(bundle.repository, InMemoryConversationRepository)


def test_memory_bundle_backend_is_memory() -> None:
    bundle = ConversationRepositoryFactory(environ={}).create()
    assert bundle.backend is ConversationRepositoryBackend.MEMORY


def test_memory_bundle_durable_is_false() -> None:
    bundle = ConversationRepositoryFactory(environ={}).create()
    assert bundle.durable is False


def test_memory_path_does_not_create_lakebase_settings() -> None:
    settings_factory = CountingSettingsFactory()
    factory = ConversationRepositoryFactory(
        environ={},
        durable_settings_factory=settings_factory,
    )
    factory.create()
    assert settings_factory.calls == 0


def test_memory_path_does_not_create_provider() -> None:
    provider_factory = CountingProviderFactory()
    factory = ConversationRepositoryFactory(
        environ={},
        durable_connection_factory=provider_factory,
    )
    factory.create()
    assert provider_factory.calls == 0


def test_memory_path_does_not_create_workspaceclient() -> None:
    with no_network():
        bundle = ConversationRepositoryFactory(environ={}).create()
    assert isinstance(bundle.repository, InMemoryConversationRepository)


def test_memory_path_does_not_read_pg_variables() -> None:
    env = GuardedEnvironment(
        {},
        forbidden_keys={
            "PGHOST",
            "PGDATABASE",
            "PGPORT",
            "PGUSER",
            "PGSSLMODE",
            "PGAPPNAME",
            "LAKEBASE_ENDPOINT_NAME",
        },
    )
    bundle = ConversationRepositoryFactory(environ=env).create()
    assert isinstance(bundle.repository, InMemoryConversationRepository)


def test_memory_close_is_safe() -> None:
    bundle = ConversationRepositoryFactory(environ={}).create()
    bundle.close()


def test_repeated_memory_close_is_safe() -> None:
    bundle = ConversationRepositoryFactory(environ={}).create()
    bundle.close()
    bundle.close()


def test_lakebase_path_creates_settings_exactly_once() -> None:
    settings_factory = CountingSettingsFactory()
    provider_factory = CountingProviderFactory()
    repository_factory = CountingRepositoryFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=settings_factory,
        durable_connection_factory=provider_factory,
        durable_repository_factory=repository_factory,
    )
    factory.create()
    assert settings_factory.calls == 1


def test_provider_created_exactly_once() -> None:
    settings_factory = CountingSettingsFactory()
    provider_factory = CountingProviderFactory()
    repository_factory = CountingRepositoryFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=settings_factory,
        durable_connection_factory=provider_factory,
        durable_repository_factory=repository_factory,
    )
    factory.create()
    assert provider_factory.calls == 1


def test_repository_receives_provider() -> None:
    settings_factory = CountingSettingsFactory()
    provider_factory = CountingProviderFactory()
    repository_factory = CountingRepositoryFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=settings_factory,
        durable_connection_factory=provider_factory,
        durable_repository_factory=repository_factory,
    )
    bundle = factory.create()
    assert repository_factory.providers[0] is provider_factory.providers[0]
    assert getattr(bundle.repository, "provider") is provider_factory.providers[0]


def test_lakebase_bundle_backend_is_lakebase() -> None:
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=CountingProviderFactory(),
        durable_repository_factory=CountingRepositoryFactory(),
    )
    bundle = factory.create()
    assert bundle.backend is ConversationRepositoryBackend.LAKEBASE


def test_lakebase_bundle_durable_is_true() -> None:
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=CountingProviderFactory(),
        durable_repository_factory=CountingRepositoryFactory(),
    )
    bundle = factory.create()
    assert bundle.durable is True


def test_lakebase_construction_opens_no_pool() -> None:
    provider_factory = CountingProviderFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=provider_factory,
        durable_repository_factory=CountingRepositoryFactory(),
    )
    factory.create()
    assert provider_factory.providers[0].pool_open_calls == 0


def test_lakebase_construction_creates_no_credential() -> None:
    provider_factory = CountingProviderFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=provider_factory,
        durable_repository_factory=CountingRepositoryFactory(),
    )
    factory.create()
    assert provider_factory.providers[0].credential_calls == 0


def test_lakebase_construction_executes_no_sql() -> None:
    provider_factory = CountingProviderFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=provider_factory,
        durable_repository_factory=CountingRepositoryFactory(),
    )
    factory.create()
    assert provider_factory.providers[0].sql_calls == 0


def test_lakebase_bundle_close_closes_provider() -> None:
    provider_factory = CountingProviderFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=provider_factory,
        durable_repository_factory=CountingRepositoryFactory(),
    )
    bundle = factory.create()
    bundle.close()
    assert provider_factory.providers[0].close_calls == 1


def test_repeated_lakebase_close_is_safe() -> None:
    provider_factory = CountingProviderFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=provider_factory,
        durable_repository_factory=CountingRepositoryFactory(),
    )
    bundle = factory.create()
    bundle.close()
    bundle.close()
    assert provider_factory.providers[0].close_calls == 1


def test_context_manager_closes_provider() -> None:
    provider_factory = CountingProviderFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=provider_factory,
        durable_repository_factory=CountingRepositoryFactory(),
    )
    with factory.create() as bundle:
        assert bundle.backend is ConversationRepositoryBackend.LAKEBASE
    assert provider_factory.providers[0].close_calls == 1


def test_failure_after_provider_creation_closes_provider() -> None:
    provider_factory = CountingProviderFactory()
    repository_factory = CountingRepositoryFactory(fail=True)
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=provider_factory,
        durable_repository_factory=repository_factory,
    )
    with pytest.raises(ConversationRepositoryFactoryInitializationError):
        factory.create()
    assert provider_factory.providers[0].close_calls == 1


def test_initialization_failure_is_sanitized() -> None:
    provider_factory = CountingProviderFactory()
    repository_factory = CountingRepositoryFactory(
        fail=True,
        exc_message="host=prod.example token=abc endpoint=projects/x service_principal=sp-1",
    )
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=provider_factory,
        durable_repository_factory=repository_factory,
    )
    with pytest.raises(ConversationRepositoryFactoryInitializationError) as excinfo:
        factory.create()
    text = str(excinfo.value)
    assert "host=prod.example" not in text
    assert "token=abc" not in text
    assert "service_principal" not in text


def test_no_silent_fallback_to_memory() -> None:
    memory_factory = CountingMemoryFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        memory_repository_factory=memory_factory,
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=CountingProviderFactory(),
        durable_repository_factory=CountingRepositoryFactory(fail=True),
    )
    with pytest.raises(ConversationRepositoryFactoryInitializationError):
        factory.create()
    assert memory_factory.calls == 0


def test_repr_contains_no_host() -> None:
    bundle = ConversationRepositoryBundle(
        repository=FakeRepository(),
        backend=ConversationRepositoryBackend.LAKEBASE,
        durable=True,
        _resource_closer=lambda: None,
    )
    assert "secret-host.internal" not in repr(bundle)


def test_repr_contains_no_endpoint() -> None:
    bundle = ConversationRepositoryBundle(
        repository=FakeRepository(),
        backend=ConversationRepositoryBackend.LAKEBASE,
        durable=True,
        _resource_closer=lambda: None,
    )
    assert "projects/secret/branches/main/endpoints/primary" not in repr(bundle)


def test_repr_contains_no_service_principal_id() -> None:
    bundle = ConversationRepositoryBundle(
        repository=FakeRepository(),
        backend=ConversationRepositoryBackend.LAKEBASE,
        durable=True,
        _resource_closer=lambda: None,
    )
    assert "sp-1234567890" not in repr(bundle)


def test_errors_contain_no_environment_values() -> None:
    secret_value = "very-secret-value"
    with pytest.raises(ConversationRepositoryFactoryConfigurationError) as excinfo:
        ConversationRepositoryFactorySettings.from_environment(
            {"ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": secret_value}
        )
    assert secret_value not in str(excinfo.value)


def test_errors_contain_no_raw_exception_details() -> None:
    def failing_provider_factory(settings: Any) -> Any:
        raise RuntimeError("password=hunter2 host=prod.example endpoint=projects/x")

    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=failing_provider_factory,
        durable_repository_factory=CountingRepositoryFactory(),
    )
    with pytest.raises(ConversationRepositoryFactoryInitializationError) as excinfo:
        factory.create()
    text = str(excinfo.value)
    assert "hunter2" not in text
    assert "prod.example" not in text
    assert "projects/x" not in text


def test_memory_repository_satisfies_conversation_repository() -> None:
    repo = InMemoryConversationRepository()
    assert isinstance(repo, ConversationRepository)


def test_lakebase_repository_satisfies_conversation_repository() -> None:
    @contextmanager
    def provider():
        yield object()

    repo = LakebaseConversationRepository(connection_provider=provider)
    assert isinstance(repo, ConversationRepository)


def test_bundle_exposes_repository_contract() -> None:
    bundle = ConversationRepositoryFactory(environ={}).create()
    assert isinstance(bundle.repository, ConversationRepository)


def test_no_existing_runtime_module_imports_the_factory() -> None:
    # Only this one approved Phase 3C module is permitted to import the factory.
    # All other application modules must not import conversation_repository_factory.
    _APPROVED_IMPORTER = "app/services/durable_genie_session_runtime_factory.py"
    app_root = pathlib.Path("app")
    offenders: List[str] = []
    for path in app_root.rglob("*.py"):
        if path.name == "conversation_repository_factory.py":
            continue
        if path.as_posix() == _APPROVED_IMPORTER:
            continue
        text = path.read_text(encoding="utf-8")
        if "conversation_repository_factory" in text:
            offenders.append(str(path))
    assert offenders == []


def test_no_global_repository_constructed_at_import() -> None:
    clear_factory_module()
    module = importlib.import_module("app.services.conversation_repository_factory")
    values = list(module.__dict__.values())
    assert not any(isinstance(value, ConversationRepositoryBundle) for value in values)
    assert not any(isinstance(value, InMemoryConversationRepository) for value in values)


def test_no_network_access_occurs_during_import() -> None:
    clear_factory_module()
    with no_network():
        module = importlib.import_module("app.services.conversation_repository_factory")
    assert hasattr(module, "ConversationRepositoryFactory")


def test_no_network_access_occurs_during_memory_construction() -> None:
    with no_network():
        bundle = ConversationRepositoryFactory(environ={}).create()
    assert isinstance(bundle.repository, InMemoryConversationRepository)


def test_no_network_access_occurs_during_mocked_lakebase_construction() -> None:
    with no_network():
        factory = ConversationRepositoryFactory(
            environ=make_lakebase_env(),
            durable_settings_factory=CountingSettingsFactory(),
            durable_connection_factory=CountingProviderFactory(),
            durable_repository_factory=CountingRepositoryFactory(),
        )
        bundle = factory.create()
    assert bundle.backend is ConversationRepositoryBackend.LAKEBASE


def test_independent_factory_calls_create_independent_bundles() -> None:
    factory = ConversationRepositoryFactory(environ={})
    bundle_a = factory.create()
    bundle_b = factory.create()
    assert bundle_a is not bundle_b
    assert bundle_a.repository is not bundle_b.repository


def test_closing_one_bundle_does_not_close_another() -> None:
    provider_factory = CountingProviderFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=provider_factory,
        durable_repository_factory=CountingRepositoryFactory(),
    )
    bundle_a = factory.create()
    bundle_b = factory.create()
    bundle_a.close()
    assert provider_factory.providers[0].close_calls == 1
    assert provider_factory.providers[1].close_calls == 0
    bundle_b.close()
    assert provider_factory.providers[1].close_calls == 1


def test_repeated_create_calls_do_not_share_provider_state() -> None:
    provider_factory = CountingProviderFactory()
    factory = ConversationRepositoryFactory(
        environ=make_lakebase_env(),
        durable_settings_factory=CountingSettingsFactory(),
        durable_connection_factory=provider_factory,
        durable_repository_factory=CountingRepositoryFactory(),
    )
    factory.create()
    factory.create()
    assert len(provider_factory.providers) == 2
    assert provider_factory.providers[0] is not provider_factory.providers[1]


def test_enabled_flag_with_memory_backend_still_returns_memory_repository() -> None:
    bundle = ConversationRepositoryFactory(
        environ={
            "CONVERSATION_REPOSITORY_BACKEND": "memory",
            "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY": "true",
        }
    ).create()
    assert isinstance(bundle.repository, InMemoryConversationRepository)
    assert bundle.backend is ConversationRepositoryBackend.MEMORY


def test_blank_backend_defaults_to_memory() -> None:
    settings = ConversationRepositoryFactorySettings.from_environment(
        {"CONVERSATION_REPOSITORY_BACKEND": "   "}
    )
    assert settings.backend is ConversationRepositoryBackend.MEMORY


def test_convenience_function_returns_new_bundle_each_time() -> None:
    bundle_a = create_conversation_repository({})
    bundle_b = create_conversation_repository({})
    assert bundle_a is not bundle_b
    assert bundle_a.repository is not bundle_b.repository


def test_factory_source_has_no_merge_markers_and_parses() -> None:
    path = pathlib.Path("app/services/conversation_repository_factory.py")
    source = path.read_text(encoding="utf-8")
    assert "<<<<<<<" not in source
    assert ">>>>>>>" not in source
    ast.parse(source)


def test_factory_test_source_parses() -> None:
    path = pathlib.Path("tests/test_conversation_repository_factory.py")
    source = path.read_text(encoding="utf-8")
    ast.parse(source)
