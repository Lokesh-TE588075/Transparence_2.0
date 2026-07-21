"""Runtime singleton provider for the message history repository.

Mirrors the lazy double-checked locking pattern from genie_backend_factory.py.
The repository is constructed once per process and reused for every request.

Disabled (returns None) when:
  - ENABLE_MESSAGE_HISTORY is false (default)
  - Lakebase environment variables (PGHOST, etc.) are absent
  - LakebaseConnectionProvider construction fails

Callsite contract:
  result = get_message_repository()
  if result is None:
      # feature disabled or unavailable — skip history persistence / return
      return

H1 -- TransparencE Conversation History Persistence
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)

_lock: threading.Lock = threading.Lock()
_repo: Optional[object] = None   # LakebaseMessageRepository | None
_initialized: bool = False        # True after first build attempt (even if it returned None)
_SENTINEL = object()              # marks "init attempted but disabled/failed"


def get_message_repository() -> Optional[object]:
    """Return the process-level LakebaseMessageRepository, or None.

    None is returned when:
      - ENABLE_MESSAGE_HISTORY is false
      - Lakebase configuration is absent
      - Provider construction fails

    Never raises.  Callers must handle None gracefully.
    """
    global _repo, _initialized
    # Fast path — already initialized
    if _initialized:
        return _repo if _repo is not _SENTINEL else None

    with _lock:
        if _initialized:
            return _repo if _repo is not _SENTINEL else None
        try:
            _repo = _build_repository()
        except Exception as exc:
            log.warning("message_repository_runtime: build failed: %s", str(exc)[:200])
            _repo = _SENTINEL
        _initialized = True
        return _repo if _repo is not _SENTINEL else None


def reset_message_repository() -> None:
    """Discard the singleton for test teardown or config reload."""
    global _repo, _initialized
    with _lock:
        old = _repo
        _repo = None
        _initialized = False
    _safe_close(old)


def _build_repository() -> Optional[object]:
    """Attempt to construct a LakebaseMessageRepository.

    Returns None when disabled or when Lakebase is unconfigured.
    Raises on unexpected construction errors.
    """
    import os
    # Import settings lazily (same pattern as genie_backend_factory)
    from app.config import settings

    # Feature flag check
    enabled = getattr(settings, "ENABLE_MESSAGE_HISTORY", False)
    if not enabled:
        log.debug("message_repository_runtime: ENABLE_MESSAGE_HISTORY=false; skipping")
        return None

    # Check that minimum Lakebase env vars are present
    if not os.getenv("PGHOST", "").strip():
        log.warning(
            "message_repository_runtime: PGHOST not set; Lakebase message history unavailable"
        )
        return None

    # Construct the connection provider (same as durable session runtime)
    from app.services.lakebase_connection_provider import (
        LakebaseConnectionProvider,
        LakebaseConnectionSettings,
        LakebaseConnectionProviderError,
    )
    from app.services.lakebase_message_repository import LakebaseMessageRepository

    try:
        conn_settings = LakebaseConnectionSettings.from_environment()
        provider = LakebaseConnectionProvider(conn_settings)
    except LakebaseConnectionProviderError as exc:
        log.error(
            "message_repository_runtime: connection provider error: %s", str(exc)[:200]
        )
        return None

    repo = LakebaseMessageRepository(connection_provider=provider)
    log.info("message_repository_runtime: LakebaseMessageRepository initialized")
    return repo


def _safe_close(resource: Optional[object]) -> None:
    if resource is None or resource is _SENTINEL:
        return
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
