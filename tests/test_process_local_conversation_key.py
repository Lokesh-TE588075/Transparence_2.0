"""Tests for Phase 4C4B3A process-local conversation key helper.

Covers:
  1.  Valid inputs produce correct fixed format.
  2.  Same inputs produce identical output (determinism).
  3.  Different owner → different key.
  4.  Different session ID → different key.
  5.  Different frontend ID → different key.
  6.  Output does not contain raw owner hash.
  7.  Output does not contain raw session ID.
  8.  Output does not contain raw frontend ID.
  9.  Invalid owner hash (too short) is rejected.
  10. Uppercase owner hash is rejected.
  11. Empty session ID is rejected.
  12. Session ID with control characters is rejected.
  13. Excessively long session ID is rejected.
  14. Invalid frontend ID (email) is rejected.
  15. Exception repr exposes no raw inputs.
  16. No identifiers logged during construction.
  17. Thread-safe deterministic concurrent use.
  18. Large valid Unicode frontend ID follows the established validator contract.

Plus two exact digest vectors so that any accidental domain-separator
change is detected immediately.

No live infrastructure.  No HTTP.  No credentials.
"""

from __future__ import annotations

import logging
import os as _os
import sys as _sys
import threading

_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)
del _os, _REPO_ROOT

from app.services.process_local_conversation_key import (
    ProcessLocalConversationKeyError,
    build_process_local_conversation_key,
    is_valid_process_local_key,
    KEY_PREFIX,
    _DOMAIN_SEP,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_OWNER = "a" * 64          # 64 lowercase hex chars
_VALID_OWNER_B = "b" * 64        # different owner
_VALID_SESSION = "test-session-001"
_VALID_SESSION_B = "test-session-002"
_VALID_FRONTEND = "frontend-conv-001"
_VALID_FRONTEND_B = "frontend-conv-002"

# Pre-computed exact digest vectors.  These must not change unless the
# domain separator, prefix, or digest algorithm intentionally changes.
# Computed by: sha256(b"transparence-process-local-conversation:v1\x00"
#              + owner.encode("ascii") + b"\x00"
#              + session.encode("utf-8") + b"\x00"
#              + frontend.encode("utf-8")).hexdigest()
_EXPECTED_VECTOR_1 = (
    "plc_v1_8555bda75eb3fb6a048b82a7218843f01e00c444dca06fa2d9222b3e9c0b4566"
)  # (aaa..., test-session-001, frontend-conv-001)
_EXPECTED_VECTOR_2 = (
    "plc_v1_25d237104a3862a471728cff3bdf01fd634ed082672bf6576541f7b3c76d31e8"
)  # (bbb..., test-session-001, frontend-conv-001)


# ---------------------------------------------------------------------------
# Test 1 — Valid inputs, exact output format
# ---------------------------------------------------------------------------


class TestValidInputs:
    """Tests 1-2: Format correctness and determinism."""

    def test_01_valid_inputs_return_expected_format(self):
        """Output is 71-char string starting with plc_v1_."""
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert isinstance(result, str)
        assert result.startswith(KEY_PREFIX)
        assert len(result) == len(KEY_PREFIX) + 64  # 7 + 64 = 71
        assert is_valid_process_local_key(result)

    def test_01b_exact_vector_1(self):
        """Exact digest for (aaa..., test-session-001, frontend-conv-001)."""
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert result == _EXPECTED_VECTOR_1, (
            f"Domain separator or digest changed.  Got {result!r}, "
            f"expected {_EXPECTED_VECTOR_1!r}"
        )

    def test_01c_exact_vector_2(self):
        """Exact digest for (bbb..., test-session-001, frontend-conv-001)."""
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER_B,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert result == _EXPECTED_VECTOR_2, (
            f"Domain separator or digest changed.  Got {result!r}, "
            f"expected {_EXPECTED_VECTOR_2!r}"
        )

    def test_02_same_inputs_produce_identical_key(self):
        """Deterministic: same inputs always return the same key."""
        k1 = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        k2 = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert k1 == k2


# ---------------------------------------------------------------------------
# Tests 3-5 — Input-sensitivity
# ---------------------------------------------------------------------------


class TestInputSensitivity:
    """Tests 3-5: Each input independently affects the output."""

    def test_03_different_owner_produces_different_key(self):
        k_a = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        k_b = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER_B,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert k_a != k_b

    def test_04_different_session_produces_different_key(self):
        k_a = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        k_b = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION_B,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert k_a != k_b

    def test_05_different_frontend_produces_different_key(self):
        k_a = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        k_b = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND_B,
        )
        assert k_a != k_b


# ---------------------------------------------------------------------------
# Tests 6-8 — Raw inputs absent from output
# ---------------------------------------------------------------------------


class TestRawInputsAbsent:
    """Tests 6-8: None of the raw inputs appear in the opaque output."""

    def test_06_output_contains_no_owner_hash(self):
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert _VALID_OWNER not in result

    def test_07_output_contains_no_session_id(self):
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert _VALID_SESSION not in result

    def test_08_output_contains_no_frontend_id(self):
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert _VALID_FRONTEND not in result


# ---------------------------------------------------------------------------
# Tests 9-14 — Validation rejection
# ---------------------------------------------------------------------------


class TestValidationRejection:
    """Tests 9-14: Invalid inputs raise ProcessLocalConversationKeyError."""

    def test_09_invalid_owner_hash_too_short_rejected(self):
        try:
            build_process_local_conversation_key(
                owner_user_id_hash="abc123",
                session_id=_VALID_SESSION,
                frontend_conversation_id=_VALID_FRONTEND,
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_10_uppercase_owner_hash_rejected(self):
        try:
            build_process_local_conversation_key(
                owner_user_id_hash="A" * 64,
                session_id=_VALID_SESSION,
                frontend_conversation_id=_VALID_FRONTEND,
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_10b_mixed_case_owner_hash_rejected(self):
        try:
            build_process_local_conversation_key(
                owner_user_id_hash="a" * 63 + "Z",
                session_id=_VALID_SESSION,
                frontend_conversation_id=_VALID_FRONTEND,
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_11_empty_session_id_rejected(self):
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id="",
                frontend_conversation_id=_VALID_FRONTEND,
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_12_session_control_chars_rejected_cr(self):
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id="session\r123",
                frontend_conversation_id=_VALID_FRONTEND,
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_12b_session_control_chars_rejected_lf(self):
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id="session\n123",
                frontend_conversation_id=_VALID_FRONTEND,
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_12c_session_control_chars_rejected_nul(self):
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id="session\x00123",
                frontend_conversation_id=_VALID_FRONTEND,
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_12d_session_control_chars_rejected_del(self):
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id="session\x7f123",
                frontend_conversation_id=_VALID_FRONTEND,
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_13_excessive_session_id_rejected(self):
        # 513 bytes UTF-8 — one byte over the limit
        long_session = "x" * 513
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id=long_session,
                frontend_conversation_id=_VALID_FRONTEND,
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_13b_max_valid_session_id_accepted(self):
        # 512 bytes UTF-8 — exactly at the limit
        max_session = "x" * 512
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=max_session,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert is_valid_process_local_key(result)

    def test_14_invalid_frontend_id_email_rejected(self):
        """Frontend IDs that look like email addresses are rejected."""
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id=_VALID_SESSION,
                frontend_conversation_id="user@example.com",
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_14b_empty_frontend_id_rejected(self):
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id=_VALID_SESSION,
                frontend_conversation_id="",
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_14c_whitespace_only_frontend_id_rejected(self):
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id=_VALID_SESSION,
                frontend_conversation_id="   ",
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass


# ---------------------------------------------------------------------------
# Test 15 — Exception repr exposes no raw inputs
# ---------------------------------------------------------------------------


class TestExceptionSafety:
    """Test 15: Exceptions never expose raw identifiers."""

    def test_15_exception_repr_exposes_no_inputs(self):
        owner = "c" * 64  # valid format but triggers the bad-session rejection
        session = "bad\nsession"
        frontend = "frontend-safe-001"
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=owner,
                session_id=session,
                frontend_conversation_id=frontend,
            )
        except ProcessLocalConversationKeyError as exc:
            r = repr(exc)
            s = str(exc)
            # No raw inputs in repr or str
            assert owner not in r
            assert "bad" not in r  # session prefix
            assert "session" not in r
            assert frontend not in r
            assert owner not in s
            assert "bad" not in s
            assert frontend not in s

    def test_15b_bad_owner_exception_repr_clean(self):
        bad_owner = "AAAA"  # too short and uppercase
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=bad_owner,
                session_id=_VALID_SESSION,
                frontend_conversation_id=_VALID_FRONTEND,
            )
        except ProcessLocalConversationKeyError as exc:
            assert bad_owner not in repr(exc)
            assert bad_owner not in str(exc)


# ---------------------------------------------------------------------------
# Test 16 — No identifiers logged
# ---------------------------------------------------------------------------


class TestNoIdentifiersLogged:
    """Test 16: Logging does not expose raw inputs."""

    def test_16_no_identifiers_in_log_output(self):
        """Capture log output and confirm raw inputs never appear."""
        import io
        handler = logging.StreamHandler(io.StringIO())
        handler.setLevel(logging.DEBUG)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        level_before = root_logger.level
        root_logger.setLevel(logging.DEBUG)
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id=_VALID_SESSION,
                frontend_conversation_id=_VALID_FRONTEND,
            )
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(level_before)

        log_output = handler.stream.getvalue()
        assert _VALID_OWNER not in log_output
        assert _VALID_SESSION not in log_output
        assert _VALID_FRONTEND not in log_output

    def test_16b_no_identifiers_logged_on_error(self):
        """Even validation failures must not log raw inputs."""
        import io
        handler = logging.StreamHandler(io.StringIO())
        handler.setLevel(logging.DEBUG)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        level_before = root_logger.level
        root_logger.setLevel(logging.DEBUG)
        bad_session = "contaminated\rsession"
        try:
            try:
                build_process_local_conversation_key(
                    owner_user_id_hash=_VALID_OWNER,
                    session_id=bad_session,
                    frontend_conversation_id=_VALID_FRONTEND,
                )
            except ProcessLocalConversationKeyError:
                pass
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(level_before)

        log_output = handler.stream.getvalue()
        assert _VALID_OWNER not in log_output
        assert "contaminated" not in log_output


# ---------------------------------------------------------------------------
# Test 17 — Thread-safe deterministic concurrent use
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Test 17: Concurrent calls with distinct inputs remain independent."""

    def test_17_thread_safe_deterministic(self):
        results: dict = {}
        errors: list = []

        def worker(thread_id: int, owner: str, session: str, frontend: str):
            try:
                key = build_process_local_conversation_key(
                    owner_user_id_hash=owner,
                    session_id=session,
                    frontend_conversation_id=frontend,
                )
                results[thread_id] = key
            except Exception as exc:  # noqa: BLE001
                errors.append((thread_id, exc))

        threads = [
            threading.Thread(
                target=worker,
                args=(i, _VALID_OWNER if i % 2 == 0 else _VALID_OWNER_B,
                      f"session-{i:04d}", f"conv-{i:04d}"),
            )
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 20

        # All keys are valid format
        for key in results.values():
            assert is_valid_process_local_key(key), f"Invalid key: {key!r}"

        # Same owner+session+frontend → same key (deterministic across threads)
        expected_even = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id="session-0000",
            frontend_conversation_id="conv-0000",
        )
        assert results[0] == expected_even


# ---------------------------------------------------------------------------
# Test 18 — Large valid Unicode frontend input
# ---------------------------------------------------------------------------


class TestLargeUnicodeFrontendInput:
    """Test 18: Large valid Unicode frontend ID follows the established validator contract."""

    def test_18_large_unicode_frontend_follows_validator_contract(self):
        """A frontend ID with non-ASCII Unicode and no '@' is accepted.

        DurableGenieSessionKey only requires: non-empty after strip, no '@'.
        This test proves we don't add a stricter limit.
        """
        # 200 Unicode characters — well within 256 chars, contains Japanese text
        unicode_frontend = "\u30b3\u30f3\u30d0\u30fc\u30b8\u30e7\u30f3" * 28 + "x"  # 197+1=198 chars
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=unicode_frontend,
        )
        assert is_valid_process_local_key(result)
        assert unicode_frontend.encode("utf-8").decode("utf-8") not in result  # raw not present

    def test_18b_unicode_frontend_without_at_accepted(self):
        """Unicode content without '@' passes DurableGenieSessionKey contract."""
        frontend = "\u4e2d\u6587id-12345"  # Chinese chars + id
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=frontend,
        )
        assert is_valid_process_local_key(result)


# ---------------------------------------------------------------------------
# is_valid_process_local_key boundary tests
# ---------------------------------------------------------------------------


class TestIsValidProcessLocalKey:
    """Boundary tests for the format-check helper."""

    def test_valid_key_accepted(self):
        key = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=_VALID_FRONTEND,
        )
        assert is_valid_process_local_key(key) is True

    def test_raw_session_id_rejected(self):
        assert is_valid_process_local_key("session-123:conv-456") is False

    def test_raw_owner_hash_rejected(self):
        assert is_valid_process_local_key(_VALID_OWNER) is False

    def test_raw_frontend_id_rejected(self):
        assert is_valid_process_local_key(_VALID_FRONTEND) is False

    def test_non_string_rejected(self):
        assert is_valid_process_local_key(None) is False
        assert is_valid_process_local_key(42) is False
        assert is_valid_process_local_key([]) is False

    def test_wrong_prefix_rejected(self):
        # Correct length but wrong prefix
        assert is_valid_process_local_key("plc_v2_" + "a" * 64) is False

    def test_uppercase_hex_rejected(self):
        # Uppercase hex chars in digest part must be rejected
        assert is_valid_process_local_key("plc_v1_" + "A" * 64) is False

    def test_correct_format_accepted(self):
        assert is_valid_process_local_key("plc_v1_" + "0" * 64) is True
        assert is_valid_process_local_key("plc_v1_" + "f" * 64) is True
        assert is_valid_process_local_key("plc_v1_" + "0123456789abcdef" * 4) is True


# ---------------------------------------------------------------------------
# Validator parity — process-local helper vs DurableGenieSessionKey
# ---------------------------------------------------------------------------


class TestFrontendIDValidatorParity:
    """Explicit contract-parity tests: process-local helper and
    DurableGenieSessionKey must accept and reject the same representative
    frontend conversation IDs.

    BEHAVIOURAL NOTE — whitespace handling:
    Both validators strip leading/trailing whitespace before their
    accept/reject checks, so accept/reject parity is preserved for all
    representative cases below.  However, DurableGenieSessionKey stores the
    STRIPPED value as the durable key, while the process-local helper uses
    the RAW (un-stripped) value in the SHA-256 digest.  This is a known,
    accepted difference: it affects the resulting digest but NOT which inputs
    are accepted or rejected.

    CONTROL CHAR NOTE:
    Neither DurableGenieSessionKey nor the process-local helper restricts
    control characters in frontend_conversation_id.  This differs from
    session_id, which DOES reject control characters.
    """

    def test_parity_plain_id_accepted(self):
        """Plain identifier accepted by both contracts."""
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id="conv-plain-001",
        )
        assert is_valid_process_local_key(result)

    def test_parity_leading_whitespace_accepted(self):
        """Leading whitespace is accepted (stripped for validation, raw in digest).

        DurableGenieSessionKey strips and stores the stripped value.
        process_local helper strips for validation only; uses raw value in digest.
        Both ACCEPT the input — accept/reject parity holds.
        """
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id="  conv-padded",
        )
        assert is_valid_process_local_key(result)

    def test_parity_trailing_whitespace_accepted(self):
        """Trailing whitespace is accepted by both contracts."""
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id="conv-padded  ",
        )
        assert is_valid_process_local_key(result)

    def test_parity_empty_rejected(self):
        """Empty string rejected by both contracts (confirms parity)."""
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id=_VALID_SESSION,
                frontend_conversation_id="",
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_parity_whitespace_only_rejected(self):
        """Whitespace-only string rejected by both contracts (confirms parity)."""
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id=_VALID_SESSION,
                frontend_conversation_id="   ",
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_parity_at_sign_rejected(self):
        """Email-like ID with @ rejected by both contracts (confirms parity)."""
        try:
            build_process_local_conversation_key(
                owner_user_id_hash=_VALID_OWNER,
                session_id=_VALID_SESSION,
                frontend_conversation_id="user@example.com",
            )
            assert False, "Should have raised"
        except ProcessLocalConversationKeyError:
            pass

    def test_parity_unicode_accepted(self):
        """Unicode without @ accepted by both contracts."""
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id="\u5bfe\u8a71-12345",
        )
        assert is_valid_process_local_key(result)

    def test_parity_max_representative_valid_accepted(self):
        """Very long frontend ID without @ accepted by both contracts.

        Neither DurableGenieSessionKey nor process_local imposes a length cap
        on frontend_conversation_id.
        """
        long_id = "conv-" + "x" * 500  # 505 chars, no @
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=long_id,
        )
        assert is_valid_process_local_key(result)

    def test_parity_control_char_in_frontend_accepted_by_both(self):
        """Control characters in frontend_conversation_id are accepted by both
        DurableGenieSessionKey and process_local helper.

        Neither validator restricts control characters in the frontend ID.
        (This differs from session_id, which rejects control characters.)
        """
        # \x01 SOH is a control char; neither validator blocks it in frontend ID
        ctrl_frontend = "conv\x01id"
        result = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_OWNER,
            session_id=_VALID_SESSION,
            frontend_conversation_id=ctrl_frontend,
        )
        assert is_valid_process_local_key(result)
