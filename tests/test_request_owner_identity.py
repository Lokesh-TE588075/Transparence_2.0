from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import hmac
import importlib.util
import socket
import sys
from pathlib import Path
import unicodedata

import pytest

from app.services.request_owner_identity import (
    RequestOwnerIdentity,
    RequestOwnerIdentityConfigurationError,
    RequestOwnerIdentityInvalidError,
    RequestOwnerIdentityMissingError,
    RequestOwnerIdentityProvider,
    RequestOwnerIdentitySettings,
    create_request_owner_identity_provider,
)


MODULE_PATH = Path("app/services/request_owner_identity.py")
HEADER_NAME = "X-Forwarded-User"
SOURCE_LABEL = "x-forwarded-user"
DOMAIN_PREFIX = b"transparence-owner-identity:v1\x00"
VALID_SECRET_TEXT = "0123456789abcdef0123456789abcdef"
VALID_SECRET_BYTES = VALID_SECRET_TEXT.encode("utf-8")


class _EnvironmentAccessTrap(dict):
    def get(self, key, default=None):
        raise AssertionError(f"unexpected environment read for {key!r}")


class _SocketTrap:
    def __call__(self, *args, **kwargs):
        raise AssertionError("unexpected network access")


def _fresh_import_under_alias(alias: str):
    spec = importlib.util.spec_from_file_location(alias, MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(alias, None)


def _provider(secret_text: str = VALID_SECRET_TEXT) -> RequestOwnerIdentityProvider:
    return RequestOwnerIdentityProvider(
        RequestOwnerIdentitySettings(hmac_secret=secret_text.encode("utf-8"))
    )


def _expected_digest(secret_text: str, principal: str) -> str:
    return hmac.new(
        secret_text.encode("utf-8"),
        DOMAIN_PREFIX + principal.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _module_source() -> str:
    return MODULE_PATH.read_text(encoding="utf-8")


def _asset_source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


class TestConfiguration:
    def test_import_performs_no_environment_read(self, monkeypatch):
        import os

        monkeypatch.setattr(os, "environ", _EnvironmentAccessTrap())
        module = _fresh_import_under_alias("phase4a_request_owner_identity_import_test")
        assert hasattr(module, "RequestOwnerIdentitySettings")

    def test_injected_environment_supported(self):
        settings = RequestOwnerIdentitySettings.from_environment(
            {"CONVERSATION_OWNER_HMAC_SECRET": VALID_SECRET_TEXT}
        )
        assert settings.hmac_secret == VALID_SECRET_BYTES

    def test_missing_secret_rejected(self):
        with pytest.raises(RequestOwnerIdentityConfigurationError) as exc:
            RequestOwnerIdentitySettings.from_environment({})
        assert VALID_SECRET_TEXT not in str(exc.value)

    def test_blank_secret_rejected(self):
        with pytest.raises(RequestOwnerIdentityConfigurationError) as exc:
            RequestOwnerIdentitySettings.from_environment(
                {"CONVERSATION_OWNER_HMAC_SECRET": ""}
            )
        assert "blank" in str(exc.value).lower()
        assert VALID_SECRET_TEXT not in str(exc.value)

    def test_short_secret_rejected(self):
        short_secret = "too-short-secret"
        with pytest.raises(RequestOwnerIdentityConfigurationError) as exc:
            RequestOwnerIdentitySettings.from_environment(
                {"CONVERSATION_OWNER_HMAC_SECRET": short_secret}
            )
        assert short_secret not in str(exc.value)

    def test_valid_secret_accepted(self):
        settings = RequestOwnerIdentitySettings.from_environment(
            {"CONVERSATION_OWNER_HMAC_SECRET": VALID_SECRET_TEXT}
        )
        assert settings.hmac_secret == VALID_SECRET_BYTES

    def test_leading_whitespace_rejected(self):
        secret = " " + VALID_SECRET_TEXT
        with pytest.raises(RequestOwnerIdentityConfigurationError) as exc:
            RequestOwnerIdentitySettings.from_environment(
                {"CONVERSATION_OWNER_HMAC_SECRET": secret}
            )
        assert secret not in str(exc.value)

    def test_trailing_whitespace_rejected(self):
        secret = VALID_SECRET_TEXT + " "
        with pytest.raises(RequestOwnerIdentityConfigurationError) as exc:
            RequestOwnerIdentitySettings.from_environment(
                {"CONVERSATION_OWNER_HMAC_SECRET": secret}
            )
        assert secret not in str(exc.value)

    def test_no_default_secret(self):
        with pytest.raises(RequestOwnerIdentityConfigurationError):
            create_request_owner_identity_provider(environ={})

    def test_no_random_secret_generated(self):
        with pytest.raises(RequestOwnerIdentityConfigurationError):
            create_request_owner_identity_provider(environ={})
        with pytest.raises(RequestOwnerIdentityConfigurationError):
            create_request_owner_identity_provider(environ={})

    def test_configuration_error_hides_secret(self):
        secret = "short-secret-material"
        with pytest.raises(RequestOwnerIdentityConfigurationError) as exc:
            RequestOwnerIdentitySettings.from_environment(
                {"CONVERSATION_OWNER_HMAC_SECRET": secret}
            )
        assert secret not in str(exc.value)

    def test_settings_repr_hides_secret(self):
        settings = RequestOwnerIdentitySettings(hmac_secret=VALID_SECRET_BYTES)
        rendered = repr(settings)
        assert VALID_SECRET_TEXT not in rendered
        assert "redacted" in rendered


class TestPrincipalValidation:
    def test_none_rejected(self):
        with pytest.raises(RequestOwnerIdentityMissingError) as exc:
            _provider().derive_from_header_value(None)
        assert "missing" in str(exc.value).lower()

    def test_missing_header_rejected(self):
        with pytest.raises(RequestOwnerIdentityMissingError):
            _provider().derive_from_headers({})

    def test_empty_value_rejected(self):
        with pytest.raises(RequestOwnerIdentityInvalidError):
            _provider().derive_from_header_value("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(RequestOwnerIdentityInvalidError):
            _provider().derive_from_header_value("   \t  ")

    def test_surrounding_whitespace_stripped(self):
        identity = _provider().derive_from_header_value("  Alice@example.com  ")
        assert identity.audit_principal == "Alice@example.com"

    def test_unicode_nfc_normalization(self):
        decomposed = "Cafe\u0301"
        identity = _provider().derive_from_header_value(decomposed)
        assert identity.audit_principal == unicodedata.normalize("NFC", decomposed)

    def test_case_preserved(self):
        identity = _provider().derive_from_header_value("User.Name")
        assert identity.audit_principal == "User.Name"

    @pytest.mark.parametrize("value", ["alice\rsmith", "alice\nsmith", "alice\x00smith"])
    def test_cr_lf_nul_rejected(self, value):
        with pytest.raises(RequestOwnerIdentityInvalidError):
            _provider().derive_from_header_value(value)

    @pytest.mark.parametrize("value", ["alice\x01smith", "alice\x1fsmith", "alice\x7fsmith"])
    def test_other_control_characters_rejected(self, value):
        with pytest.raises(RequestOwnerIdentityInvalidError):
            _provider().derive_from_header_value(value)

    def test_comma_separated_values_rejected(self):
        with pytest.raises(RequestOwnerIdentityInvalidError):
            _provider().derive_from_header_value("user-a,user-b")

    def test_maximum_length_accepted(self):
        value = "a" * 512
        identity = _provider().derive_from_header_value(value)
        assert identity.audit_principal == value

    def test_excessive_length_rejected(self):
        with pytest.raises(RequestOwnerIdentityInvalidError):
            _provider().derive_from_header_value("a" * 513)

    def test_display_name_parsing_not_performed(self):
        value = "Alice Example <alice@example.com>"
        identity = _provider().derive_from_header_value(value)
        assert identity.audit_principal == value
        assert identity.owner_user_id_hash == _expected_digest(VALID_SECRET_TEXT, value)

    def test_angle_bracket_extraction_not_performed(self):
        value = "<opaque-principal>"
        identity = _provider().derive_from_header_value(value)
        assert identity.audit_principal == value
        assert identity.owner_user_id_hash == _expected_digest(VALID_SECRET_TEXT, value)

    def test_invalid_errors_hide_principal(self):
        principal = "bad,user@example.com"
        with pytest.raises(RequestOwnerIdentityInvalidError) as exc:
            _provider().derive_from_header_value(principal)
        assert principal not in str(exc.value)


class TestHeaderMapping:
    def test_exact_header_name_accepted(self):
        identity = _provider().derive_from_headers({HEADER_NAME: "alice@example.com"})
        assert identity.audit_principal == "alice@example.com"

    def test_lowercase_header_name_accepted(self):
        identity = _provider().derive_from_headers({"x-forwarded-user": "alice@example.com"})
        assert identity.audit_principal == "alice@example.com"

    def test_mixed_case_header_name_accepted(self):
        identity = _provider().derive_from_headers({"X-FoRwArDeD-UsEr": "alice@example.com"})
        assert identity.audit_principal == "alice@example.com"

    def test_unrelated_headers_ignored(self):
        headers = {"Cookie": "abc=1", HEADER_NAME: "alice@example.com", "X-Irrelevant": "1"}
        identity = _provider().derive_from_headers(headers)
        assert identity.audit_principal == "alice@example.com"

    def test_duplicate_differently_cased_header_keys_rejected(self):
        headers = {
            "X-Forwarded-User": "alice@example.com",
            "x-forwarded-user": "bob@example.com",
        }
        with pytest.raises(RequestOwnerIdentityInvalidError) as exc:
            _provider().derive_from_headers(headers)
        assert "alice@example.com" not in str(exc.value)
        assert "bob@example.com" not in str(exc.value)

    def test_authorization_header_not_used(self):
        headers = {"Authorization": "Bearer secret-token"}
        with pytest.raises(RequestOwnerIdentityMissingError):
            _provider().derive_from_headers(headers)

    def test_cookie_not_used(self):
        headers = {"Cookie": "user=alice@example.com"}
        with pytest.raises(RequestOwnerIdentityMissingError):
            _provider().derive_from_headers(headers)

    def test_query_like_fields_not_used(self):
        headers = {"conversation_id": "frontend-123", "owner": "alice@example.com"}
        with pytest.raises(RequestOwnerIdentityMissingError):
            _provider().derive_from_headers(headers)

    def test_email_body_like_fields_not_used(self):
        headers = {"user_email": "alice@example.com", "email": "alice@example.com"}
        with pytest.raises(RequestOwnerIdentityMissingError):
            _provider().derive_from_headers(headers)

    def test_x_forwarded_email_is_ignored(self):
        headers = {"X-Forwarded-Email": "alice@example.com"}
        with pytest.raises(RequestOwnerIdentityMissingError):
            _provider().derive_from_headers(headers)

    def test_x_user_email_is_ignored(self):
        headers = {"X-User-Email": "alice@example.com"}
        with pytest.raises(RequestOwnerIdentityMissingError):
            _provider().derive_from_headers(headers)

    def test_frontend_conversation_id_cannot_provide_ownership(self):
        headers = {"conversation_id": "frontend-1", "frontend_conversation_id": "frontend-1"}
        with pytest.raises(RequestOwnerIdentityMissingError):
            _provider().derive_from_headers(headers)

    def test_anonymous_is_never_generated(self):
        with pytest.raises(RequestOwnerIdentityMissingError) as exc:
            _provider().derive_from_headers({"X-Forwarded-Email": "legacy@example.com"})
        assert "anonymous" not in str(exc.value)


class TestHmacBehaviour:
    def test_output_is_64_lowercase_hex_characters(self):
        identity = _provider().derive_from_header_value("alice@example.com")
        assert len(identity.owner_user_id_hash) == 64
        assert identity.owner_user_id_hash == identity.owner_user_id_hash.lower()
        int(identity.owner_user_id_hash, 16)

    def test_sha256_hmac_used(self):
        principal = "alice@example.com"
        identity = _provider().derive_from_header_value(principal)
        assert identity.owner_user_id_hash == _expected_digest(VALID_SECRET_TEXT, principal)

    def test_deterministic_for_same_secret_and_principal(self):
        provider = _provider()
        a = provider.derive_from_header_value("alice@example.com")
        b = provider.derive_from_header_value("alice@example.com")
        assert a.owner_user_id_hash == b.owner_user_id_hash

    def test_different_principal_gives_different_hash(self):
        provider = _provider()
        a = provider.derive_from_header_value("alice@example.com")
        b = provider.derive_from_header_value("bob@example.com")
        assert a.owner_user_id_hash != b.owner_user_id_hash

    def test_different_secret_gives_different_hash(self):
        a = _provider(VALID_SECRET_TEXT).derive_from_header_value("alice@example.com")
        b = _provider("fedcba9876543210fedcba9876543210").derive_from_header_value(
            "alice@example.com"
        )
        assert a.owner_user_id_hash != b.owner_user_id_hash

    def test_surrounding_whitespace_canonicalization_remains_deterministic(self):
        provider = _provider()
        a = provider.derive_from_header_value("alice@example.com")
        b = provider.derive_from_header_value("  alice@example.com  ")
        assert a.owner_user_id_hash == b.owner_user_id_hash

    def test_unicode_equivalent_forms_produce_same_hash(self):
        provider = _provider()
        a = provider.derive_from_header_value("Caf\u00e9")
        b = provider.derive_from_header_value("Cafe\u0301")
        assert a.owner_user_id_hash == b.owner_user_id_hash

    def test_case_distinct_principals_produce_different_hashes(self):
        provider = _provider()
        a = provider.derive_from_header_value("Alice")
        b = provider.derive_from_header_value("alice")
        assert a.owner_user_id_hash != b.owner_user_id_hash

    def test_domain_separation_prefix_influences_digest(self):
        principal = "alice@example.com"
        identity = _provider().derive_from_header_value(principal)
        no_prefix = hmac.new(
            VALID_SECRET_BYTES,
            principal.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert identity.owner_user_id_hash != no_prefix

    def test_raw_principal_absent_from_hash(self):
        principal = "alice@example.com"
        identity = _provider().derive_from_header_value(principal)
        assert principal not in identity.owner_user_id_hash

    def test_raw_secret_absent_from_hash(self):
        identity = _provider().derive_from_header_value("alice@example.com")
        assert VALID_SECRET_TEXT not in identity.owner_user_id_hash

    def test_hash_is_not_plain_sha256_of_principal(self):
        principal = "alice@example.com"
        identity = _provider().derive_from_header_value(principal)
        plain = hashlib.sha256(principal.encode("utf-8")).hexdigest()
        assert identity.owner_user_id_hash != plain

    def test_no_random_salt(self):
        provider = _provider()
        results = {
            provider.derive_from_header_value("alice@example.com").owner_user_id_hash
            for _ in range(5)
        }
        assert len(results) == 1

    def test_repeated_provider_instances_give_same_result(self):
        a = _provider().derive_from_header_value("alice@example.com")
        b = _provider().derive_from_header_value("alice@example.com")
        assert a.owner_user_id_hash == b.owner_user_id_hash


class TestDomainObject:
    def test_owner_hash_populated(self):
        identity = _provider().derive_from_header_value("alice@example.com")
        assert identity.owner_user_id_hash

    def test_audit_principal_populated_with_canonical_value(self):
        identity = _provider().derive_from_header_value("  Alice@example.com  ")
        assert identity.audit_principal == "Alice@example.com"

    def test_source_is_x_forwarded_user(self):
        identity = _provider().derive_from_header_value("alice@example.com")
        assert identity.source == SOURCE_LABEL

    def test_frozen_identity_cannot_be_mutated(self):
        identity = _provider().derive_from_header_value("alice@example.com")
        with pytest.raises(FrozenInstanceError):
            identity.source = "changed"

    def test_identity_repr_hides_principal(self):
        identity = _provider().derive_from_header_value("alice@example.com")
        rendered = repr(identity)
        assert "alice@example.com" not in rendered
        assert "redacted" in rendered

    def test_identity_repr_hides_hash(self):
        identity = _provider().derive_from_header_value("alice@example.com")
        rendered = repr(identity)
        assert identity.owner_user_id_hash not in rendered

    def test_ownership_identifier_is_distinct_from_audit_principal(self):
        identity = _provider().derive_from_header_value("alice@example.com")
        assert identity.owner_user_id_hash != identity.audit_principal

    def test_safe_equality_behaviour(self):
        a = _provider().derive_from_header_value("alice@example.com")
        b = _provider().derive_from_header_value("alice@example.com")
        c = _provider().derive_from_header_value("bob@example.com")
        assert a == b
        assert a != c


class TestProviderAndSecurity:
    def test_provider_repr_hides_secret(self):
        rendered = repr(_provider())
        assert VALID_SECRET_TEXT not in rendered
        assert "redacted" in rendered

    def test_provider_construction_performs_no_network_access(self, monkeypatch):
        monkeypatch.setattr(socket, "create_connection", _SocketTrap())
        provider = _provider()
        assert isinstance(provider, RequestOwnerIdentityProvider)

    def test_derivation_performs_no_network_access(self, monkeypatch):
        monkeypatch.setattr(socket, "create_connection", _SocketTrap())
        identity = _provider().derive_from_header_value("alice@example.com")
        assert identity.source == SOURCE_LABEL

    def test_no_workspace_client(self):
        assert "WorkspaceClient" not in _module_source()

    def test_no_credential_generation(self):
        source = _module_source()
        assert "oauth" not in source.lower()
        assert "credential" not in source.lower()

    def test_no_pool(self):
        source = _module_source()
        assert "connectionpool" not in source.lower()
        assert "psycopg" not in source.lower()

    def test_no_sql(self):
        source_upper = _module_source().upper()
        assert "SELECT " not in source_upper
        assert "INSERT " not in source_upper
        assert "UPDATE " not in source_upper
        assert "DELETE " not in source_upper

    def test_no_logging_of_principal(self):
        source = _module_source()
        assert "logging" not in source
        assert "logger" not in source

    def test_no_logging_of_secret(self):
        source = _module_source()
        assert "print(" not in source
        assert "logger" not in source

    def test_no_global_provider_singleton(self):
        module = _fresh_import_under_alias("phase4a_request_owner_identity_singleton_test")
        assert not any(
            isinstance(value, module.RequestOwnerIdentityProvider)
            for value in vars(module).values()
        )


class TestIntegrationBoundaries:
    def test_chat_py_does_not_import_identity_module(self):
        # Phase 4B2: chat.py is permitted to import request_owner_identity_runtime
        # (the approved Phase 4B2 boundary wrapper) but must NOT import the core
        # request_owner_identity module directly.
        source = _asset_source("app/routes/chat.py")
        assert "from app.services.request_owner_identity import" not in source, (
            "chat.py must not import request_owner_identity directly; "
            "only request_owner_identity_runtime is permitted in Phase 4B2"
        )
        assert "RequestOwnerIdentityProvider" not in source, (
            "chat.py must not reference RequestOwnerIdentityProvider directly"
        )

    def test_main_py_does_not_import_identity_module(self):
        source = _asset_source("app/main.py")
        assert "request_owner_identity" not in source

    def test_genie_pipeline_py_does_not_import_identity_module(self):
        source = _asset_source("app/services/genie_pipeline.py")
        assert "request_owner_identity" not in source

    def test_durable_adapter_does_not_import_identity_module(self):
        source = _asset_source("app/services/durable_genie_session_adapter.py")
        assert "request_owner_identity" not in source

    def test_no_request_path_uses_derived_owner_identity(self):
        source = _asset_source("app/routes/chat.py")
        assert "owner_user_id_hash" not in source
        assert "RequestOwnerIdentityProvider" not in source

    def test_no_fallback_identity_exists_in_module(self):
        source = _module_source()
        assert "X-Forwarded-Email" not in source
        assert "X-User-Email" not in source
        assert "anonymous" not in source

    def test_app_yaml_unchanged(self):
        # Phase 4B1: CONVERSATION_OWNER_HMAC_SECRET is now declared in app.yaml
        # via valueFrom (not plaintext).  The pre-4B1 "not in source" assertion
        # is superseded; this test now validates the Phase 4B1 configuration.
        source = _asset_source("app.yaml")
        assert "CONVERSATION_OWNER_HMAC_SECRET" in source, (
            "Phase 4B1 requires CONVERSATION_OWNER_HMAC_SECRET in app.yaml"
        )
        assert "valueFrom: conversation-owner-hmac-secret" in source, (
            "CONVERSATION_OWNER_HMAC_SECRET must use valueFrom, not a plaintext value"
        )
        assert "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY" in source, (
            "Phase 4B1 requires ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY feature flag in app.yaml"
        )

    def test_requirements_txt_unchanged(self):
        source = _asset_source("requirements.txt")
        assert "request_owner_identity" not in source
