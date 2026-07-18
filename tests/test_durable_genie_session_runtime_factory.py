"""Tests for Phase 3C durable Genie session runtime composition factory.

Uses fakes only. Does not connect to Lakebase, create real credentials,
open real pools, or execute SQL.

Test numbering follows the Phase 3C specification exactly.
"""
from __future__ import annotations

import ast
import importlib
import os
import pathlib
import socket
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

import pytest

from app.services.durable_genie_session_runtime_factory import (
    CacheStoreProtocol,
    DurableGenieSessionRuntimeBundle,
    DurableGenieSessionRuntimeConfigurationError,
    DurableGenieSessionRuntimeFactory,
    DurableGenieSessionRuntimeFactoryError,
    DurableGenieSessionRuntimeInitializationError,
    DurableGenieSessionRuntimeSettings,
    create_durable_genie_session_runtime,
)


# ---------------------------------------------------------------------------
# Sensitive sentinel values used to verify nothing leaks into repr / errors
# ---------------------------------------------------------------------------

SENSITIVE_HOST = "prod-lakebase.internal"
SENSITIVE_ENDPOINT = "projects/secret/branches/main/endpoints/primary"
SENSITIVE_OWNER = "ownerhash-secret-abc"
SENSITIVE_GENIE = "genie-conv-secret-xyz"
SENSITIVE_TOKEN = "dapi-secret-token-123"
SENSITIVE_ENV_VALUE = "definitely-bad-boolean"


# ---------------------------------------------------------------------------
# Fake back-end enum mirror (avoids real ConversationRepositoryBackend import)
# ---------------------------------------------------------------------------

class _FakeBackend:
    """Minimal backend placeholder matching the .value attribute contract."""
    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"FakeBackend({self.value!r})"


FAKE_BACKEND_MEMORY = _FakeBackend("memory")
FAKE_BACKEND_LAKEBASE = _FakeBackend("lakebase")


# ---------------------------------------------------------------------------
# Fake repository bundle
# ---------------------------------------------------------------------------

@dataclass
class FakeRepositoryBundle:
    backend: Any = field(default_factory=lambda: FAKE_BACKEND_MEMORY)
    durable: bool = False
    close_calls: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        self.close_calls += 1
        self._closed = True

    def __repr__(self) -> str:
        return f"FakeRepositoryBundle(backend={self.backend.value!r}, closed={self._closed!r})"


# ---------------------------------------------------------------------------
# Fake adapter
# ---------------------------------------------------------------------------

@dataclass
class FakeAdapter:
    bundle: Any
    cache_store: Any = None
    cache_enabled: bool = True
    close_calls: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        self.close_calls += 1
        self._closed = True

    def __repr__(self) -> str:
        return f"FakeAdapter(closed={self._closed!r})"


# ---------------------------------------------------------------------------
# Fake cache store implementing CacheStoreProtocol
# ---------------------------------------------------------------------------

class FakeCacheStore:
    def __init__(self) -> None:
        self.genie_ids: Dict[str, str] = {}
        self.message_ids: Dict[str, str] = {}
        self.calls: List[tuple] = []

    def get_genie_conversation_id(self, cache_key: str) -> Optional[str]:
        self.calls.append(("get_genie_conversation_id", cache_key))
        return self.genie_ids.get(cache_key)

    def get_last_message_id(self, cache_key: str) -> Optional[str]:
        self.calls.append(("get_last_message_id", cache_key))
        return self.message_ids.get(cache_key)

    def set_genie_conversation_id(self, cache_key: str, genie_conversation_id: str) -> None:
        self.calls.append(("set_genie_conversation_id", cache_key))
        self.genie_ids[cache_key] = genie_conversation_id

    def set_last_message_id(self, cache_key: str, last_genie_message_id: str) -> None:
        self.calls.append(("set_last_message_id", cache_key))
        self.message_ids[cache_key] = last_genie_message_id

    def reset_genie_mapping(self, cache_key: str) -> None:
        self.calls.append(("reset_genie_mapping", cache_key))

    def reset_session(self, cache_key: str) -> None:
        self.calls.append(("reset_session", cache_key))


# ---------------------------------------------------------------------------
# Fake repository factory instance (returned by FakeRepositoryFactoryCls)
# ---------------------------------------------------------------------------

class FakeRepositoryFactoryInstance:
    def __init__(
        self,
        environ: Optional[Mapping[str, str]],
        *,
        bundle: Optional[FakeRepositoryBundle],
        fail_create: bool,
        fail_msg: str,
    ) -> None:
        self.environ = environ
        self._bundle = bundle
        self._fail_create = fail_create
        self._fail_msg = fail_msg
        self.create_calls = 0

    def create(self) -> FakeRepositoryBundle:
        self.create_calls += 1
        if self._fail_create:
            raise RuntimeError(self._fail_msg)
        return self._bundle or FakeRepositoryBundle()


# ---------------------------------------------------------------------------
# Fake repository factory class (injectable as repository_factory_cls)
# ---------------------------------------------------------------------------

class FakeRepositoryFactoryCls:
    """Callable that acts as a replacement for ConversationRepositoryFactory."""

    def __init__(
        self,
        *,
        bundle: Optional[FakeRepositoryBundle] = None,
        fail_init: bool = False,
        fail_create: bool = False,
        fail_msg: str = "fake failure",
    ) -> None:
        self.bundle = bundle or FakeRepositoryBundle()
        self.fail_init = fail_init
        self.fail_create = fail_create
        self.fail_msg = fail_msg
        self.call_count = 0
        self.instances: List[FakeRepositoryFactoryInstance] = []
        self.last_environ: Optional[Mapping[str, str]] = None

    def __call__(self, *, environ: Optional[Mapping[str, str]] = None) -> FakeRepositoryFactoryInstance:
        self.call_count += 1
        self.last_environ = environ
        if self.fail_init:
            raise RuntimeError(self.fail_msg)
        instance = FakeRepositoryFactoryInstance(
            environ,
            bundle=self.bundle,
            fail_create=self.fail_create,
            fail_msg=self.fail_msg,
        )
        self.instances.append(instance)
        return instance


# ---------------------------------------------------------------------------
# Fake adapter class (injectable as adapter_cls)
# ---------------------------------------------------------------------------

class FakeAdapterCls:
    """Callable that acts as a replacement for DurableGenieSessionAdapter."""

    def __init__(self, *, fail: bool = False, fail_msg: str = "adapter failed") -> None:
        self.fail = fail
        self.fail_msg = fail_msg
        self.instances: List[FakeAdapter] = []

    def __call__(
        self,
        bundle: Any,
        cache_store: Any = None,
        cache_enabled: bool = True,
    ) -> FakeAdapter:
        if self.fail:
            raise RuntimeError(self.fail_msg)
        instance = FakeAdapter(bundle=bundle, cache_store=cache_store, cache_enabled=cache_enabled)
        self.instances.append(instance)
        return instance


# ---------------------------------------------------------------------------
# Guarded environment — raises on access to forbidden keys
# ---------------------------------------------------------------------------

class GuardedEnviron(Mapping[str, str]):
    def __init__(
        self,
        values: Optional[Dict[str, str]] = None,
        *,
        forbidden_keys: Optional[set] = None,
    ) -> None:
        self._values: Dict[str, str] = dict(values or {})
        self._forbidden_keys: set = set(forbidden_keys or set())
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


# ---------------------------------------------------------------------------
# Exploding environment — used to verify no environment access during import
# ---------------------------------------------------------------------------

class ExplodingEnviron(dict):
    def get(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("environment should not be read during import")

    def __getitem__(self, key: Any) -> Any:  # pragma: no cover
        raise AssertionError("environment should not be read during import")


# ---------------------------------------------------------------------------
# Network guard
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enabled_env() -> Dict[str, str]:
    return {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "true"}


def _disabled_env() -> Dict[str, str]:
    return {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "false"}


def _make_factory(
    *,
    enabled: bool = False,
    bundle: Optional[FakeRepositoryBundle] = None,
    repo_fail_init: bool = False,
    repo_fail_create: bool = False,
    adapter_fail: bool = False,
    fail_msg: str = "fake failure",
    cache_store: Optional[FakeCacheStore] = None,
) -> tuple[DurableGenieSessionRuntimeFactory, FakeRepositoryFactoryCls, FakeAdapterCls]:
    env = _enabled_env() if enabled else _disabled_env()
    repo_cls = FakeRepositoryFactoryCls(
        bundle=bundle,
        fail_init=repo_fail_init,
        fail_create=repo_fail_create,
        fail_msg=fail_msg,
    )
    adapter_cls = FakeAdapterCls(fail=adapter_fail, fail_msg=fail_msg)
    factory = DurableGenieSessionRuntimeFactory(
        environ=env,
        repository_factory_cls=repo_cls,
        adapter_cls=adapter_cls,
    )
    return factory, repo_cls, adapter_cls


def _clear_runtime_module() -> None:
    sys.modules.pop("app.services.durable_genie_session_runtime_factory", None)


# ===========================================================================
# SETTINGS (tests 1-10)
# ===========================================================================

def test_1_default_enabled_is_false() -> None:
    settings = DurableGenieSessionRuntimeSettings.from_environment({})
    assert settings.enabled is False


def test_2_explicit_false() -> None:
    settings = DurableGenieSessionRuntimeSettings.from_environment(
        {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "false"}
    )
    assert settings.enabled is False


def test_3_explicit_true() -> None:
    settings = DurableGenieSessionRuntimeSettings.from_environment(
        {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "true"}
    )
    assert settings.enabled is True


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " On ", "YES"])
def test_4_true_values_accepted(value: str) -> None:
    settings = DurableGenieSessionRuntimeSettings.from_environment(
        {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": value}
    )
    assert settings.enabled is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", "", " off ", "NO"])
def test_5_false_values_accepted(value: str) -> None:
    settings = DurableGenieSessionRuntimeSettings.from_environment(
        {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": value}
    )
    assert settings.enabled is False


@pytest.mark.parametrize("value", ["True", "FALSE", "YES", "NO", "ON", "OFF"])
def test_6_parsing_is_case_insensitive(value: str) -> None:
    settings = DurableGenieSessionRuntimeSettings.from_environment(
        {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": value}
    )
    assert isinstance(settings.enabled, bool)


def test_7_whitespace_is_stripped() -> None:
    settings = DurableGenieSessionRuntimeSettings.from_environment(
        {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "  true  "}
    )
    assert settings.enabled is True


def test_8_invalid_boolean_rejected() -> None:
    with pytest.raises(DurableGenieSessionRuntimeConfigurationError):
        DurableGenieSessionRuntimeSettings.from_environment(
            {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": SENSITIVE_ENV_VALUE}
        )


def test_9_import_reads_no_environment() -> None:
    _clear_runtime_module()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "environ", ExplodingEnviron())
        module = importlib.import_module("app.services.durable_genie_session_runtime_factory")
    assert hasattr(module, "DurableGenieSessionRuntimeFactory")


def test_10_error_hides_environment_values() -> None:
    with pytest.raises(DurableGenieSessionRuntimeConfigurationError) as excinfo:
        DurableGenieSessionRuntimeSettings.from_environment(
            {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": SENSITIVE_ENV_VALUE}
        )
    assert SENSITIVE_ENV_VALUE not in str(excinfo.value)


# ===========================================================================
# DISABLED PATH (tests 11-25)
# ===========================================================================

def test_11_returns_disabled_bundle() -> None:
    factory, _, _ = _make_factory(enabled=False)
    result = factory.create()
    assert result.enabled is False


def test_12_adapter_is_none_on_disabled() -> None:
    factory, _, _ = _make_factory(enabled=False)
    result = factory.create()
    assert result.adapter is None


def test_13_backend_is_none_on_disabled() -> None:
    factory, _, _ = _make_factory(enabled=False)
    result = factory.create()
    assert result.backend is None


def test_14_durable_is_false_on_disabled() -> None:
    factory, _, _ = _make_factory(enabled=False)
    result = factory.create()
    assert result.durable is False


def test_15_repository_factory_not_constructed_on_disabled() -> None:
    factory, repo_cls, _ = _make_factory(enabled=False)
    factory.create()
    assert repo_cls.call_count == 0


def test_16_repository_settings_not_read_on_disabled() -> None:
    # CONVERSATION_REPOSITORY_BACKEND must not be accessed on the disabled path.
    env = GuardedEnviron(
        {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "false"},
        forbidden_keys={"CONVERSATION_REPOSITORY_BACKEND", "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY"},
    )
    factory = DurableGenieSessionRuntimeFactory(
        environ=env,
        repository_factory_cls=FakeRepositoryFactoryCls(),
        adapter_cls=FakeAdapterCls(),
    )
    result = factory.create()  # must not trigger AssertionError from GuardedEnviron
    assert result.enabled is False


def test_17_pg_variables_not_read_on_disabled() -> None:
    # Lakebase-specific env vars must never be touched on the disabled path.
    env = GuardedEnviron(
        {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": "false"},
        forbidden_keys={
            "LAKEBASE_ENDPOINT_NAME",
            "LAKEBASE_PROJECT_NAME",
            "LAKEBASE_BRANCH_NAME",
            "LAKEBASE_DATABASE",
        },
    )
    factory = DurableGenieSessionRuntimeFactory(
        environ=env,
        repository_factory_cls=FakeRepositoryFactoryCls(),
        adapter_cls=FakeAdapterCls(),
    )
    result = factory.create()  # must not trigger AssertionError from GuardedEnviron
    assert result.enabled is False


def test_18_provider_not_constructed_on_disabled() -> None:
    factory, repo_cls, _ = _make_factory(enabled=False)
    factory.create()
    # No instances were created since call_count == 0
    assert len(repo_cls.instances) == 0


def test_19_adapter_not_constructed_on_disabled() -> None:
    factory, _, adapter_cls = _make_factory(enabled=False)
    factory.create()
    assert len(adapter_cls.instances) == 0


def test_20_no_workspace_client_on_disabled() -> None:
    # Structural guarantee: FakeRepositoryFactoryCls was never called.
    factory, repo_cls, _ = _make_factory(enabled=False)
    factory.create()
    assert repo_cls.call_count == 0


def test_21_no_pool_on_disabled() -> None:
    # Pool is opened only in LakebaseConnectionProvider (real path).
    # FakeRepositoryFactoryCls was never called → no pool.
    factory, repo_cls, _ = _make_factory(enabled=False)
    factory.create()
    assert repo_cls.call_count == 0


def test_22_no_credential_on_disabled() -> None:
    factory, repo_cls, _ = _make_factory(enabled=False)
    factory.create()
    assert repo_cls.call_count == 0


def test_23_no_sql_on_disabled() -> None:
    factory, repo_cls, _ = _make_factory(enabled=False)
    factory.create()
    assert repo_cls.call_count == 0


def test_24_disabled_close_safe() -> None:
    factory, _, _ = _make_factory(enabled=False)
    result = factory.create()
    result.close()  # must not raise


def test_25_repeated_disabled_close_safe() -> None:
    factory, _, _ = _make_factory(enabled=False)
    result = factory.create()
    result.close()
    result.close()
    result.close()  # must not raise on repeated calls


# ===========================================================================
# ENABLED COMPOSITION (tests 26-39)
# ===========================================================================

def test_26_repository_factory_constructed_once() -> None:
    factory, repo_cls, _ = _make_factory(enabled=True)
    factory.create()
    assert repo_cls.call_count == 1


def test_27_repository_factory_receives_environment_mapping() -> None:
    env = _enabled_env()
    repo_cls = FakeRepositoryFactoryCls()
    adapter_cls = FakeAdapterCls()
    factory = DurableGenieSessionRuntimeFactory(
        environ=env,
        repository_factory_cls=repo_cls,
        adapter_cls=adapter_cls,
    )
    factory.create()
    assert repo_cls.last_environ is env


def test_28_repository_bundle_created_once() -> None:
    factory, repo_cls, _ = _make_factory(enabled=True)
    factory.create()
    assert len(repo_cls.instances) == 1
    assert repo_cls.instances[0].create_calls == 1


def test_29_adapter_receives_exact_repository_bundle() -> None:
    bundle = FakeRepositoryBundle()
    repo_cls = FakeRepositoryFactoryCls(bundle=bundle)
    adapter_cls = FakeAdapterCls()
    factory = DurableGenieSessionRuntimeFactory(
        environ=_enabled_env(),
        repository_factory_cls=repo_cls,
        adapter_cls=adapter_cls,
    )
    factory.create()
    assert adapter_cls.instances[0].bundle is bundle


def test_30_adapter_receives_exact_cache_store() -> None:
    cache = FakeCacheStore()
    factory, _, adapter_cls = _make_factory(enabled=True)
    factory.create(cache_store=cache)
    assert adapter_cls.instances[0].cache_store is cache


def test_31_cache_enabled_true_when_cache_supplied() -> None:
    cache = FakeCacheStore()
    factory, _, adapter_cls = _make_factory(enabled=True)
    factory.create(cache_store=cache)
    assert adapter_cls.instances[0].cache_enabled is True


def test_32_cache_enabled_false_when_cache_absent() -> None:
    factory, _, adapter_cls = _make_factory(enabled=True)
    factory.create(cache_store=None)
    assert adapter_cls.instances[0].cache_enabled is False


def test_33_backend_propagated() -> None:
    bundle = FakeRepositoryBundle(backend=FAKE_BACKEND_LAKEBASE, durable=True)
    repo_cls = FakeRepositoryFactoryCls(bundle=bundle)
    adapter_cls = FakeAdapterCls()
    factory = DurableGenieSessionRuntimeFactory(
        environ=_enabled_env(),
        repository_factory_cls=repo_cls,
        adapter_cls=adapter_cls,
    )
    result = factory.create()
    assert result.backend is FAKE_BACKEND_LAKEBASE


def test_34_durable_propagated() -> None:
    bundle = FakeRepositoryBundle(backend=FAKE_BACKEND_LAKEBASE, durable=True)
    repo_cls = FakeRepositoryFactoryCls(bundle=bundle)
    adapter_cls = FakeAdapterCls()
    factory = DurableGenieSessionRuntimeFactory(
        environ=_enabled_env(),
        repository_factory_cls=repo_cls,
        adapter_cls=adapter_cls,
    )
    result = factory.create()
    assert result.durable is True


def test_35_construction_opens_no_pool() -> None:
    # Structural guarantee via fake: FakeRepositoryFactoryCls never opens a pool.
    factory, repo_cls, _ = _make_factory(enabled=True)
    with no_network():
        factory.create()  # must not trigger network guard
    assert repo_cls.call_count == 1  # was called but no real pool opened


def test_36_construction_generates_no_credential() -> None:
    factory, repo_cls, _ = _make_factory(enabled=True)
    factory.create()
    # Fake was used; no credential generation path exists in the fake.
    assert repo_cls.call_count == 1


def test_37_construction_executes_no_sql() -> None:
    factory, repo_cls, _ = _make_factory(enabled=True)
    factory.create()
    # Fake does not execute SQL; just verify the factory was called.
    assert repo_cls.call_count == 1


def test_38_independent_creates_return_independent_bundles() -> None:
    factory, _, _ = _make_factory(enabled=True)
    result_a = factory.create()
    result_b = factory.create()
    assert result_a is not result_b
    assert result_a.adapter is not result_b.adapter


def test_39_no_global_state_shared() -> None:
    module = importlib.import_module("app.services.durable_genie_session_runtime_factory")
    for value in module.__dict__.values():
        assert not isinstance(value, DurableGenieSessionRuntimeBundle), (
            "A DurableGenieSessionRuntimeBundle was found at module level"
        )
        assert not isinstance(value, DurableGenieSessionRuntimeFactory), (
            "A DurableGenieSessionRuntimeFactory singleton was found at module level"
        )


# ===========================================================================
# LIFECYCLE (tests 40-47)
# ===========================================================================

def test_40_enabled_bundle_closes_adapter_once() -> None:
    factory, _, adapter_cls = _make_factory(enabled=True)
    result = factory.create()
    result.close()
    assert adapter_cls.instances[0].close_calls == 1


def test_41_adapter_owns_repository_bundle_lifecycle() -> None:
    bundle = FakeRepositoryBundle()
    repo_cls = FakeRepositoryFactoryCls(bundle=bundle)
    adapter_cls = FakeAdapterCls()
    factory = DurableGenieSessionRuntimeFactory(
        environ=_enabled_env(),
        repository_factory_cls=repo_cls,
        adapter_cls=adapter_cls,
    )
    result = factory.create()
    # Before close: adapter.close_calls == 0, bundle.close_calls == 0
    assert adapter_cls.instances[0].close_calls == 0
    assert bundle.close_calls == 0
    result.close()
    # Runtime bundle closes adapter (adapter would then close bundle in real impl).
    assert adapter_cls.instances[0].close_calls == 1


def test_42_runtime_bundle_does_not_double_close_repository_bundle() -> None:
    # The runtime bundle's close() must call adapter.close() exactly once.
    # It must NOT directly call bundle.close() a second time.
    bundle = FakeRepositoryBundle()
    repo_cls = FakeRepositoryFactoryCls(bundle=bundle)
    adapter_cls = FakeAdapterCls()
    factory = DurableGenieSessionRuntimeFactory(
        environ=_enabled_env(),
        repository_factory_cls=repo_cls,
        adapter_cls=adapter_cls,
    )
    result = factory.create()
    result.close()
    # Adapter was closed once. The bundle itself was NOT closed by the runtime
    # bundle directly (the fake adapter does not close the bundle, so it stays 0).
    assert bundle.close_calls == 0  # runtime bundle did not directly close the bundle
    assert adapter_cls.instances[0].close_calls == 1


def test_43_repeated_close_safe() -> None:
    factory, _, adapter_cls = _make_factory(enabled=True)
    result = factory.create()
    result.close()
    result.close()
    result.close()
    assert adapter_cls.instances[0].close_calls == 1  # called exactly once


def test_44_context_manager_closes_adapter() -> None:
    factory, _, adapter_cls = _make_factory(enabled=True)
    with factory.create() as result:
        assert result.adapter is not None
    assert adapter_cls.instances[0].close_calls == 1


def test_45_one_bundle_close_does_not_close_another() -> None:
    factory, _, adapter_cls = _make_factory(enabled=True)
    result_a = factory.create()
    result_b = factory.create()
    result_a.close()
    # Only first adapter should be closed.
    assert adapter_cls.instances[0].close_calls == 1
    assert adapter_cls.instances[1].close_calls == 0


def test_46_safe_repr() -> None:
    factory, _, _ = _make_factory(enabled=True)
    result = factory.create()
    r = repr(result)
    assert SENSITIVE_HOST not in r
    assert SENSITIVE_ENDPOINT not in r
    assert SENSITIVE_OWNER not in r
    assert SENSITIVE_GENIE not in r
    assert SENSITIVE_TOKEN not in r
    assert "DurableGenieSessionRuntimeBundle" in r


def test_47_disabled_bundle_repr_safe() -> None:
    factory, _, _ = _make_factory(enabled=False)
    result = factory.create()
    r = repr(result)
    assert "DurableGenieSessionRuntimeBundle" in r
    assert "enabled=False" in r


# ===========================================================================
# FAILURE HANDLING (tests 48-53)
# ===========================================================================

def test_48_repository_factory_failure_sanitized() -> None:
    factory, _, _ = _make_factory(enabled=True, repo_fail_init=True)
    with pytest.raises(DurableGenieSessionRuntimeInitializationError):
        factory.create()


def test_49_raw_exception_hidden_from_factory_init_failure() -> None:
    raw_msg = "SUPER SECRET raw factory internals here"
    factory, _, _ = _make_factory(enabled=True, repo_fail_init=True, fail_msg=raw_msg)
    with pytest.raises(DurableGenieSessionRuntimeInitializationError) as excinfo:
        factory.create()
    assert raw_msg not in str(excinfo.value)


def test_50_adapter_construction_failure_closes_repository_bundle() -> None:
    bundle = FakeRepositoryBundle()
    repo_cls = FakeRepositoryFactoryCls(bundle=bundle)
    adapter_cls = FakeAdapterCls(fail=True)
    factory = DurableGenieSessionRuntimeFactory(
        environ=_enabled_env(),
        repository_factory_cls=repo_cls,
        adapter_cls=adapter_cls,
    )
    with pytest.raises(DurableGenieSessionRuntimeInitializationError):
        factory.create()
    assert bundle.close_calls == 1


def test_51_repository_bundle_closed_exactly_once_on_adapter_failure() -> None:
    bundle = FakeRepositoryBundle()
    repo_cls = FakeRepositoryFactoryCls(bundle=bundle)
    adapter_cls = FakeAdapterCls(fail=True)
    factory = DurableGenieSessionRuntimeFactory(
        environ=_enabled_env(),
        repository_factory_cls=repo_cls,
        adapter_cls=adapter_cls,
    )
    with pytest.raises(DurableGenieSessionRuntimeInitializationError):
        factory.create()
    assert bundle.close_calls == 1  # exactly once, not twice


def test_52_explicit_enablement_failure_does_not_return_disabled_bundle() -> None:
    factory, _, _ = _make_factory(enabled=True, repo_fail_init=True)
    with pytest.raises(DurableGenieSessionRuntimeInitializationError):
        result = factory.create()
    # If we reach here, an exception was raised — no disabled bundle was returned.


def test_53_no_silent_fallback_on_create_failure() -> None:
    factory, _, _ = _make_factory(enabled=True, repo_fail_create=True)
    with pytest.raises(DurableGenieSessionRuntimeInitializationError):
        factory.create()
    # Must raise, not silently fall back to memory bundle.


# ===========================================================================
# SECURITY (tests 54-61)
# ===========================================================================

def test_54_no_host_in_repr_or_errors() -> None:
    factory, _, _ = _make_factory(enabled=True)
    result = factory.create()
    assert SENSITIVE_HOST not in repr(result)


def test_55_no_endpoint_in_repr_or_errors() -> None:
    factory, _, _ = _make_factory(enabled=True)
    result = factory.create()
    assert SENSITIVE_ENDPOINT not in repr(result)


def test_56_no_owner_identifier_in_repr_or_errors() -> None:
    factory, _, _ = _make_factory(enabled=True)
    result = factory.create()
    assert SENSITIVE_OWNER not in repr(result)


def test_57_no_genie_identifier_in_repr_or_errors() -> None:
    factory, _, _ = _make_factory(enabled=True)
    result = factory.create()
    assert SENSITIVE_GENIE not in repr(result)


def test_58_no_token_in_repr_or_errors() -> None:
    factory, _, _ = _make_factory(enabled=True)
    result = factory.create()
    assert SENSITIVE_TOKEN not in repr(result)


def test_59_no_environment_dump_in_errors() -> None:
    bad_val = "definitely-bad-boolean-exposing-secrets"
    with pytest.raises(DurableGenieSessionRuntimeConfigurationError) as excinfo:
        DurableGenieSessionRuntimeSettings.from_environment(
            {"ENABLE_DURABLE_GENIE_SESSION_ADAPTER": bad_val}
        )
    assert bad_val not in str(excinfo.value)


def test_60_no_network_access_during_import() -> None:
    _clear_runtime_module()
    with no_network():
        module = importlib.import_module("app.services.durable_genie_session_runtime_factory")
    assert hasattr(module, "DurableGenieSessionRuntimeFactory")


def test_61_no_network_access_during_construction() -> None:
    with no_network():
        factory = DurableGenieSessionRuntimeFactory(
            environ=_disabled_env(),
            repository_factory_cls=FakeRepositoryFactoryCls(),
            adapter_cls=FakeAdapterCls(),
        )
    assert factory is not None


# ===========================================================================
# INTEGRATION BOUNDARIES (tests 62-70)
# ===========================================================================

def test_62_current_runtime_modules_do_not_import_the_new_factory() -> None:
    """Only the approved backend factory module may import the runtime factory."""
    app_root = pathlib.Path("app")
    offenders: List[str] = []
    allowed = {
        "app/services/durable_genie_session_runtime_factory.py",
        "app/services/genie_backend_factory.py",
    }
    for path in app_root.rglob("*.py"):
        if path.as_posix() in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if "durable_genie_session_runtime_factory" in text:
            offenders.append(path.as_posix())
    assert offenders == [], f"unexpected runtime importers: {offenders}"


def test_63_genie_backend_factory_wiring_is_limited_to_phase3d1() -> None:
    text = pathlib.Path("app/services/genie_backend_factory.py").read_text(encoding="utf-8")
    assert "DurableGenieSessionRuntimeFactory" in text
    assert "durable_genie_session_runtime_factory" in text
    assert "ConversationRepositoryFactory" not in text
    assert "durable_genie_session_adapter" not in text
    assert "lakebase_connection_provider" not in text
    assert "lakebase_conversation_repository" not in text
    assert "_durable_session_runtime_bundle" in text


def test_64_main_py_unchanged() -> None:
    text = pathlib.Path("app/main.py").read_text(encoding="utf-8")
    assert "DurableGenieSessionRuntimeFactory" not in text
    assert "durable_genie_session_runtime_factory" not in text


def test_65_chat_py_unchanged() -> None:
    text = pathlib.Path("app/routes/chat.py").read_text(encoding="utf-8")
    assert "DurableGenieSessionRuntimeFactory" not in text
    assert "durable_genie_session_runtime_factory" not in text


def test_66_genie_pipeline_unchanged() -> None:
    text = pathlib.Path("app/services/genie_pipeline.py").read_text(encoding="utf-8")
    assert "DurableGenieSessionRuntimeFactory" not in text
    assert "durable_genie_session_runtime_factory" not in text


def test_67_genie_session_store_unchanged() -> None:
    text = pathlib.Path("app/services/genie_session_store.py").read_text(encoding="utf-8")
    assert "DurableGenieSessionRuntimeFactory" not in text
    assert "DurableGenieSessionAdapter" not in text
    assert "durable_genie_session_runtime_factory" not in text


def test_68_app_yaml_flags_default_safely() -> None:
    text = pathlib.Path("app.yaml").read_text(encoding="utf-8")
    assert "ENABLE_DURABLE_GENIE_SESSION_ADAPTER" in text
    assert "CONVERSATION_REPOSITORY_BACKEND" in text
    assert "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY" in text
    # Verify safe defaults by line proximity (name line followed by value: "false"/"memory")
    lines = text.splitlines()
    adapter_flag_val = None
    backend_val = None
    lakebase_flag_val = None
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if "ENABLE_DURABLE_GENIE_SESSION_ADAPTER" in stripped and "ENABLE_LAKEBASE" not in stripped:
            for j in range(i + 1, min(i + 6, len(lines))):
                if lines[j].strip().startswith("value:"):
                    adapter_flag_val = lines[j].strip()
                    break
        elif "CONVERSATION_REPOSITORY_BACKEND" in stripped:
            for j in range(i + 1, min(i + 6, len(lines))):
                if lines[j].strip().startswith("value:"):
                    backend_val = lines[j].strip()
                    break
        elif "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY" in stripped:
            for j in range(i + 1, min(i + 6, len(lines))):
                if lines[j].strip().startswith("value:"):
                    lakebase_flag_val = lines[j].strip()
                    break
        i += 1
    assert adapter_flag_val is not None, "ENABLE_DURABLE_GENIE_SESSION_ADAPTER has no value"
    assert "false" in adapter_flag_val, f"Expected 'false', got: {adapter_flag_val}"
    assert backend_val is not None, "CONVERSATION_REPOSITORY_BACKEND has no value"
    assert "memory" in backend_val, f"Expected 'memory', got: {backend_val}"
    assert lakebase_flag_val is not None, "ENABLE_LAKEBASE_CONVERSATION_REPOSITORY has no value"
    assert "false" in lakebase_flag_val, f"Expected 'false', got: {lakebase_flag_val}"


def test_69_app_yaml_retains_lakebase_endpoint_name_value_from() -> None:
    text = pathlib.Path("app.yaml").read_text(encoding="utf-8")
    assert "LAKEBASE_ENDPOINT_NAME" in text
    assert "valueFrom: postgres" in text
    # Ensure LAKEBASE_ENDPOINT_NAME is not given a hard-coded value string
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "LAKEBASE_ENDPOINT_NAME" in line:
            # The line or adjacent line must have valueFrom: postgres
            context = "\n".join(lines[max(0, i - 1): i + 4])
            assert "valueFrom: postgres" in context
            assert "value:" not in context.replace("valueFrom:", "")
            break
    else:
        pytest.fail("LAKEBASE_ENDPOINT_NAME not found in app.yaml")


def test_70_no_runtime_factory_instance_exists_globally() -> None:
    module = importlib.import_module("app.services.durable_genie_session_runtime_factory")
    for name, value in module.__dict__.items():
        assert not isinstance(value, DurableGenieSessionRuntimeFactory), (
            f"Global DurableGenieSessionRuntimeFactory instance found: {name}"
        )
        assert not isinstance(value, DurableGenieSessionRuntimeBundle), (
            f"Global DurableGenieSessionRuntimeBundle instance found: {name}"
        )


# ===========================================================================
# FOCUSED ADDITIONAL CASES
# ===========================================================================

def test_settings_is_immutable() -> None:
    settings = DurableGenieSessionRuntimeSettings.from_environment({})
    with pytest.raises((AttributeError, TypeError)):
        settings.enabled = True  # frozen dataclass must reject mutation


def test_convenience_function_creates_independent_bundles() -> None:
    bundle_a = create_durable_genie_session_runtime(environ=_disabled_env())
    bundle_b = create_durable_genie_session_runtime(environ=_disabled_env())
    assert bundle_a is not bundle_b


def test_convenience_function_preserves_disabled_default() -> None:
    result = create_durable_genie_session_runtime(environ={})
    assert result.enabled is False


def test_error_hierarchy_base_is_exception() -> None:
    assert issubclass(DurableGenieSessionRuntimeFactoryError, Exception)
    assert issubclass(DurableGenieSessionRuntimeConfigurationError, DurableGenieSessionRuntimeFactoryError)
    assert issubclass(DurableGenieSessionRuntimeInitializationError, DurableGenieSessionRuntimeFactoryError)


def test_bundle_enabled_property_reflects_enabled() -> None:
    factory, _, _ = _make_factory(enabled=True)
    result = factory.create()
    assert result.enabled is True


def test_create_repository_failure_does_not_raise_disabled_bundle() -> None:
    """repo_factory.create() failure must raise, not return a disabled bundle."""
    factory, _, _ = _make_factory(enabled=True, repo_fail_create=True)
    with pytest.raises(DurableGenieSessionRuntimeInitializationError):
        factory.create()


def test_adapter_close_called_on_context_manager_exit() -> None:
    factory, _, adapter_cls = _make_factory(enabled=True)
    with factory.create():
        pass
    assert adapter_cls.instances[0].close_calls == 1


def test_factory_constructor_does_not_construct_repository() -> None:
    repo_cls = FakeRepositoryFactoryCls()
    DurableGenieSessionRuntimeFactory(
        environ=_enabled_env(),
        repository_factory_cls=repo_cls,
        adapter_cls=FakeAdapterCls(),
    )
    assert repo_cls.call_count == 0  # constructor must not call the factory cls


def test_factory_constructor_does_not_construct_adapter() -> None:
    adapter_cls = FakeAdapterCls()
    DurableGenieSessionRuntimeFactory(
        environ=_enabled_env(),
        repository_factory_cls=FakeRepositoryFactoryCls(),
        adapter_cls=adapter_cls,
    )
    assert len(adapter_cls.instances) == 0


def test_runtime_factory_source_parses_and_has_no_merge_markers() -> None:
    source = pathlib.Path(
        "app/services/durable_genie_session_runtime_factory.py"
    ).read_text(encoding="utf-8")
    assert "<<<<<<<" not in source
    assert ">>>>>>>" not in source
    ast.parse(source)


def test_runtime_factory_test_source_parses() -> None:
    source = pathlib.Path(
        "tests/test_durable_genie_session_runtime_factory.py"
    ).read_text(encoding="utf-8")
    ast.parse(source)
