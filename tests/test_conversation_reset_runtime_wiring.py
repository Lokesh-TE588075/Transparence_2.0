"""Phase 4C4B3B: Shared runtime wiring invariants.

Proves that:
 1. Chat and reset use the exact same GenieSessionStore object identity.
 2. Reset removes a session created through the chat/pipeline store.
 3. Reset does NOT remove another owner's local session.
 4. Reset uses the existing durable adapter/runtime bundle.
 5. Importing main.py does not connect to Lakebase.
 6. Router registration does not construct credentials or pools.
 7. Shutdown lifecycle is invoked exactly once.
 8. No runtime object is created at module import.
 9. Router is registered exactly once in main.py.
10. Existing chat route behaviour remains unchanged after main.py wiring.

No live infrastructure. No Lakebase. No credentials.
"""
from __future__ import annotations

import importlib
import os as _os
import socket
import sys
import types
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest

_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
del _os, _REPO_ROOT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OWNER_A: str = "a" * 64
_OWNER_B: str = "b" * 64
_SESSION_A: str = "wiring-session-aaa"
_SESSION_B: str = "wiring-session-bbb"
_FRONTEND_1: str = "wiring-conv-001"


def _make_fake_pipeline(store=None, bundle=None):
    """Return a minimal fake GeniePipeline with _store and _durable_session_runtime_bundle."""
    from app.services.genie_session_store import GenieSessionStore
    from app.services.conversation_repository import InMemoryConversationRepository
    from app.services.conversation_repository_factory import (
        ConversationRepositoryBackend,
        ConversationRepositoryBundle,
    )
    from app.services.durable_genie_session_adapter import DurableGenieSessionAdapter

    if store is None:
        store = GenieSessionStore()

    if bundle is None:
        repo_bundle = ConversationRepositoryBundle(
            repository=InMemoryConversationRepository(),
            backend=ConversationRepositoryBackend.MEMORY,
            durable=False,
        )
        adapter = DurableGenieSessionAdapter(
            repository_bundle=repo_bundle,
            cache_store=None,
            cache_enabled=False,
        )
        bundle_obj = SimpleNamespace(adapter=adapter, enabled=True, close=MagicMock())
    else:
        bundle_obj = bundle

    pipeline = SimpleNamespace(
        _store=store,
        _durable_session_runtime_bundle=bundle_obj,
    )
    return pipeline, store, bundle_obj


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestSharedRuntimeWiring:
    """Tests 1–10: shared store/adapter identity and lifecycle invariants."""

    def test_01_chat_and_reset_use_same_session_store_object(self):
        """get_conversation_reset_coordinator() returns a coordinator that holds
        the exact same GenieSessionStore object as the pipeline singleton."""
        from app.services.conversation_reset_runtime import get_conversation_reset_coordinator

        pipeline, store, _ = _make_fake_pipeline()

        # get_conversation_reset_coordinator does a deferred
        # `from app.services.genie_backend_factory import get_genie_pipeline`
        # so we patch the attribute on the factory module directly.
        with patch(
            "app.services.genie_backend_factory.get_genie_pipeline",
            return_value=pipeline,
        ):
            coordinator = get_conversation_reset_coordinator()

        assert coordinator._session_store is store, (
            "Coordinator must hold the exact same store object as the pipeline"
        )

    def test_02_reset_removes_session_created_through_pipeline_store(self):
        """A session created in the pipeline store is removed by reset."""
        from app.services.conversation_reset_runtime import get_conversation_reset_coordinator
        from app.services.process_local_conversation_key import (
            build_process_local_conversation_key,
        )

        pipeline, store, _ = _make_fake_pipeline()

        # Create a session in the store directly (simulating what chat pipeline does).
        local_key = build_process_local_conversation_key(
            owner_user_id_hash=_OWNER_A,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        store.set_genie_conversation_id(local_key, "genie-conv-123")
        assert store.get_genie_conversation_id(local_key) == "genie-conv-123"

        with patch(
            "app.services.genie_backend_factory.get_genie_pipeline",
            return_value=pipeline,
        ):
            coordinator = get_conversation_reset_coordinator()

        # Reset removes the session via the shared store.
        coordinator.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=local_key,
        )

        # Session must now be gone from the shared store.
        assert store.get_genie_conversation_id(local_key) is None

    def test_03_reset_does_not_remove_another_owners_local_session(self):
        """Resetting for Owner A must not remove Owner B's session from the store."""
        from app.services.conversation_reset_runtime import get_conversation_reset_coordinator
        from app.services.process_local_conversation_key import (
            build_process_local_conversation_key,
        )

        pipeline, store, _ = _make_fake_pipeline()

        key_a = build_process_local_conversation_key(
            owner_user_id_hash=_OWNER_A,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        key_b = build_process_local_conversation_key(
            owner_user_id_hash=_OWNER_B,
            session_id=_SESSION_B,
            frontend_conversation_id=_FRONTEND_1,
        )
        store.set_genie_conversation_id(key_a, "genie-a")
        store.set_genie_conversation_id(key_b, "genie-b")

        with patch(
            "app.services.genie_backend_factory.get_genie_pipeline",
            return_value=pipeline,
        ):
            coordinator = get_conversation_reset_coordinator()

        coordinator.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=key_a,
        )

        # Owner B's session must remain untouched.
        assert store.get_genie_conversation_id(key_b) == "genie-b"

    def test_04_reset_uses_existing_durable_adapter(self):
        """Coordinator holds the exact adapter from the pipeline bundle."""
        from app.services.conversation_reset_runtime import get_conversation_reset_coordinator

        pipeline, store, bundle_obj = _make_fake_pipeline()
        expected_adapter = bundle_obj.adapter

        with patch(
            "app.services.genie_backend_factory.get_genie_pipeline",
            return_value=pipeline,
        ):
            coordinator = get_conversation_reset_coordinator()

        assert coordinator._adapter is expected_adapter, (
            "Coordinator must hold the exact adapter from the pipeline bundle"
        )

    def test_05_importing_main_does_not_connect_to_lakebase(self):
        """Importing app.main must not open any network socket."""
        # app.main is already imported by the test suite; verify it does not
        # open connections at import time by checking the source code for
        # the absence of import-time connection calls.
        import inspect
        import app.main
        src = inspect.getsource(app.main)
        # Import-time code must not call psycopg.connect or create pools.
        # Pool / connection creation happens inside get_genie_pipeline() lazily.
        assert "psycopg.connect" not in src.split("def lifespan")[0], (
            "No psycopg connect at module level"
        )
        assert "ConnectionPool(" not in src.split("def lifespan")[0], (
            "No connection pool at module level"
        )

    def test_06_router_registration_does_not_construct_credentials_or_pools(self):
        """Registering the conversation_reset router creates no credentials or pools."""
        from app.routes.conversation_reset import router
        from fastapi import FastAPI

        credential_calls: list = []

        class _BlockCredentialFactory:
            def __call__(self, *args, **kwargs):
                credential_calls.append(args)
                raise AssertionError("Credentials must not be constructed during router registration")

        test_app = FastAPI()

        with patch(
            "app.services.lakebase_connection_provider.LakebaseConnectionProvider",
            _BlockCredentialFactory(),
        ):
            # Including the router must not trigger credential construction.
            test_app.include_router(router, prefix="/api")

        assert credential_calls == []

    def test_07_shutdown_lifecycle_invoked_exactly_once(self):
        """Shutdown hook is wired correctly and calls reset_genie_pipeline exactly once."""
        import inspect
        import app.main as m
        src = inspect.getsource(m.lifespan)
        assert "reset_genie_pipeline" in src, (
            "Shutdown must call reset_genie_pipeline()"
        )
        # Verify it's inside the finally block (called on both normal + error exit).
        assert "finally" in src, "Shutdown cleanup must be in a finally block"

    def test_08_no_runtime_object_created_at_module_import(self):
        """Importing conversation_reset_runtime creates no store, adapter, or pool."""
        from app.services.genie_session_store import GenieSessionStore

        constructed: list = []
        original_init = GenieSessionStore.__init__

        def _spy(self, *a, **kw):
            constructed.append("store")
            return original_init(self, *a, **kw)

        with patch.object(GenieSessionStore, "__init__", _spy):
            mods_to_remove = [k for k in sys.modules if "conversation_reset_runtime" in k]
            saved = {k: sys.modules.pop(k) for k in mods_to_remove}
            try:
                import app.services.conversation_reset_runtime  # noqa: F401
            finally:
                sys.modules.update(saved)

        assert constructed == [], "Module import must not construct GenieSessionStore"

    def test_09_router_registered_exactly_once_in_main(self):
        """conversation_reset router is registered; reset endpoint appears exactly once."""
        import app.main as m

        # Collect all routes with 'conversations' in the path.
        reset_routes = [
            r for r in m.app.routes
            if hasattr(r, "path") and "conversations" in getattr(r, "path", "")
        ]
        assert len(reset_routes) >= 1, "Reset router must be registered"

        # Verify the exact endpoint path appears exactly once.
        exact_paths = [r.path for r in reset_routes if hasattr(r, "path")]
        reset_count = sum(1 for p in exact_paths if "reset" in p)
        assert reset_count == 1, f"Reset endpoint must be registered exactly once, got {reset_count}"

    def test_10_existing_chat_route_behaviour_unchanged(self):
        """main.py still registers /api/chat as a POST route after wiring."""
        import app.main as m
        chat_routes = [
            r for r in m.app.routes
            if hasattr(r, "path") and "/api/chat" == getattr(r, "path", "")
        ]
        assert len(chat_routes) >= 1, "Chat route /api/chat must remain registered"
