"""Tests for GET /api/conversations/{id}/messages endpoint.

H1 -- TransparencE Conversation History Persistence
"""
import asyncio
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeTrustedIdentity:
    def __init__(self, owner_hash):
        self.owner_user_id_hash = owner_hash


OWNER_A_HASH = "owner_hash_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OWNER_B_HASH = "owner_hash_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CONV_1 = "conv-id-0001"


def _make_fake_record(seq=1, role="user", text="hello", owner=OWNER_A_HASH, conv=CONV_1):
    from datetime import datetime, timezone
    import uuid
    from app.services.message_repository import MessageRecord
    now = datetime.now(timezone.utc)
    return MessageRecord(
        message_id=str(uuid.uuid4()),
        owner_user_id_hash=owner,
        frontend_conversation_id=conv,
        message_sequence=seq,
        role=role,
        message_text=text,
        response_payload_json=None,
        is_active=True,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# App fixture with mocked dependencies
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_app():
    """Create a minimal FastAPI app with the conversation_history router."""
    from fastapi import FastAPI
    from app.routes import conversation_history
    app = FastAPI()
    app.include_router(conversation_history.router, prefix="/api")
    return app


@pytest.fixture
def client(mock_app):
    return TestClient(mock_app, raise_server_exceptions=False)


def _patch_identity(identity):
    """Patch resolve_request_owner_identity."""
    return patch(
        "app.routes.conversation_history.resolve_request_owner_identity",
        return_value=identity,
    )


def _patch_repo(repo):
    """Patch get_message_repository."""
    return patch(
        "app.routes.conversation_history.get_message_repository",
        return_value=repo,
    )


# ---------------------------------------------------------------------------
# Identity checks
# ---------------------------------------------------------------------------

class TestIdentity:
    def test_returns_503_when_identity_configuration_error(self, client):
        from app.services.request_owner_identity_runtime import RequestOwnerIdentityRuntimeConfigurationError
        with patch(
            "app.routes.conversation_history.resolve_request_owner_identity",
            side_effect=RequestOwnerIdentityRuntimeConfigurationError("misconfigured"),
        ):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        assert resp.status_code == 503

    def test_returns_401_when_identity_resolution_error(self, client):
        from app.services.request_owner_identity_runtime import RequestOwnerIdentityRuntimeResolutionError
        with patch(
            "app.routes.conversation_history.resolve_request_owner_identity",
            side_effect=RequestOwnerIdentityRuntimeResolutionError("missing header"),
        ):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        assert resp.status_code == 401

    def test_returns_503_when_identity_is_none(self, client):
        with _patch_identity(None):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Feature disabled
# ---------------------------------------------------------------------------

class TestFeatureDisabled:
    def test_returns_503_when_repo_is_none(self, client):
        identity = _FakeTrustedIdentity(OWNER_A_HASH)
        with _patch_identity(identity), _patch_repo(None):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_returns_400_for_empty_conversation_id(self, client):
        identity = _FakeTrustedIdentity(OWNER_A_HASH)
        mock_repo = MagicMock()
        mock_repo.list_messages.return_value = ([], 0)
        with _patch_identity(identity), _patch_repo(mock_repo):
            # Path parameter is just whitespace
            resp = client.get("/api/conversations/ /messages")
        assert resp.status_code in (400, 422)  # 422 from FastAPI URL parsing, 400 from our guard


# ---------------------------------------------------------------------------
# Successful responses
# ---------------------------------------------------------------------------

class TestSuccessfulResponse:
    def test_returns_200_with_messages(self, client):
        identity = _FakeTrustedIdentity(OWNER_A_HASH)
        records = [_make_fake_record(seq=1, role="user", text="hello")]
        mock_repo = MagicMock()
        mock_repo.list_messages.return_value = (records, 1)
        with _patch_identity(identity), _patch_repo(mock_repo):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_messages"] == 1
        assert len(data["messages"]) == 1
        msg = data["messages"][0]
        assert msg["role"] == "user"
        assert msg["content"] == "hello"

    def test_response_contains_pagination_fields(self, client):
        identity = _FakeTrustedIdentity(OWNER_A_HASH)
        mock_repo = MagicMock()
        mock_repo.list_messages.return_value = ([], 0)
        with _patch_identity(identity), _patch_repo(mock_repo):
            resp = client.get(f"/api/conversations/{CONV_1}/messages?page=1&page_size=10")
        data = resp.json()
        assert "page" in data
        assert "page_size" in data
        assert "has_more" in data

    def test_returns_200_with_empty_messages_when_none_found(self, client):
        identity = _FakeTrustedIdentity(OWNER_A_HASH)
        mock_repo = MagicMock()
        mock_repo.list_messages.return_value = ([], 0)
        with _patch_identity(identity), _patch_repo(mock_repo):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"] == []


# ---------------------------------------------------------------------------
# Security: no internal identifiers in response
# ---------------------------------------------------------------------------

class TestNoInternalIdentifiers:
    def test_owner_hash_not_in_response(self, client):
        identity = _FakeTrustedIdentity(OWNER_A_HASH)
        records = [_make_fake_record()]
        mock_repo = MagicMock()
        mock_repo.list_messages.return_value = (records, 1)
        with _patch_identity(identity), _patch_repo(mock_repo):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        body = resp.text
        assert OWNER_A_HASH not in body

    def test_genie_fields_not_in_response(self, client):
        from datetime import datetime, timezone
        import uuid
        from app.services.message_repository import MessageRecord
        now = datetime.now(timezone.utc)
        forbidden_payload = '{"status": "success", "genie_conversation_id": "GC-SECRET", "generated_sql": "SELECT *"}'
        rec = MessageRecord(
            message_id=str(uuid.uuid4()),
            owner_user_id_hash=OWNER_A_HASH,
            frontend_conversation_id=CONV_1,
            message_sequence=1,
            role="assistant",
            message_text="hi",
            response_payload_json=forbidden_payload,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        identity = _FakeTrustedIdentity(OWNER_A_HASH)
        mock_repo = MagicMock()
        mock_repo.list_messages.return_value = ([rec], 1)
        with _patch_identity(identity), _patch_repo(mock_repo):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        body = resp.text
        # parse_response_payload does not strip forbidden fields from stored payload
        # but the route's _build_message_item only forwards safe HistoryMessageItem fields
        assert "genie_conversation_id" not in body
        assert "generated_sql" not in body


# ---------------------------------------------------------------------------
# Owner isolation
# ---------------------------------------------------------------------------

class TestOwnerIsolation:
    def test_owner_a_cannot_see_owner_b_messages(self, client):
        """Repo is queried with owner A's hash; owner B's messages not returned."""
        identity_a = _FakeTrustedIdentity(OWNER_A_HASH)
        mock_repo = MagicMock()
        # When list_messages is called with OWNER_A_HASH, return empty (B's messages not stored for A)
        mock_repo.list_messages.return_value = ([], 0)
        with _patch_identity(identity_a), _patch_repo(mock_repo):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        assert resp.status_code == 200
        # Verify list_messages was called with owner A's hash
        call_kwargs = mock_repo.list_messages.call_args.kwargs
        assert call_kwargs["owner_user_id_hash"] == OWNER_A_HASH


# ---------------------------------------------------------------------------
# Repository unavailable
# ---------------------------------------------------------------------------

class TestRepositoryUnavailable:
    def test_returns_503_on_repository_error(self, client):
        from app.services.message_repository import MessageRepositoryUnavailableError
        identity = _FakeTrustedIdentity(OWNER_A_HASH)
        mock_repo = MagicMock()
        mock_repo.list_messages.side_effect = MessageRepositoryUnavailableError("db down")
        with _patch_identity(identity), _patch_repo(mock_repo):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        assert resp.status_code == 503

    def test_returns_503_on_unexpected_exception(self, client):
        identity = _FakeTrustedIdentity(OWNER_A_HASH)
        mock_repo = MagicMock()
        mock_repo.list_messages.side_effect = RuntimeError("unexpected")
        with _patch_identity(identity), _patch_repo(mock_repo):
            resp = client.get(f"/api/conversations/{CONV_1}/messages")
        assert resp.status_code == 503
