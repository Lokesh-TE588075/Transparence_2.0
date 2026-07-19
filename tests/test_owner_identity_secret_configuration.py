"""Phase 4B1 — Owner-Identity HMAC Secret Configuration Tests.

Validates that app.yaml is correctly configured for the HMAC secret resource,
that the feature flag is disabled, that no plaintext secret exists anywhere in
the repository, and that no production Python files have been modified to
enable request-path identity enforcement.

All tests are static/declarative — they parse files and inspect source text.
No live API calls, no secret value retrieval, no network I/O.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(".").resolve()
_APP_YAML = _REPO_ROOT / "app.yaml"
_REQUEST_OWNER_IDENTITY = _REPO_ROOT / "app" / "services" / "request_owner_identity.py"
_CHAT_PY = _REPO_ROOT / "app" / "routes" / "chat.py"
_MAIN_PY = _REPO_ROOT / "app" / "main.py"
_REQUIREMENTS = _REPO_ROOT / "requirements.txt"

# Phase 4B1 constants (public metadata only — no secret value)
_SECRET_RESOURCE_KEY = "conversation-owner-hmac-secret"
_SECRET_SCOPE = "transparence-owner-identity"
_SECRET_KEY_NAME = "conversation-owner-hmac-v1"
_ENV_VAR_HMAC = "CONVERSATION_OWNER_HMAC_SECRET"
_ENV_VAR_FLAG = "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY"
_HMAC_SECRET_SOURCE_ENV_VAR = "CONVERSATION_OWNER_HMAC_SECRET"

# Patterns whose presence in repository files would indicate a leaked secret.
# URL-safe base64 strings of 48+ bytes encode as 64+ chars of [A-Za-z0-9_-].
_SUSPICIOUS_SECRET_RE = re.compile(r"[A-Za-z0-9_\-]{64,}")


# ---------------------------------------------------------------------------
# Shared fixture: parsed app.yaml
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_yaml_doc() -> Dict[str, Any]:
    """Return the parsed app.yaml document."""
    content = _APP_YAML.read_text(encoding="utf-8")
    return yaml.safe_load(content)


@pytest.fixture(scope="module")
def app_yaml_env(app_yaml_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the flat list of env entries from app.yaml."""
    return app_yaml_doc.get("env", [])


@pytest.fixture(scope="module")
def env_by_name(app_yaml_env: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Return a dict mapping env entry name → entry dict."""
    return {e["name"]: e for e in app_yaml_env if "name" in e}


# ---------------------------------------------------------------------------
# Test 22: app.yaml parses successfully (placed first for fast failure)
# ---------------------------------------------------------------------------


def test_app_yaml_parses_successfully():
    """Test 22 — app.yaml must be valid YAML."""
    content = _APP_YAML.read_text(encoding="utf-8")
    doc = yaml.safe_load(content)
    assert isinstance(doc, dict), "app.yaml root must be a YAML mapping"
    assert "env" in doc, "app.yaml must contain an 'env' key"


# ---------------------------------------------------------------------------
# Tests 1–5: CONVERSATION_OWNER_HMAC_SECRET configuration
# ---------------------------------------------------------------------------


def test_hmac_secret_env_var_exists(env_by_name: Dict[str, Dict[str, Any]]):
    """Test 1 — CONVERSATION_OWNER_HMAC_SECRET must be present in app.yaml env."""
    assert _ENV_VAR_HMAC in env_by_name, (
        f"{_ENV_VAR_HMAC!r} not found in app.yaml env entries; "
        f"found: {list(env_by_name)}"
    )


def test_hmac_secret_uses_value_from(env_by_name: Dict[str, Dict[str, Any]]):
    """Test 2 — CONVERSATION_OWNER_HMAC_SECRET must use valueFrom (not inline value)."""
    entry = env_by_name.get(_ENV_VAR_HMAC, {})
    assert "valueFrom" in entry, (
        f"{_ENV_VAR_HMAC!r} must use 'valueFrom' instead of 'value'. Entry: {entry}"
    )


def test_hmac_secret_value_from_points_to_correct_resource(
    env_by_name: Dict[str, Dict[str, Any]]
):
    """Test 3 — valueFrom must equal the app resource key 'conversation-owner-hmac-secret'."""
    entry = env_by_name.get(_ENV_VAR_HMAC, {})
    actual = entry.get("valueFrom")
    assert actual == _SECRET_RESOURCE_KEY, (
        f"{_ENV_VAR_HMAC!r} valueFrom must be {_SECRET_RESOURCE_KEY!r}, got {actual!r}"
    )


def test_hmac_secret_has_no_inline_value(env_by_name: Dict[str, Dict[str, Any]]):
    """Test 4 — CONVERSATION_OWNER_HMAC_SECRET must not have a 'value' field."""
    entry = env_by_name.get(_ENV_VAR_HMAC, {})
    assert "value" not in entry, (
        f"{_ENV_VAR_HMAC!r} must not have an inline 'value' field; entry: {entry}"
    )


def test_no_plaintext_secret_in_app_yaml():
    """Test 5 — app.yaml must not contain any plaintext HMAC secret value."""
    raw = _APP_YAML.read_text(encoding="utf-8")
    matches = _SUSPICIOUS_SECRET_RE.findall(raw)
    # Allow known non-secret long strings: GUIDs, warehouse IDs, table paths,
    # deployment IDs, URLs.  They are short or contain dots/slashes.
    # Any 64+ char alphanum-only token is suspicious.
    suspicious = [
        m for m in matches
        if len(m) >= 64
        and "/" not in m  # not a path
        and "." not in m  # not a table/URL reference
        and "-" * 2 not in m  # not a UUID segment with hyphens
        and m not in (
            # Known long identifiers in app.yaml
            "01f17a93e6aa1b97a9da7ef329e15e46",  # Genie space ID
            "01f17ee9d6681d73bd555f0783fdfd09",
            "01f181110277111f8f8d22379e477ecc",
        )
    ]
    assert not suspicious, (
        f"Suspicious long token(s) in app.yaml that may be a plaintext secret: {suspicious}"
    )


# ---------------------------------------------------------------------------
# Tests 6–7: ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY feature flag
# ---------------------------------------------------------------------------


def test_feature_flag_exists(env_by_name: Dict[str, Dict[str, Any]]):
    """Test 6 — ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY must be present."""
    assert _ENV_VAR_FLAG in env_by_name, (
        f"{_ENV_VAR_FLAG!r} not found in app.yaml env entries"
    )


def test_feature_flag_is_disabled(env_by_name: Dict[str, Dict[str, Any]]):
    """Test 7 — ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY must be 'false'."""
    entry = env_by_name.get(_ENV_VAR_FLAG, {})
    actual = entry.get("value")
    assert actual == "false", (
        f"{_ENV_VAR_FLAG!r} must be 'false', got {actual!r}. "
        "Do not enable this flag until Phase 4B2 integration is complete."
    )


# ---------------------------------------------------------------------------
# Test 8: No duplicate environment variable names
# ---------------------------------------------------------------------------


def test_no_duplicate_env_names(app_yaml_env: List[Dict[str, Any]]):
    """Test 8 — env section must not contain duplicate variable names."""
    names = [e["name"] for e in app_yaml_env if "name" in e]
    seen = set()
    dupes = []
    for n in names:
        if n in seen:
            dupes.append(n)
        seen.add(n)
    assert not dupes, f"Duplicate env variable names in app.yaml: {dupes}"


# ---------------------------------------------------------------------------
# Test 9: Existing Lakebase valueFrom remains 'postgres'
# ---------------------------------------------------------------------------


def test_lakebase_endpoint_name_value_from_unchanged(
    env_by_name: Dict[str, Dict[str, Any]]
):
    """Test 9 — LAKEBASE_ENDPOINT_NAME must still use valueFrom: postgres."""
    entry = env_by_name.get("LAKEBASE_ENDPOINT_NAME", {})
    assert entry, "LAKEBASE_ENDPOINT_NAME missing from app.yaml"
    assert entry.get("valueFrom") == "postgres", (
        f"LAKEBASE_ENDPOINT_NAME valueFrom must be 'postgres', got: {entry}"
    )


# ---------------------------------------------------------------------------
# Test 10: Existing persistence flags remain disabled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("var_name,expected_value", [
    ("ENABLE_DURABLE_GENIE_SESSION_ADAPTER", "false"),
    ("CONVERSATION_REPOSITORY_BACKEND", "memory"),
    ("ENABLE_LAKEBASE_CONVERSATION_REPOSITORY", "false"),
])
def test_persistence_flags_remain_disabled(
    var_name: str,
    expected_value: str,
    env_by_name: Dict[str, Dict[str, Any]],
):
    """Test 10 — Phase 3C persistence flags must remain at their disabled defaults."""
    entry = env_by_name.get(var_name, {})
    assert entry, f"{var_name!r} is missing from app.yaml"
    actual = entry.get("value")
    assert actual == expected_value, (
        f"{var_name!r} must be {expected_value!r}, got {actual!r}"
    )


# ---------------------------------------------------------------------------
# Tests 11–13: request_owner_identity.py source validation
# ---------------------------------------------------------------------------


def test_request_owner_identity_reads_correct_env_var():
    """Test 11 — request_owner_identity.py must read CONVERSATION_OWNER_HMAC_SECRET."""
    source = _REQUEST_OWNER_IDENTITY.read_text(encoding="utf-8")
    assert _HMAC_SECRET_SOURCE_ENV_VAR in source, (
        f"{_REQUEST_OWNER_IDENTITY.name} must reference "
        f"{_HMAC_SECRET_SOURCE_ENV_VAR!r}"
    )


def test_no_default_secret_in_source():
    """Test 12 — No hardcoded default secret value in request_owner_identity.py."""
    source = _REQUEST_OWNER_IDENTITY.read_text(encoding="utf-8")
    # The from_environment method must not supply a fallback default value
    # for the secret.  A default would allow silent operation without config.
    assert ".get(" + repr(_HMAC_SECRET_SOURCE_ENV_VAR) + "," not in source.replace(" ", ""), (
        "from_environment must not provide a default value for the HMAC secret"
    )
    # Confirm .get() call has no second argument for this env var
    pattern = re.compile(
        r"\.get\s*\(\s*cls\.secret_env_var\s*,",
    )
    assert not pattern.search(source), (
        "from_environment must not supply a fallback default for secret_env_var"
    )


def test_no_generated_runtime_secret():
    """Test 13 — No code generates a runtime secret if the env var is absent."""
    source = _REQUEST_OWNER_IDENTITY.read_text(encoding="utf-8")
    # Confirm no secrets.token_* or os.urandom call inside the module
    assert "secrets.token" not in source, (
        "request_owner_identity.py must not auto-generate a fallback secret"
    )
    assert "os.urandom" not in source, (
        "request_owner_identity.py must not auto-generate a fallback secret"
    )


# ---------------------------------------------------------------------------
# Tests 14–17: Enforcement isolation — chat.py, main.py, request path
# ---------------------------------------------------------------------------


def test_chat_py_does_not_import_request_owner_identity():
    """Test 14 — chat.py must not import request_owner_identity."""
    source = _CHAT_PY.read_text(encoding="utf-8")
    assert "request_owner_identity" not in source, (
        "chat.py must not import or reference request_owner_identity — "
        "identity enforcement must not be active in the request path"
    )


def test_chat_py_does_not_read_x_forwarded_user():
    """Test 15 — chat.py must not read X-Forwarded-User header."""
    source = _CHAT_PY.read_text(encoding="utf-8")
    assert "X-Forwarded-User" not in source, (
        "chat.py must not read X-Forwarded-User — "
        "owner identity extraction must not be in the request path"
    )
    assert "x-forwarded-user" not in source.lower(), (
        "chat.py must not read X-Forwarded-User (case-insensitive check)"
    )


def test_main_py_does_not_import_request_owner_identity():
    """Test 16 — main.py must not import request_owner_identity."""
    source = _MAIN_PY.read_text(encoding="utf-8")
    assert "request_owner_identity" not in source, (
        "main.py must not import or reference request_owner_identity"
    )


def test_no_request_path_identity_enforcement():
    """Test 17 — No request-path file should enforce owner identity."""
    request_path_files = [
        _REPO_ROOT / "app" / "routes" / "chat.py",
        _REPO_ROOT / "app" / "routes" / "export.py",
        _REPO_ROOT / "app" / "routes" / "feedback.py",
        _REPO_ROOT / "app" / "routes" / "health.py",
        _REPO_ROOT / "app" / "main.py",
    ]
    for path in request_path_files:
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        assert "request_owner_identity" not in source, (
            f"{path.name} must not reference request_owner_identity — "
            "identity enforcement must remain dormant until Phase 4B2"
        )
        assert "RequestOwnerIdentityProvider" not in source, (
            f"{path.name} must not reference RequestOwnerIdentityProvider"
        )


# ---------------------------------------------------------------------------
# Test 18: No secret value in repository files
# ---------------------------------------------------------------------------


def test_no_secret_value_in_repository_files():
    """Test 18 — No long random-looking token (potential HMAC secret) in source files."""
    # Scan files most likely to accidentally capture a secret
    scan_paths = [
        _APP_YAML,
        _REQUEST_OWNER_IDENTITY,
        _CHAT_PY,
        _MAIN_PY,
        _REQUIREMENTS,
        _REPO_ROOT / "app" / "config.py",
        _REPO_ROOT / "app" / "services" / "genie_backend_factory.py",
    ]
    # A URL-safe base64 token of 48 bytes → 64 chars; look for 56+ char tokens
    # that are NOT known benign identifiers (UUIDs, deployment IDs).
    _KNOWN_LONG_IDS = {
        "01f17a93e6aa1b97a9da7ef329e15e46",
        "01f17ee9d6681d73bd555f0783fdfd09",
        "01f181110277111f8f8d22379e477ecc",
        "8e46614f7064d8fd",
    }
    leak_pattern = re.compile(r"[A-Za-z0-9_\-]{56,}")
    for p in scan_paths:
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for match in leak_pattern.finditer(text):
            token = match.group()
            if token in _KNOWN_LONG_IDS:
                continue
            if "/" in token or "." in token:
                continue
            # All-hyphen separator lines (YAML comment decorators) are not secrets
            if set(token) <= {"-"}:
                continue
            # URL-safe base64 token of 56+ chars without slashes/dots is suspicious.
            assert False, (
                f"Possible secret token found in {p.name}: token length={len(token)} "
                f"(value deliberately not printed for security)"
            )


# ---------------------------------------------------------------------------
# Test 19: Secret is not logged in request_owner_identity.py
# ---------------------------------------------------------------------------


def test_secret_not_logged():
    """Test 19 — request_owner_identity.py must not log the secret value."""
    source = _REQUEST_OWNER_IDENTITY.read_text(encoding="utf-8")
    # The module uses __repr__ with <redacted> to prevent accidental logging.
    assert "<redacted>" in source, (
        "RequestOwnerIdentitySettings.__repr__ must redact the secret"
    )
    # Confirm no logger call references hmac_secret directly
    assert "logger." not in source, (
        "request_owner_identity.py must not use a logger instance "
        "(risk of accidental secret logging)"
    )


# ---------------------------------------------------------------------------
# Test 20: requirements.txt unchanged
# ---------------------------------------------------------------------------


_REQUIREMENTS_EXPECTED_PACKAGES = [
    "fastapi",
    "uvicorn",
    "pydantic",
    "databricks-sdk",
    "databricks-sql-connector",
    "psycopg",
    "openai",
    "sqlparse",
    "rapidfuzz",
    "pandas",
    "python-dotenv",
]


def test_requirements_txt_unchanged():
    """Test 20 — requirements.txt must not have been modified by Phase 4B1."""
    content = _REQUIREMENTS.read_text(encoding="utf-8")
    for pkg in _REQUIREMENTS_EXPECTED_PACKAGES:
        assert pkg in content, (
            f"Expected package {pkg!r} not found in requirements.txt"
        )
    # Confirm no secret-management library was added
    secret_libs = ["cryptography", "PyNaCl", "secret", "keyring"]
    for lib in secret_libs:
        assert lib not in content, (
            f"Unexpected secret-management library {lib!r} found in requirements.txt "
            "— Phase 4B1 must not introduce new runtime dependencies"
        )


# ---------------------------------------------------------------------------
# Test 21: No deployment configuration enables the feature
# ---------------------------------------------------------------------------


def test_no_deployment_config_enables_feature():
    """Test 21 — No deployment config file enables ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY."""
    # Check app.yaml (already tested via test_feature_flag_is_disabled)
    raw = _APP_YAML.read_text(encoding="utf-8")
    # Confirm it does not appear with value 'true'
    enabled_pattern = re.compile(
        r"ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY.*?value:\s*[\"']?true[\"']?",
        re.DOTALL,
    )
    assert not enabled_pattern.search(raw), (
        "app.yaml must not enable ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY"
    )
    # Also check for any .env files that might override
    env_files = list((_REPO_ROOT).glob(".env*"))
    for ef in env_files:
        if ef.name in (".env.example", ".env.template"):
            continue
        content = ef.read_text(encoding="utf-8")
        if "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY" in content:
            assert "true" not in content.split(
                "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY"
            )[1].split("\n")[0], (
                f"{ef.name} enables ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY"
            )
