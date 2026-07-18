from __future__ import annotations

import pathlib
import socket
import sys
import types
from contextlib import contextmanager
from typing import Any, Dict, Optional

import pytest

from app.services.durable_genie_session_runtime_factory import (
    DurableGenieSessionRuntimeInitializationError,
)
from app.services.genie_pipeline import GeniePipeline
from app.services import genie_backend_factory as backend_factory


@contextmanager
def no_network():
    orig_create = socket.create_connection
    orig_socket = socket.socket

    def _explode(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("network access is not allowed in this test")

    socket.create_connection = _explode
    socket.socket = _explode
    try:
        yield
    finally:
        socket.create_connection = orig_create
        socket.socket = orig_socket


class FakeSettings:
    DATABRICKS_HOST = "fake-workspace.databricks.com"
    GENIE_RESPONSE_TIMEOUT_SECONDS = 33
    GENIE_EXPORT_STATUS_TTL_HOURS = 12
    GENIE_SPACE_ID = "space-123"
    GENIE_POLL_INTERVAL_SECONDS = 0.25
    GENIE_DEBUG = False
    GENIE_ENABLE_PROMPT_ENRICHMENT = True
    GENIE_EXPORT_PREVIEW_ROW_LIMIT = 25
    GENIE_MAX_DOWNLOAD_ROWS = 999
    GENIE_ASYNC_EXPORT_ENABLED = False
    GENIE_EXPORT_MODE = "returned_rows_only"
    GENIE_MAX_EXPORT_ROWS = 12345
    GENIE_EXPORT_QUERY_TIMEOUT_SECONDS = 77
    GENIE_EXPORT_STRIP_LIMIT = True
    GENIE_ENABLE_TABLE_SUMMARY = True
    GENIE_RAW_TABLE_SUMMARY_THRESHOLD = 50
    GENIE_SUMMARY_TOP_N = 5
    GENIE_ENABLE_COMPUTED_CHART = True

    def __init__(self, env: Optional[Dict[str, str]] = None) -> None:
        self._env = env or {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "false"}

    def model_dump(self) -> Dict[str, str]:
        return dict(self._env)


class CountingStore:
    created = 0
    last_instance = None

    def __init__(self, ttl_hours: int = 24) -> None:
        type(self).created += 1
        type(self).last_instance = self
        self.ttl_hours = ttl_hours

    @classmethod
    def reset(cls) -> None:
        cls.created = 0
        cls.last_instance = None


class FakeGenieClient:
    instances = []

    def __init__(self, host: str, timeout_seconds: int, user_token: str) -> None:
        self.host = host
        self.timeout_seconds = timeout_seconds
        self.user_token = user_token
        type(self).instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


class FakeAuditService:
    created = 0

    def __init__(self) -> None:
        type(self).created += 1

    @classmethod
    def reset(cls) -> None:
        cls.created = 0


class FakeSQLService:
    created = 0

    def __init__(self) -> None:
        type(self).created += 1

    @classmethod
    def reset(cls) -> None:
        cls.created = 0


class FakeRuntimeBundle:
    def __init__(self, enabled: bool, adapter: Any = None, durable: bool = False) -> None:
        self.enabled = enabled
        self.adapter = adapter
        self.durable = durable
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FakeAdapter:
    def __init__(self) -> None:
        self.run_calls = 0
        self.get_calls = 0
        self.put_calls = 0


class FakeRuntimeFactory:
    instances = []
    create_calls = []
    bundle_to_return: Any = None
    create_error: Optional[Exception] = None

    def __init__(self, *, environ: Optional[Dict[str, str]] = None) -> None:
        self.environ = environ
        type(self).instances.append(self)

    def create(self, *, cache_store: Any = None) -> Any:
        type(self).create_calls.append(cache_store)
        if type(self).create_error is not None:
            raise type(self).create_error
        return type(self).bundle_to_return

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.create_calls = []
        cls.bundle_to_return = None
        cls.create_error = None


class ExplodingRuntimeFactory(FakeRuntimeFactory):
    def create(self, *, cache_store: Any = None) -> Any:
        type(self).create_calls.append(cache_store)
        raise RuntimeError(
            "boom host=prod-lakebase.internal token=dapi-secret endpoint=projects/secret"
        )


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    backend_factory.reset_genie_pipeline()
    yield
    backend_factory.reset_genie_pipeline()


@pytest.fixture
def patched_backend(monkeypatch: pytest.MonkeyPatch):
    CountingStore.reset()
    FakeGenieClient.reset()
    FakeAuditService.reset()
    FakeSQLService.reset()
    FakeRuntimeFactory.reset()
    ExplodingRuntimeFactory.reset()

    settings_module = types.ModuleType("app.config")
    settings_module.settings = FakeSettings()
    monkeypatch.setitem(sys.modules, "app.config", settings_module)
    monkeypatch.setattr(backend_factory, "GenieSessionStore", CountingStore)
    monkeypatch.setattr(backend_factory, "GenieClient", FakeGenieClient)
    monkeypatch.setattr(backend_factory, "AuditService", FakeAuditService)
    monkeypatch.setattr(backend_factory, "SQLService", FakeSQLService)
    monkeypatch.setattr(
        backend_factory,
        "get_export_job_manager",
        lambda ttl_hours: {"ttl_hours": ttl_hours},
    )
    return settings_module.settings


def _attached_bundle(pipeline: GeniePipeline) -> Any:
    return getattr(pipeline, "_durable_session_runtime_bundle", None)


def test_get_genie_pipeline_still_returns_genie_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    patched_backend: FakeSettings,
) -> None:
    FakeRuntimeFactory.bundle_to_return = FakeRuntimeBundle(enabled=False)
    original_build = backend_factory._build_pipeline
    monkeypatch.setattr(
        backend_factory,
        "_build_pipeline",
        lambda user_token=None: original_build(
            user_token=user_token,
            runtime_factory_cls=FakeRuntimeFactory,
        ),
    )

    with no_network():
        pipeline = backend_factory.get_genie_pipeline()

    assert isinstance(pipeline, GeniePipeline)


def test_existing_singleton_identity_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    patched_backend: FakeSettings,
) -> None:
    FakeRuntimeFactory.bundle_to_return = FakeRuntimeBundle(enabled=False)
    original_build = backend_factory._build_pipeline
    monkeypatch.setattr(
        backend_factory,
        "_build_pipeline",
        lambda user_token=None: original_build(
            user_token=user_token,
            runtime_factory_cls=FakeRuntimeFactory,
        ),
    )

    with no_network():
        p1 = backend_factory.get_genie_pipeline()
        p2 = backend_factory.get_genie_pipeline()

    assert p1 is p2
    assert len(FakeRuntimeFactory.instances) == 1


def test_build_pipeline_creates_exactly_one_store_and_passes_it_everywhere(
    patched_backend: FakeSettings,
) -> None:
    bundle = FakeRuntimeBundle(enabled=False)
    FakeRuntimeFactory.bundle_to_return = bundle

    with no_network():
        pipeline = backend_factory._build_pipeline(runtime_factory_cls=FakeRuntimeFactory)

    assert CountingStore.created == 1
    assert pipeline._store is CountingStore.last_instance
    assert FakeRuntimeFactory.create_calls == [CountingStore.last_instance]
    assert _attached_bundle(pipeline) is bundle


def test_disabled_bundle_is_attached_without_adapter_calls(
    patched_backend: FakeSettings,
) -> None:
    adapter = FakeAdapter()
    bundle = FakeRuntimeBundle(enabled=False, adapter=adapter, durable=False)
    FakeRuntimeFactory.bundle_to_return = bundle

    with no_network():
        pipeline = backend_factory._build_pipeline(runtime_factory_cls=FakeRuntimeFactory)

    assert _attached_bundle(pipeline) is bundle
    assert adapter.run_calls == 0
    assert adapter.get_calls == 0
    assert adapter.put_calls == 0
    assert CountingStore.created == 1


def test_enabled_bundle_is_attached_without_second_store_or_adapter_use(
    patched_backend: FakeSettings,
) -> None:
    adapter = FakeAdapter()
    bundle = FakeRuntimeBundle(enabled=True, adapter=adapter, durable=True)
    FakeRuntimeFactory.bundle_to_return = bundle

    with no_network():
        pipeline = backend_factory._build_pipeline(runtime_factory_cls=FakeRuntimeFactory)

    assert _attached_bundle(pipeline) is bundle
    assert CountingStore.created == 1
    assert FakeRuntimeFactory.create_calls == [pipeline._store]
    assert adapter.run_calls == 0
    assert adapter.get_calls == 0
    assert adapter.put_calls == 0


def test_user_token_behavior_is_preserved(
    patched_backend: FakeSettings,
) -> None:
    FakeRuntimeFactory.bundle_to_return = FakeRuntimeBundle(enabled=False)

    with no_network():
        pipeline = backend_factory._build_pipeline(
            user_token="user-token-123",
            runtime_factory_cls=FakeRuntimeFactory,
        )

    assert pipeline._client.user_token == "user-token-123"


def test_reset_genie_pipeline_closes_attached_bundle_once(
    monkeypatch: pytest.MonkeyPatch,
    patched_backend: FakeSettings,
) -> None:
    bundle = FakeRuntimeBundle(enabled=True)
    FakeRuntimeFactory.bundle_to_return = bundle
    original_build = backend_factory._build_pipeline
    monkeypatch.setattr(
        backend_factory,
        "_build_pipeline",
        lambda user_token=None: original_build(
            user_token=user_token,
            runtime_factory_cls=FakeRuntimeFactory,
        ),
    )

    with no_network():
        pipeline = backend_factory.get_genie_pipeline()
    assert _attached_bundle(pipeline) is bundle

    backend_factory.reset_genie_pipeline()

    assert bundle.close_calls == 1
    assert getattr(pipeline, "_durable_session_runtime_bundle", None) is None


def test_repeated_reset_is_safe_and_does_not_double_close(
    monkeypatch: pytest.MonkeyPatch,
    patched_backend: FakeSettings,
) -> None:
    bundle = FakeRuntimeBundle(enabled=True)
    FakeRuntimeFactory.bundle_to_return = bundle
    original_build = backend_factory._build_pipeline
    monkeypatch.setattr(
        backend_factory,
        "_build_pipeline",
        lambda user_token=None: original_build(
            user_token=user_token,
            runtime_factory_cls=FakeRuntimeFactory,
        ),
    )

    with no_network():
        backend_factory.get_genie_pipeline()
    backend_factory.reset_genie_pipeline()
    backend_factory.reset_genie_pipeline()

    assert bundle.close_calls == 1


def test_legacy_pipeline_without_private_attribute_resets_safely(
    patched_backend: FakeSettings,
) -> None:
    pipeline = GeniePipeline(genie_client=object(), session_store=CountingStore(), space_id="space")
    backend_factory._genie_pipeline = pipeline

    backend_factory.reset_genie_pipeline()

    assert backend_factory._genie_pipeline is None


def test_disabled_bundle_cleanup_is_harmless(
    monkeypatch: pytest.MonkeyPatch,
    patched_backend: FakeSettings,
) -> None:
    bundle = FakeRuntimeBundle(enabled=False)
    FakeRuntimeFactory.bundle_to_return = bundle
    original_build = backend_factory._build_pipeline
    monkeypatch.setattr(
        backend_factory,
        "_build_pipeline",
        lambda user_token=None: original_build(
            user_token=user_token,
            runtime_factory_cls=FakeRuntimeFactory,
        ),
    )

    with no_network():
        backend_factory.get_genie_pipeline()
    backend_factory.reset_genie_pipeline()

    assert bundle.close_calls == 1


def test_runtime_initialization_failure_is_surfaced_and_not_cached(
    monkeypatch: pytest.MonkeyPatch,
    patched_backend: FakeSettings,
) -> None:
    original_build = backend_factory._build_pipeline
    monkeypatch.setattr(
        backend_factory,
        "_build_pipeline",
        lambda user_token=None: original_build(
            user_token=user_token,
            runtime_factory_cls=ExplodingRuntimeFactory,
        ),
    )

    with no_network(), pytest.raises(RuntimeError) as excinfo:
        backend_factory.get_genie_pipeline()

    msg = str(excinfo.value)
    assert msg == "Failed to initialize the durable Genie session runtime."
    assert "prod-lakebase.internal" not in msg
    assert "token" not in msg.lower()
    assert backend_factory._genie_pipeline is None


def test_sanitized_runtime_factory_error_is_preserved(
    patched_backend: FakeSettings,
) -> None:
    FakeRuntimeFactory.create_error = DurableGenieSessionRuntimeInitializationError(
        "Failed to construct the durable Genie session adapter."
    )

    with no_network(), pytest.raises(DurableGenieSessionRuntimeInitializationError) as excinfo:
        backend_factory._build_pipeline(runtime_factory_cls=FakeRuntimeFactory)

    assert str(excinfo.value) == "Failed to construct the durable Genie session adapter."
    assert "host" not in str(excinfo.value).lower()
    assert "token" not in str(excinfo.value).lower()


def test_no_separate_global_runtime_bundle_exists() -> None:
    assert not hasattr(backend_factory, "_durable_runtime_bundle")
    assert not hasattr(backend_factory, "durable_runtime_bundle")


def test_no_separate_global_repository_exists() -> None:
    assert not hasattr(backend_factory, "_conversation_repository")
    assert not hasattr(backend_factory, "conversation_repository")


def test_request_path_modules_do_not_access_private_bundle() -> None:
    for path in [
        "app/routes/chat.py",
        "app/services/genie_pipeline.py",
        "app/main.py",
        "app/services/genie_session_store.py",
    ]:
        text = pathlib.Path(path).read_text(encoding="utf-8")
        assert "_durable_session_runtime_bundle" not in text


def test_runtime_wiring_does_not_import_repository_or_lakebase_modules() -> None:
    text = pathlib.Path("app/services/genie_backend_factory.py").read_text(encoding="utf-8")
    assert "ConversationRepositoryFactory" not in text
    assert "durable_genie_session_adapter" not in text
    assert "lakebase_connection_provider" not in text
    assert "lakebase_conversation_repository" not in text


def test_main_chat_pipeline_and_store_files_remain_runtime_unmodified() -> None:
    for path in [
        "app/main.py",
        "app/routes/chat.py",
        "app/services/genie_pipeline.py",
        "app/services/genie_session_store.py",
    ]:
        text = pathlib.Path(path).read_text(encoding="utf-8")
        assert "durable_genie_session_runtime_factory" not in text
        assert "DurableGenieSessionAdapter" not in text


def test_runtime_factory_receives_settings_environment_mapping(
    patched_backend: FakeSettings,
) -> None:
    env = {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "true", "EXTRA_FLAG": "1"}
    patched_backend._env = env
    FakeRuntimeFactory.bundle_to_return = FakeRuntimeBundle(enabled=True)

    with no_network():
        backend_factory._build_pipeline(runtime_factory_cls=FakeRuntimeFactory)

    assert FakeRuntimeFactory.instances[0].environ == env


def test_failed_initialization_does_not_return_partially_initialized_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    patched_backend: FakeSettings,
) -> None:
    original_build = backend_factory._build_pipeline
    monkeypatch.setattr(
        backend_factory,
        "_build_pipeline",
        lambda user_token=None: original_build(
            user_token=user_token,
            runtime_factory_cls=ExplodingRuntimeFactory,
        ),
    )

    with no_network(), pytest.raises(RuntimeError):
        backend_factory.get_genie_pipeline()

    FakeRuntimeFactory.bundle_to_return = FakeRuntimeBundle(enabled=False)
    monkeypatch.setattr(
        backend_factory,
        "_build_pipeline",
        lambda user_token=None: original_build(
            user_token=user_token,
            runtime_factory_cls=FakeRuntimeFactory,
        ),
    )

    with no_network():
        pipeline = backend_factory.get_genie_pipeline()

    assert isinstance(pipeline, GeniePipeline)
    assert backend_factory._genie_pipeline is pipeline


def test_private_bundle_not_accessed_by_pipeline_construction(
    patched_backend: FakeSettings,
) -> None:
    adapter = FakeAdapter()
    bundle = FakeRuntimeBundle(enabled=True, adapter=adapter, durable=True)
    FakeRuntimeFactory.bundle_to_return = bundle

    with no_network():
        pipeline = backend_factory._build_pipeline(runtime_factory_cls=FakeRuntimeFactory)

    assert pipeline._store is CountingStore.last_instance
    assert _attached_bundle(pipeline) is bundle
    assert adapter.run_calls == 0
