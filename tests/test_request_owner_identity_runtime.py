"""Phase 4B2 — Runtime flag and resolution tests.

Covers all 40 requirements specified for tests/test_request_owner_identity_runtime.py:
  Settings (1–11): parsing, repr safety, configuration error hiding
  Disabled resolution (12–22): returns None, no side effects
  Enabled resolution (23–34): provider lifecycle, forwarding, error surfacing
  Security and boundaries (35–40): no globals, no logging, no framework imports
"""
from __future__ import annotations

import importlib.util
import os
import socket
import sys
from pathlib import Path
from typing import Mapping, Optional
from unittest.mock import MagicMock, call, patch

import pytest

from app.services.request_owner_identity_runtime import (
    REQUEST_OWNER_IDENTITY_STATE_ATTRIBUTE,
    RequestOwnerIdentityRuntime,
    RequestOwnerIdentityRuntimeConfigurationError,
    RequestOwnerIdentityRuntimeError,
    RequestOwnerIdentityRuntimeResolutionError,
    RequestOwnerIdentityRuntimeSettings,
    resolve_request_owner_identity,
)

# ---------------------------------------------------------------------------
# Module-level constants for tests
# ---------------------------------------------------------------------------

_MODULE_PATH = Path("app/services/request_owner_identity_runtime.py")
_VALID_SECRET = "0123456789abcdef0123456789abcdef"  # 32 chars ≥ 32 bytes
_VALID_HEADERS = {"X-Forwarded-User": "alice@example.com"}
_FLAG_ENV_VAR = "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY"
_SECRET_ENV_VAR = "CONVERSATION_OWNER_HMAC_SECRET"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _EnvTrap(dict):
    """Dict subclass that raises on .get() to detect env reads."""
    def get(self, key, default=None):
        raise AssertionError(f"Unexpected environment read for {key!r}")


class _SelectiveEnvTrap(dict):
    """Dict that allows reading the flag but raises on HMAC secret reads."""
    def __init__(self, flag_value: str = "false"):
        super().__init__({_FLAG_ENV_VAR: flag_value})

    def get(self, key, default=None):
        if key == _SECRET_ENV_VAR:
            raise AssertionError(
                f"Forbidden read of HMAC secret env var {key!r} on the disabled path"
            )
        return super().get(key, default)


def _fresh_module_import(alias: str):
    """Import the runtime module under a fresh alias to test import-time behavior."""
    spec = importlib.util.spec_from_file_location(alias, _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    try:
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.modules.pop(alias, None)


def _make_identity(principal: str = "alice@example.com"):
    """Create a real RequestOwnerIdentity via the provider."""
    from app.services.request_owner_identity import (
        RequestOwnerIdentityProvider,
        RequestOwnerIdentitySettings,
    )
    settings = RequestOwnerIdentitySettings(hmac_secret=_VALID_SECRET.encode())
    provider = RequestOwnerIdentityProvider(settings)
    return provider.derive_from_header_value(principal)


def _enabled_environ() -> dict:
    return {
        _FLAG_ENV_VAR: "true",
        _SECRET_ENV_VAR: _VALID_SECRET,
    }


def _disabled_environ(value: str = "false") -> dict:
    return {_FLAG_ENV_VAR: value}


# ---------------------------------------------------------------------------
# Tests 1–11: Settings
# ---------------------------------------------------------------------------


class TestSettings:
    # Test 1: default disabled (absent variable → empty string → false)
    def test_default_disabled(self):
        s = RequestOwnerIdentityRuntimeSettings.from_environment({})
        assert s.enabled is False

    # Test 2: explicit false
    def test_explicit_false(self):
        s = RequestOwnerIdentityRuntimeSettings.from_environment({_FLAG_ENV_VAR: "false"})
        assert s.enabled is False

    # Test 3: explicit true
    def test_explicit_true(self):
        s = RequestOwnerIdentityRuntimeSettings.from_environment({_FLAG_ENV_VAR: "true"})
        assert s.enabled is True

    # Test 4: accepted true values
    @pytest.mark.parametrize("val", ["true", "1", "yes", "on"])
    def test_accepted_true_values(self, val):
        s = RequestOwnerIdentityRuntimeSettings.from_environment({_FLAG_ENV_VAR: val})
        assert s.enabled is True

    # Test 5: accepted false values
    @pytest.mark.parametrize("val", ["false", "0", "no", "off", ""])
    def test_accepted_false_values(self, val):
        s = RequestOwnerIdentityRuntimeSettings.from_environment({_FLAG_ENV_VAR: val})
        assert s.enabled is False

    # Test 6: case-insensitive
    @pytest.mark.parametrize("val", ["True", "TRUE", "FALSE", "YES", "NO", "ON", "OFF"])
    def test_case_insensitive_parsing(self, val):
        s = RequestOwnerIdentityRuntimeSettings.from_environment({_FLAG_ENV_VAR: val})
        assert isinstance(s.enabled, bool)

    # Test 7: surrounding whitespace stripped
    def test_whitespace_stripped_true(self):
        s = RequestOwnerIdentityRuntimeSettings.from_environment({_FLAG_ENV_VAR: "  true  "})
        assert s.enabled is True

    def test_whitespace_stripped_false(self):
        s = RequestOwnerIdentityRuntimeSettings.from_environment({_FLAG_ENV_VAR: "  false  "})
        assert s.enabled is False

    # Test 8: invalid value rejected
    @pytest.mark.parametrize("val", ["maybe", "enabled", "disabled", "2", "t", "f"])
    def test_invalid_value_rejected(self, val):
        with pytest.raises(RequestOwnerIdentityRuntimeConfigurationError):
            RequestOwnerIdentityRuntimeSettings.from_environment({_FLAG_ENV_VAR: val})

    # Test 9: import performs no environment read
    def test_import_performs_no_environment_read(self, monkeypatch):
        monkeypatch.setattr(os, "environ", _EnvTrap())
        mod = _fresh_module_import("_phase4b2_settings_import_trap")
        assert hasattr(mod, "RequestOwnerIdentityRuntimeSettings")

    # Test 10: settings repr is safe
    def test_settings_repr_safe(self):
        s = RequestOwnerIdentityRuntimeSettings.from_environment({_FLAG_ENV_VAR: "true"})
        rendered = repr(s)
        assert "enabled=True" in rendered
        assert _VALID_SECRET not in rendered

    # Test 11: configuration errors hide raw values
    def test_configuration_error_hides_raw_value(self):
        raw = "dangerous-bad-value"
        with pytest.raises(RequestOwnerIdentityRuntimeConfigurationError) as exc:
            RequestOwnerIdentityRuntimeSettings.from_environment({_FLAG_ENV_VAR: raw})
        assert raw not in str(exc.value)


# ---------------------------------------------------------------------------
# Tests 12–22: Disabled resolution
# ---------------------------------------------------------------------------


class TestDisabledResolution:
    # Test 12: returns None
    def test_returns_none_when_disabled(self):
        runtime = RequestOwnerIdentityRuntime(environment=_disabled_environ())
        result = runtime.resolve_from_headers(_VALID_HEADERS)
        assert result is None

    # Test 13: does not read HMAC secret
    def test_does_not_read_hmac_secret(self):
        env = _SelectiveEnvTrap(flag_value="false")
        runtime = RequestOwnerIdentityRuntime(environment=env)
        # Must not raise AssertionError from _SelectiveEnvTrap.get(HMAC_SECRET)
        result = runtime.resolve_from_headers(_VALID_HEADERS)
        assert result is None

    # Test 14: does not construct provider
    def test_does_not_construct_provider(self):
        factory_called = []
        def spy_factory(*, environ=None):
            factory_called.append(environ)
            raise AssertionError("provider_factory must not be called on disabled path")
        runtime = RequestOwnerIdentityRuntime(
            environment=_disabled_environ(), provider_factory=spy_factory
        )
        runtime.resolve_from_headers(_VALID_HEADERS)
        assert factory_called == []

    # Test 15: does not inspect X-Forwarded-User
    def test_does_not_inspect_forwarded_user_header(self):
        class _HeaderTrap(dict):
            def __getitem__(self, key):
                raise AssertionError(f"Header must not be accessed: {key!r}")
            def keys(self):
                raise AssertionError("Header keys must not be accessed")
        runtime = RequestOwnerIdentityRuntime(environment=_disabled_environ())
        # Must not raise from _HeaderTrap
        result = runtime.resolve_from_headers(_HeaderTrap())
        assert result is None

    # Test 16: ignores malformed trusted header while disabled
    def test_ignores_malformed_trusted_header_when_disabled(self):
        malformed = {"X-Forwarded-User": ",invalid,user,"}
        runtime = RequestOwnerIdentityRuntime(environment=_disabled_environ())
        assert runtime.resolve_from_headers(malformed) is None

    # Test 17: ignores missing trusted header while disabled
    def test_ignores_missing_trusted_header_when_disabled(self):
        runtime = RequestOwnerIdentityRuntime(environment=_disabled_environ())
        assert runtime.resolve_from_headers({}) is None

    # Test 18: no legacy header inspected
    def test_no_legacy_email_header_inspected_when_disabled(self):
        headers = {"X-Forwarded-Email": "user@example.com", "X-User-Email": "user@example.com"}
        runtime = RequestOwnerIdentityRuntime(environment=_disabled_environ())
        assert runtime.resolve_from_headers(headers) is None

    # Test 19: no network
    def test_no_network_when_disabled(self, monkeypatch):
        def _trap(*args, **kwargs):
            raise AssertionError("Network access is forbidden on the disabled path")
        monkeypatch.setattr(socket, "create_connection", _trap)
        runtime = RequestOwnerIdentityRuntime(environment=_disabled_environ())
        assert runtime.resolve_from_headers(_VALID_HEADERS) is None

    # Test 20: no credential
    def test_no_credential_when_disabled(self):
        # Verified by absence of provider factory call; provider generates the credential.
        runtime = RequestOwnerIdentityRuntime(
            environment=_disabled_environ(),
            provider_factory=lambda **_: (_ for _ in ()).throw(AssertionError("no credential")),
        )
        assert runtime.resolve_from_headers(_VALID_HEADERS) is None

    # Test 21: no pool
    def test_no_pool_opened_when_disabled(self):
        source = _MODULE_PATH.read_text(encoding="utf-8")
        assert "ConnectionPool" not in source
        assert "psycopg" not in source

    # Test 22: no SQL
    def test_no_sql_when_disabled(self):
        source = _MODULE_PATH.read_text(encoding="utf-8").upper()
        assert "SELECT " not in source
        assert "INSERT " not in source
        assert "UPDATE " not in source
        assert "DELETE " not in source


# ---------------------------------------------------------------------------
# Tests 23–34: Enabled resolution
# ---------------------------------------------------------------------------


class TestEnabledResolution:
    # Test 23: provider constructed once per call
    def test_provider_constructed_once_per_call(self):
        expected_identity = _make_identity()
        construction_count = [0]

        def counting_factory(*, environ=None):
            construction_count[0] += 1
            provider = MagicMock()
            provider.derive_from_headers.return_value = expected_identity
            return provider

        runtime = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=counting_factory
        )
        runtime.resolve_from_headers(_VALID_HEADERS)
        assert construction_count[0] == 1

        runtime.resolve_from_headers(_VALID_HEADERS)
        assert construction_count[0] == 2  # new provider per call

    # Test 24: environment mapping forwarded to factory
    def test_environment_mapping_forwarded_to_factory(self):
        env = _enabled_environ()
        received = []

        def capturing_factory(*, environ=None):
            received.append(environ)
            provider = MagicMock()
            provider.derive_from_headers.return_value = _make_identity()
            return provider

        runtime = RequestOwnerIdentityRuntime(
            environment=env, provider_factory=capturing_factory
        )
        runtime.resolve_from_headers(_VALID_HEADERS)
        assert received[0] is env

    # Test 25: exact headers mapping forwarded to provider
    def test_exact_headers_forwarded_to_provider(self):
        headers = {"X-Forwarded-User": "alice@example.com", "Other-Header": "x"}
        received = []

        def capturing_factory(*, environ=None):
            provider = MagicMock()
            def _capture(h):
                received.append(h)
                return _make_identity()
            provider.derive_from_headers.side_effect = _capture
            return provider

        runtime = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=capturing_factory
        )
        runtime.resolve_from_headers(headers)
        assert received[0] is headers

    # Test 26: identity returned unchanged
    def test_identity_returned_unchanged(self):
        expected = _make_identity("bob@example.com")

        def _factory(*, environ=None):
            p = MagicMock()
            p.derive_from_headers.return_value = expected
            return p

        runtime = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=_factory
        )
        result = runtime.resolve_from_headers(_VALID_HEADERS)
        assert result is expected

    # Test 27: missing trusted header surfaced as resolution error
    def test_missing_trusted_header_surfaced(self):
        from app.services.request_owner_identity import RequestOwnerIdentityMissingError

        def _factory(*, environ=None):
            p = MagicMock()
            p.derive_from_headers.side_effect = RequestOwnerIdentityMissingError(
                "Trusted request owner identity header is missing."
            )
            return p

        runtime = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=_factory
        )
        with pytest.raises(RequestOwnerIdentityRuntimeResolutionError):
            runtime.resolve_from_headers({})

    # Test 28: invalid trusted header surfaced as resolution error
    def test_invalid_trusted_header_surfaced(self):
        from app.services.request_owner_identity import RequestOwnerIdentityInvalidError

        def _factory(*, environ=None):
            p = MagicMock()
            p.derive_from_headers.side_effect = RequestOwnerIdentityInvalidError(
                "Trusted request owner identity contains unsupported characters."
            )
            return p

        runtime = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=_factory
        )
        with pytest.raises(RequestOwnerIdentityRuntimeResolutionError):
            runtime.resolve_from_headers(_VALID_HEADERS)

    # Test 29: configuration failure surfaced safely
    def test_configuration_failure_surfaced_safely(self):
        from app.services.request_owner_identity import RequestOwnerIdentityConfigurationError

        def _factory(*, environ=None):
            raise RequestOwnerIdentityConfigurationError("secret=VERY_SECRET_VALUE")

        runtime = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=_factory
        )
        with pytest.raises(RequestOwnerIdentityRuntimeConfigurationError) as exc:
            runtime.resolve_from_headers(_VALID_HEADERS)
        # Raw configuration error must not be exposed
        assert "VERY_SECRET_VALUE" not in str(exc.value)
        assert "Trusted request identity is unavailable" in str(exc.value)

    # Test 30: no fallback to legacy email or anonymous
    def test_no_fallback_to_legacy_email(self):
        from app.services.request_owner_identity import RequestOwnerIdentityMissingError

        def _factory(*, environ=None):
            p = MagicMock()
            p.derive_from_headers.side_effect = RequestOwnerIdentityMissingError("missing")
            return p

        runtime = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=_factory
        )
        # Must raise, not fall back to anonymous
        with pytest.raises(RequestOwnerIdentityRuntimeResolutionError):
            runtime.resolve_from_headers({"X-Forwarded-Email": "fallback@example.com"})

    # Test 31: no caching between calls
    def test_no_caching_between_calls(self):
        identities = [
            _make_identity("alice@example.com"),
            _make_identity("bob@example.com"),
        ]
        call_index = [0]

        def _factory(*, environ=None):
            p = MagicMock()
            p.derive_from_headers.return_value = identities[call_index[0]]
            call_index[0] += 1
            return p

        runtime = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=_factory
        )
        r1 = runtime.resolve_from_headers(_VALID_HEADERS)
        r2 = runtime.resolve_from_headers(_VALID_HEADERS)
        assert r1 is not r2

    # Test 32: independent calls are isolated
    def test_independent_calls_are_isolated(self):
        def _factory_a(*, environ=None):
            p = MagicMock()
            p.derive_from_headers.return_value = _make_identity("alice@example.com")
            return p

        def _factory_b(*, environ=None):
            p = MagicMock()
            p.derive_from_headers.return_value = _make_identity("bob@example.com")
            return p

        ra = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=_factory_a
        )
        rb = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=_factory_b
        )
        id_a = ra.resolve_from_headers(_VALID_HEADERS)
        id_b = rb.resolve_from_headers(_VALID_HEADERS)
        assert id_a != id_b

    # Test 33: repr and errors hide identity
    def test_resolution_error_hides_identity(self):
        from app.services.request_owner_identity import RequestOwnerIdentityInvalidError
        principal = "secret-principal@example.com"

        def _factory(*, environ=None):
            p = MagicMock()
            p.derive_from_headers.side_effect = RequestOwnerIdentityInvalidError(
                f"invalid: {principal}"
            )
            return p

        runtime = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=_factory
        )
        with pytest.raises(RequestOwnerIdentityRuntimeResolutionError) as exc:
            runtime.resolve_from_headers({"X-Forwarded-User": principal})
        # Surfaced error must not contain the raw principal
        assert principal not in str(exc.value)

    # Test 34: repr and errors hide secret
    def test_configuration_error_hides_secret(self):
        def _factory(*, environ=None):
            raise RuntimeError(f"secret_key={_VALID_SECRET}")

        runtime = RequestOwnerIdentityRuntime(
            environment=_enabled_environ(), provider_factory=_factory
        )
        with pytest.raises(RequestOwnerIdentityRuntimeConfigurationError) as exc:
            runtime.resolve_from_headers(_VALID_HEADERS)
        assert _VALID_SECRET not in str(exc.value)


# ---------------------------------------------------------------------------
# Tests 35–40: Security and boundaries
# ---------------------------------------------------------------------------


class TestSecurityAndBoundaries:
    def _source(self) -> str:
        return _MODULE_PATH.read_text(encoding="utf-8")

    # Test 35: no global provider singleton
    def test_no_global_provider_singleton(self):
        mod = _fresh_module_import("_phase4b2_runtime_global_provider_test")
        from app.services.request_owner_identity import RequestOwnerIdentityProvider
        assert not any(
            isinstance(v, RequestOwnerIdentityProvider)
            for v in vars(mod).values()
        )

    # Test 36: no global identity
    def test_no_global_identity(self):
        mod = _fresh_module_import("_phase4b2_runtime_global_identity_test")
        from app.services.request_owner_identity import RequestOwnerIdentity
        # Module-level variables must not hold an identity instance
        assert not any(
            isinstance(v, RequestOwnerIdentity)
            for v in vars(mod).values()
        )

    # Test 37: no logging of principal
    def test_no_logging_of_principal_in_source(self):
        source = self._source()
        # Must not contain logger.* calls that could capture principal values
        assert "logger." not in source
        assert "logging." not in source

    # Test 38: no logging of secret
    def test_no_logging_of_secret_in_source(self):
        source = self._source()
        assert "print(" not in source
        assert "logger." not in source

    # Test 39: no direct request (FastAPI/Starlette) dependency in production module
    def test_no_direct_request_dependency(self):
        source = self._source()
        assert "from fastapi" not in source
        assert "from starlette" not in source
        assert "import fastapi" not in source
        assert "import starlette" not in source

    # Test 40: production module does not import FastAPI or Starlette
    def test_production_module_does_not_import_fastapi_or_starlette(self):
        mod = _fresh_module_import("_phase4b2_runtime_no_fastapi_test")
        assert "fastapi" not in sys.modules.get(
            "_phase4b2_runtime_no_fastapi_test", type("_M", (), {"__dict__": {}})().__dict__
        )
        source = self._source()
        assert "fastapi" not in source
        assert "starlette" not in source


# ---------------------------------------------------------------------------
# Additional: Convenience function tests
# ---------------------------------------------------------------------------


class TestConvenienceFunction:
    def test_disabled_returns_none(self):
        result = resolve_request_owner_identity(
            headers=_VALID_HEADERS,
            environ=_disabled_environ(),
        )
        assert result is None

    def test_enabled_returns_identity(self):
        expected = _make_identity()
        with patch(
            "app.services.request_owner_identity_runtime._default_provider_factory"
        ) as mock_factory:
            mock_factory.return_value = MagicMock(
                derive_from_headers=MagicMock(return_value=expected)
            )
            result = resolve_request_owner_identity(
                headers=_VALID_HEADERS,
                environ=_enabled_environ(),
            )
        assert result is expected

    def test_no_module_level_env_read(self, monkeypatch):
        monkeypatch.setattr(os, "environ", _EnvTrap())
        # Importing the function must not raise; it reads env only when called
        from app.services.request_owner_identity_runtime import resolve_request_owner_identity as fn
        # Calling with explicit environ must not touch os.environ
        result = fn(headers={}, environ={_FLAG_ENV_VAR: "false"})
        assert result is None

    def test_request_owner_identity_state_attribute_constant(self):
        assert REQUEST_OWNER_IDENTITY_STATE_ATTRIBUTE == "request_owner_identity"

    def test_error_hierarchy(self):
        assert issubclass(RequestOwnerIdentityRuntimeConfigurationError, RequestOwnerIdentityRuntimeError)
        assert issubclass(RequestOwnerIdentityRuntimeResolutionError, RequestOwnerIdentityRuntimeError)
