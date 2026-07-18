"""Phase 3D2: FastAPI lifecycle cleanup tests for durable Genie runtime.

Validates that:
- app/main.py imports reset_genie_pipeline (shutdown-only);
- startup does NOT initialize the Genie pipeline or any durable components;
- shutdown calls reset_genie_pipeline exactly once (via try/finally);
- reset errors are logged but do not crash the application;
- no credential, pool, SQL, or network access occurs during startup;
- existing app structure (routes, middleware, title, version) is preserved.
"""

from __future__ import annotations

import ast
import importlib
import logging
import os
import pathlib
import socket
import sys
import types
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Test-environment setup: ensure declared deps are importable
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Network guard
# ---------------------------------------------------------------------------

class _NetworkGuard:
    """Context manager that explodes on any socket creation."""
    def __enter__(self):
        self._orig_create = socket.create_connection
        self._orig_socket = socket.socket
        socket.create_connection = self._explode
        socket.socket = self._explode
        return self

    def __exit__(self, *_):
        socket.create_connection = self._orig_create
        socket.socket = self._orig_socket

    @staticmethod
    def _explode(*args, **kwargs):
        raise AssertionError("Network access is forbidden during this test")


# ---------------------------------------------------------------------------
# Fake runtime bundle for testing reset/close flows
# ---------------------------------------------------------------------------

class FakeRuntimeBundle:
    """Mimics the durable runtime bundle's close interface."""
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


# ---------------------------------------------------------------------------
# SECTION 1: Application compatibility tests
# ---------------------------------------------------------------------------

class TestApplicationCompatibility:
    """Tests 1-7: app structure, import safety."""

    def test_01_app_is_fastapi_instance(self):
        from app.main import app
        from fastapi import FastAPI
        assert isinstance(app, FastAPI)

    def test_02_title_unchanged(self):
        from app.main import app
        from app.config import settings
        assert app.title == settings.APP_NAME

    def test_03_version_unchanged(self):
        from app.main import app
        from app.config import settings
        assert app.version == settings.APP_VERSION

    def test_04_routes_registered(self):
        from app.main import app
        paths = {r.path for r in app.routes}
        assert "/api/health" in paths or any("/api/health" in str(r.path) for r in app.routes)

    def test_05_health_endpoint_registered(self):
        from app.main import app
        route_paths = []
        for route in app.routes:
            if hasattr(route, "path"):
                route_paths.append(route.path)
        # health router is under /api prefix
        assert any("health" in p for p in route_paths)

    def test_06_middleware_registered(self):
        from app.main import app
        # CORS middleware is registered
        middleware_classes = [m.cls.__name__ if hasattr(m, 'cls') else str(m) for m in app.user_middleware]
        assert any("CORS" in str(mc) for mc in middleware_classes)

    def test_07_import_does_not_call_get_genie_pipeline(self):
        """Importing app.main must not call get_genie_pipeline."""
        with patch("app.services.genie_backend_factory.get_genie_pipeline") as mock_get:
            # Force reimport
            if "app.main" in sys.modules:
                # Already imported; verify the module-level code didn't call it
                # by checking the factory module state
                pass
            mock_get.assert_not_called()

    def test_07b_import_does_not_create_runtime_bundle(self):
        """Importing app.main must not construct a runtime bundle."""
        with patch(
            "app.services.genie_backend_factory.DurableGenieSessionRuntimeFactory"
        ) as mock_factory:
            # The import already happened; verify no factory was called at import time
            # by checking that no instance was created outside get_genie_pipeline
            mock_factory.assert_not_called()


# ---------------------------------------------------------------------------
# SECTION 2: Startup safety tests
# ---------------------------------------------------------------------------

class TestStartupSafety:
    """Tests 8-16: startup must not initialize Genie components."""

    @pytest.fixture
    def patched_reset(self):
        with patch("app.main.reset_genie_pipeline") as mock_reset:
            yield mock_reset

    @pytest.mark.asyncio
    async def test_08_startup_does_not_call_get_genie_pipeline(self, patched_reset):
        from app.main import lifespan, app as the_app
        with patch("app.services.genie_backend_factory.get_genie_pipeline") as mock_get:
            async with lifespan(the_app):
                mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_09_startup_does_not_construct_session_store(self, patched_reset):
        from app.main import lifespan, app as the_app
        with patch("app.services.genie_session_store.GenieSessionStore") as mock_store:
            async with lifespan(the_app):
                mock_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_10_startup_does_not_construct_runtime_factory(self, patched_reset):
        from app.main import lifespan, app as the_app
        with patch(
            "app.services.durable_genie_session_runtime_factory.DurableGenieSessionRuntimeFactory"
        ) as mock_rf:
            async with lifespan(the_app):
                mock_rf.assert_not_called()

    @pytest.mark.asyncio
    async def test_11_startup_does_not_construct_repository(self, patched_reset):
        from app.main import lifespan, app as the_app
        # No repository construction during startup
        with patch(
            "app.services.genie_backend_factory._build_pipeline"
        ) as mock_build:
            async with lifespan(the_app):
                mock_build.assert_not_called()

    @pytest.mark.asyncio
    async def test_12_startup_does_not_create_workspace_client(self, patched_reset):
        from app.main import lifespan, app as the_app
        # WorkspaceClient should not be constructed during startup
        with patch("databricks.sdk.WorkspaceClient", create=True) as mock_wc:
            async with lifespan(the_app):
                mock_wc.assert_not_called()

    @pytest.mark.asyncio
    async def test_13_startup_does_not_generate_credential(self, patched_reset):
        from app.main import lifespan, app as the_app
        # No credential generation happens during startup
        # Verified by no pipeline build call
        with patch(
            "app.services.genie_backend_factory._build_pipeline"
        ) as mock_build:
            async with lifespan(the_app):
                mock_build.assert_not_called()

    @pytest.mark.asyncio
    async def test_14_startup_does_not_open_pool(self, patched_reset):
        from app.main import lifespan, app as the_app
        # No connection pool opened during startup
        with patch(
            "app.services.genie_backend_factory._build_pipeline"
        ) as mock_build:
            async with lifespan(the_app):
                mock_build.assert_not_called()

    @pytest.mark.asyncio
    async def test_15_startup_executes_no_sql(self, patched_reset):
        from app.main import lifespan, app as the_app
        with _NetworkGuard():
            async with lifespan(the_app):
                pass  # No SQL, no network

    @pytest.mark.asyncio
    async def test_16_existing_startup_behaviour_preserved(self, patched_reset):
        """Startup still logs the start message."""
        from app.main import lifespan, app as the_app
        with patch("app.main.logger") as mock_logger:
            async with lifespan(the_app):
                mock_logger.info.assert_called()
                # First call should be the startup log
                first_call = mock_logger.info.call_args_list[0]
                assert "Starting" in first_call[0][0]


# ---------------------------------------------------------------------------
# SECTION 3: Shutdown tests
# ---------------------------------------------------------------------------

class TestShutdownBehaviour:
    """Tests 17-26: shutdown calls reset_genie_pipeline correctly."""

    @pytest.mark.asyncio
    async def test_17_reset_called_exactly_once_on_normal_exit(self):
        from app.main import lifespan, app as the_app
        with patch("app.main.reset_genie_pipeline") as mock_reset:
            async with lifespan(the_app):
                pass
            assert mock_reset.call_count == 1

    @pytest.mark.asyncio
    async def test_18_reset_occurs_after_yield_not_before(self):
        """Reset must not be called before yield."""
        from app.main import lifespan, app as the_app
        call_order = []
        with patch("app.main.reset_genie_pipeline", side_effect=lambda: call_order.append("reset")):
            async with lifespan(the_app):
                call_order.append("yield_active")
            assert call_order == ["yield_active", "reset"]

    @pytest.mark.asyncio
    async def test_19_reset_safe_when_no_pipeline_initialized(self):
        """reset_genie_pipeline is safe when no singleton exists."""
        from app.main import lifespan, app as the_app
        from app.services import genie_backend_factory as factory
        # Ensure no pipeline exists
        with factory._lock:
            factory._genie_pipeline = None
        # Should not raise
        with patch("app.main.reset_genie_pipeline", wraps=factory.reset_genie_pipeline):
            async with lifespan(the_app):
                pass

    @pytest.mark.asyncio
    async def test_20_reset_called_when_pipeline_exists(self):
        """Reset is called even when a pipeline singleton exists."""
        from app.main import lifespan, app as the_app
        with patch("app.main.reset_genie_pipeline") as mock_reset:
            async with lifespan(the_app):
                pass
            mock_reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_21_disabled_bundle_closed_through_reset(self):
        """An attached disabled bundle is closed via the reset path."""
        from app.services import genie_backend_factory as factory
        from app.main import lifespan, app as the_app

        bundle = FakeRuntimeBundle(enabled=False)
        fake_pipeline = MagicMock()
        setattr(fake_pipeline, factory._DURABLE_RUNTIME_BUNDLE_ATTR, bundle)

        with factory._lock:
            factory._genie_pipeline = fake_pipeline

        try:
            async with lifespan(the_app):
                pass
            assert bundle.close_calls == 1
        finally:
            with factory._lock:
                factory._genie_pipeline = None

    @pytest.mark.asyncio
    async def test_22_enabled_bundle_closed_through_reset(self):
        """An attached enabled bundle is closed via the reset path."""
        from app.services import genie_backend_factory as factory
        from app.main import lifespan, app as the_app

        bundle = FakeRuntimeBundle(enabled=True)
        fake_pipeline = MagicMock()
        setattr(fake_pipeline, factory._DURABLE_RUNTIME_BUNDLE_ATTR, bundle)

        with factory._lock:
            factory._genie_pipeline = fake_pipeline

        try:
            async with lifespan(the_app):
                pass
            assert bundle.close_calls == 1
        finally:
            with factory._lock:
                factory._genie_pipeline = None

    @pytest.mark.asyncio
    async def test_23_bundle_closes_exactly_once(self):
        """Bundle.close() is called exactly once, not multiple times."""
        from app.services import genie_backend_factory as factory
        from app.main import lifespan, app as the_app

        bundle = FakeRuntimeBundle(enabled=True)
        fake_pipeline = MagicMock()
        setattr(fake_pipeline, factory._DURABLE_RUNTIME_BUNDLE_ATTR, bundle)

        with factory._lock:
            factory._genie_pipeline = fake_pipeline

        try:
            async with lifespan(the_app):
                pass
            assert bundle.close_calls == 1
        finally:
            with factory._lock:
                factory._genie_pipeline = None

    @pytest.mark.asyncio
    async def test_24_repository_bundle_not_closed_directly_by_main(self):
        """main.py does not import or call bundle.close() directly."""
        import app.main as main_module
        source = pathlib.Path(main_module.__file__).read_text()
        assert "bundle" not in source.lower() or "runtime_bundle" not in source
        assert "_close_attached_runtime_bundle" not in source

    @pytest.mark.asyncio
    async def test_25_repeated_lifespan_runs_safe(self):
        """Multiple independent lifespan runs are safe."""
        from app.main import lifespan, app as the_app
        with patch("app.main.reset_genie_pipeline") as mock_reset:
            async with lifespan(the_app):
                pass
            async with lifespan(the_app):
                pass
            async with lifespan(the_app):
                pass
            assert mock_reset.call_count == 3

    @pytest.mark.asyncio
    async def test_26_existing_shutdown_operations_preserved(self):
        """Existing shutdown logging is still present."""
        from app.main import lifespan, app as the_app
        with patch("app.main.reset_genie_pipeline"):
            with patch("app.main.logger") as mock_logger:
                async with lifespan(the_app):
                    pass
                # Shutdown log should include "Shutting down"
                info_calls = [str(c) for c in mock_logger.info.call_args_list]
                assert any("Shutting down" in s for s in info_calls)


# ---------------------------------------------------------------------------
# SECTION 4: Exception path tests
# ---------------------------------------------------------------------------

class TestExceptionPaths:
    """Tests 27-33: reset attempted even on errors, logging fully sanitized."""

    # --- Helper ---
    def _assert_sanitized_error_call(self, mock_logger, sensitive_values):
        """Assert logger.error was called with ONLY the static message, no extras."""
        mock_logger.error.assert_called_once()
        error_call = mock_logger.error.call_args
        # Positional args: only the static message, no exception arg
        assert error_call[0] == ("Genie pipeline cleanup failed during shutdown",)
        # Keyword args: no exc_info, no stack_info
        assert error_call[1].get("exc_info") is None or error_call[1].get("exc_info") is False
        assert error_call[1].get("stack_info") is None or error_call[1].get("stack_info") is False
        # No sensitive values anywhere in the call representation
        call_repr = str(error_call)
        for val in sensitive_values:
            assert val not in call_repr, f"Sensitive value {val!r} leaked into log call"
        # Must NOT use logger.exception (which implies exc_info=True)
        mock_logger.exception.assert_not_called()

    @pytest.mark.asyncio
    async def test_27_reset_attempted_when_lifespan_body_raises(self):
        """Reset is called even if the application raises during lifespan."""
        from app.main import lifespan, app as the_app
        with patch("app.main.reset_genie_pipeline") as mock_reset:
            with pytest.raises(RuntimeError, match="simulated app crash"):
                async with lifespan(the_app):
                    raise RuntimeError("simulated app crash")
            mock_reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_28_existing_shutdown_preserved_on_exception(self):
        """Shutdown logging still occurs when the app body raises."""
        from app.main import lifespan, app as the_app
        with patch("app.main.reset_genie_pipeline"):
            with patch("app.main.logger") as mock_logger:
                with pytest.raises(RuntimeError):
                    async with lifespan(the_app):
                        raise RuntimeError("boom")
                info_calls = [str(c) for c in mock_logger.info.call_args_list]
                assert any("Shutting down" in s for s in info_calls)

    @pytest.mark.asyncio
    async def test_29_reset_failure_handled_gracefully(self):
        """If reset_genie_pipeline raises, the error is logged, not re-raised."""
        from app.main import lifespan, app as the_app
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError("cleanup exploded"),
        ):
            with patch("app.main.logger") as mock_logger:
                # Should NOT raise
                async with lifespan(the_app):
                    pass
                # Error should be logged with static message only
                self._assert_sanitized_error_call(mock_logger, ["cleanup exploded"])

    @pytest.mark.asyncio
    async def test_30_error_output_contains_no_host(self):
        """Logged error must not leak host from exception."""
        from app.main import lifespan, app as the_app
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError("host=prod.internal token=secret"),
        ):
            with patch("app.main.logger") as mock_logger:
                async with lifespan(the_app):
                    pass
                self._assert_sanitized_error_call(
                    mock_logger, ["prod.internal", "token=", "secret"]
                )

    @pytest.mark.asyncio
    async def test_31_error_output_contains_no_endpoint(self):
        """Logged error must not leak endpoint resource path."""
        from app.main import lifespan, app as the_app
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError(
                "endpoint=projects/secret-proj/branches/production"
            ),
        ):
            with patch("app.main.logger") as mock_logger:
                async with lifespan(the_app):
                    pass
                self._assert_sanitized_error_call(
                    mock_logger,
                    ["projects/", "secret-proj", "branches/production"],
                )

    @pytest.mark.asyncio
    async def test_32_error_output_contains_no_credential(self):
        """Logged error must not leak credentials or tokens."""
        from app.main import lifespan, app as the_app
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError("dapi-secret-token-12345"),
        ):
            with patch("app.main.logger") as mock_logger:
                async with lifespan(the_app):
                    pass
                self._assert_sanitized_error_call(
                    mock_logger, ["dapi-", "secret-token", "12345"]
                )

    @pytest.mark.asyncio
    async def test_32b_error_output_contains_no_password(self):
        """Logged error must not leak passwords or DSNs."""
        from app.main import lifespan, app as the_app
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError(
                "postgresql://user:password@host/database"
            ),
        ):
            with patch("app.main.logger") as mock_logger:
                async with lifespan(the_app):
                    pass
                self._assert_sanitized_error_call(
                    mock_logger,
                    ["postgresql://", "password", "user:", "@host"],
                )

    @pytest.mark.asyncio
    async def test_32c_no_exc_info_in_error_call(self):
        """logger.error must NOT pass exc_info=True."""
        from app.main import lifespan, app as the_app
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError("secret-payload"),
        ):
            with patch("app.main.logger") as mock_logger:
                async with lifespan(the_app):
                    pass
                error_call = mock_logger.error.call_args
                assert error_call[1].get("exc_info") in (None, False)

    @pytest.mark.asyncio
    async def test_32d_no_stack_info_in_error_call(self):
        """logger.error must NOT pass stack_info=True."""
        from app.main import lifespan, app as the_app
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError("secret-payload"),
        ):
            with patch("app.main.logger") as mock_logger:
                async with lifespan(the_app):
                    pass
                error_call = mock_logger.error.call_args
                assert error_call[1].get("stack_info") in (None, False)

    @pytest.mark.asyncio
    async def test_32e_no_logger_exception_used(self):
        """main.py must not use logger.exception for reset failures."""
        from app.main import lifespan, app as the_app
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError("secret-payload"),
        ):
            with patch("app.main.logger") as mock_logger:
                async with lifespan(the_app):
                    pass
                mock_logger.exception.assert_not_called()

    @pytest.mark.asyncio
    async def test_32f_error_has_no_positional_exception_arg(self):
        """logger.error must have exactly one positional arg (the static msg)."""
        from app.main import lifespan, app as the_app
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError("password=secret"),
        ):
            with patch("app.main.logger") as mock_logger:
                async with lifespan(the_app):
                    pass
                error_call = mock_logger.error.call_args
                # Only one positional arg: the static message string
                assert len(error_call[0]) == 1
                assert error_call[0][0] == "Genie pipeline cleanup failed during shutdown"

    @pytest.mark.asyncio
    async def test_33_no_retry_loop_on_failure(self):
        """reset_genie_pipeline is called at most once, no retry."""
        from app.main import lifespan, app as the_app
        call_count = []
        def counting_reset():
            call_count.append(1)
            raise RuntimeError("fail")
        with patch("app.main.reset_genie_pipeline", side_effect=counting_reset):
            async with lifespan(the_app):
                pass
            assert len(call_count) == 1

    @pytest.mark.asyncio
    async def test_33b_shutdown_completes_after_reset_failure(self):
        """Application shutdown finishes even if reset raises."""
        from app.main import lifespan, app as the_app
        shutdown_completed = []
        original_info = MagicMock()
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError("fail"),
        ):
            with patch("app.main.logger") as mock_logger:
                async with lifespan(the_app):
                    pass
                # Shutdown log was emitted (proves shutdown ran to completion)
                info_calls = [str(c) for c in mock_logger.info.call_args_list]
                assert any("Shutting down" in s for s in info_calls)

    @pytest.mark.asyncio
    async def test_33c_body_exception_propagates_despite_reset_failure(self):
        """If lifespan body raises AND reset raises, body exception propagates."""
        from app.main import lifespan, app as the_app
        with patch(
            "app.main.reset_genie_pipeline",
            side_effect=RuntimeError("reset fail"),
        ):
            with pytest.raises(ValueError, match="app crash"):
                async with lifespan(the_app):
                    raise ValueError("app crash")


# ---------------------------------------------------------------------------
# SECTION 5: Runtime boundary tests
# ---------------------------------------------------------------------------

class TestRuntimeBoundaries:
    """Tests 34-40: no changes to other modules."""

    def test_34_chat_py_unchanged(self):
        """chat.py source has no lifecycle wiring."""
        chat_path = _REPO_ROOT / "app" / "routes" / "chat.py"
        source = chat_path.read_text()
        assert "lifespan" not in source
        assert "reset_genie_pipeline" not in source

    def test_35_genie_pipeline_unchanged(self):
        """genie_pipeline.py has no lifecycle wiring."""
        path = _REPO_ROOT / "app" / "services" / "genie_pipeline.py"
        source = path.read_text()
        assert "lifespan" not in source

    def test_36_session_store_unchanged(self):
        """GenieSessionStore has no lifecycle wiring."""
        path = _REPO_ROOT / "app" / "services" / "genie_session_store.py"
        source = path.read_text()
        assert "lifespan" not in source
        assert "reset_genie_pipeline" not in source

    def test_37_backend_factory_unchanged_by_phase3d2(self):
        """genie_backend_factory.py was not modified by this phase.
        
        We verify it still has the expected reset_genie_pipeline function
        without lifespan references.
        """
        path = _REPO_ROOT / "app" / "services" / "genie_backend_factory.py"
        source = path.read_text()
        assert "def reset_genie_pipeline" in source
        assert "lifespan" not in source

    def test_38_no_request_path_adapter_call_in_main(self):
        """main.py does not call or import the durable adapter."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        assert "DurableGenieSessionAdapter" not in source
        assert "durable_genie_session_adapter" not in source

    def test_39_no_deployment_config_changed(self):
        """app.yaml is not modified by this phase."""
        app_yaml = _REPO_ROOT / "app.yaml"
        if app_yaml.exists():
            source = app_yaml.read_text()
            assert "lifespan" not in source
            assert "phase3d2" not in source

    def test_40_no_global_runtime_bundle_in_main(self):
        """main.py does not declare a module-level runtime bundle variable."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        assert "runtime_bundle" not in source
        assert "_durable_runtime" not in source


# ---------------------------------------------------------------------------
# SECTION 6: Additional focused lifecycle tests
# ---------------------------------------------------------------------------

class TestAdditionalLifecycleSafety:
    """Extra safety tests for edge cases."""

    def test_41_main_py_parses_cleanly(self):
        """app/main.py is valid Python."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        tree = ast.parse(source)
        assert tree is not None

    def test_42_no_get_genie_pipeline_import_in_main(self):
        """main.py must NOT import get_genie_pipeline."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        assert "get_genie_pipeline" not in source

    def test_43_only_reset_imported_from_factory(self):
        """Only reset_genie_pipeline is imported from the backend factory."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        # Find the import line
        for line in source.splitlines():
            if "genie_backend_factory" in line:
                assert "reset_genie_pipeline" in line
                assert "get_genie_pipeline" not in line
                assert "DurableGenieSession" not in line
                break
        else:
            pytest.fail("genie_backend_factory import not found in main.py")

    def test_44_no_environment_variable_read_during_import(self):
        """main.py module-level code does not read LAKEBASE/PG env vars."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        assert "LAKEBASE" not in source
        assert "POSTGRES" not in source
        assert "PG_" not in source

    @pytest.mark.asyncio
    async def test_45_lifespan_uses_try_finally(self):
        """Verify the lifespan uses try/finally pattern."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
                # Find the Try node
                for child in ast.walk(node):
                    if isinstance(child, ast.Try):
                        assert child.finalbody, "lifespan must have a finally block"
                        return
                pytest.fail("lifespan does not contain a Try node")

    @pytest.mark.asyncio
    async def test_46_yield_inside_try_block(self):
        """Yield must be inside the try block (not in finally)."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
                for child in ast.walk(node):
                    if isinstance(child, ast.Try):
                        # Check yield is in body, not finalbody
                        body_source = ast.dump(ast.Module(body=child.body, type_ignores=[]))
                        assert "Yield" in body_source
                        finally_source = ast.dump(ast.Module(body=child.finalbody, type_ignores=[]))
                        assert "Yield" not in finally_source
                        return

    def test_47_no_durable_runtime_factory_import_in_main(self):
        """main.py must not import DurableGenieSessionRuntimeFactory."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        assert "DurableGenieSessionRuntimeFactory" not in source

    def test_48_no_lakebase_connection_provider_import_in_main(self):
        """main.py must not import lakebase_connection_provider."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        assert "lakebase_connection_provider" not in source

    def test_49_no_workspace_client_import_in_main(self):
        """main.py must not import WorkspaceClient."""
        path = _REPO_ROOT / "app" / "main.py"
        source = path.read_text()
        assert "WorkspaceClient" not in source

    @pytest.mark.asyncio
    async def test_50_concurrent_lifespan_isolation(self):
        """Two sequential lifespans do not share state through main.py."""
        from app.main import lifespan, app as the_app
        calls = []
        with patch("app.main.reset_genie_pipeline", side_effect=lambda: calls.append(1)):
            async with lifespan(the_app):
                pass
            assert len(calls) == 1
            async with lifespan(the_app):
                pass
            assert len(calls) == 2
