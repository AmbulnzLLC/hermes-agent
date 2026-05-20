"""Tests for ``docker/install_github_app_pem.py``.

The install script runs at container boot in the AmbulnzLLC pilot (and any
deployment using a GitHub App for outbound git/GitHub auth) to drop the
App's private-key PEM at a well-known path so Hermes's built-in
``GitHubAuth._try_github_app()`` can mint installation tokens.  Its contract:

- ``GITHUB_APP_PEM_SECRET_ID`` unset → silent no-op (generic deploys aren't
  pilot).
- ``GITHUB_APP_PEM_SECRET_ID`` set → install REQUIRED.  Any failure (boto3
  missing, AWS API error, malformed JSON, missing PRIVATE_KEY field, write
  failure) crashes the boot with a non-zero exit code.  Loud > silent.
- Literal ``\\n`` two-byte sequences in PRIVATE_KEY are normalized to real
  newlines (a known secret-format quirk).
- PEM is written atomically (tmp + rename) at mode 0400.
- Idempotent: skips the write when the destination already matches.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "docker" / "install_github_app_pem.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_install_github_app_pem", SCRIPT_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A real (throwaway) RSA PEM so the "starts with -----BEGIN" sanity check
# passes.  Generated specifically for these tests; never used to sign
# anything.  Trimmed to a single representative block — the script doesn't
# parse the key, just checks the header and writes bytes.
_FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n"
    "KUpRKfFLfRYC9AIKjbJTWit+CqvjWYzvQwECAwEAAQJAIJLixBy2qpFoS4DSmoEm\n"
    "o3qGy0t6z09AIJtH+5OeRV1be+N4cDYJKffGzDa88vQENZiRm0GRq6a+HPGQMd2k\n"
    "TQIhAKMSvzIBnni7ot/OSie2TmJLY4SwTQAevXysE2RbFDYdAiEBCUEaRQnMnbp7\n"
    "9mxDXDf6AU0cN/RPBjb9qSHDcWZHGzUCIG2Es59z8ugGrDY+pxLQnwfotadxd+Uy\n"
    "v/Ow5T0q5gIJAiEAyS4RaI9YG8EWx/2w0T67ZUVAw8eOMB6BIUg0Xcu+3okCIBOs\n"
    "/5OiPgoTdSy7bcF9IGpSE8ZgGKzgYQVZeN97YE00\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
        "GITHUB_APP_PEM_SECRET_ID",
        "GITHUB_APP_PEM_DEST_PATH",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def dest_path(tmp_path, monkeypatch):
    """A writable destination path inside the test tmpdir."""
    p = tmp_path / "secrets" / "github-app.pem"
    monkeypatch.setenv("GITHUB_APP_PEM_DEST_PATH", str(p))
    return p


def _install_fake_boto3(
    monkeypatch, secret_string: str | None = None, *, raise_exc: Exception | None = None
):
    """Insert a fake ``boto3`` + ``botocore.exceptions`` into sys.modules.

    The script does ``import boto3`` and ``from botocore.exceptions import
    BotoCoreError, ClientError`` lazily inside ``main()`` so we monkey-patch
    at the sys.modules level rather than touching the script's globals.
    """
    # Real botocore.exceptions classes — easier than faking them since the
    # script catches both as base ``Exception`` subclasses.
    class _BotoCoreError(Exception):
        pass

    class _ClientError(Exception):
        pass

    fake_boto3 = MagicMock()
    fake_botocore_exceptions = MagicMock()
    fake_botocore_exceptions.BotoCoreError = _BotoCoreError
    fake_botocore_exceptions.ClientError = _ClientError

    client = MagicMock()
    if raise_exc is not None:
        client.get_secret_value.side_effect = raise_exc
    else:
        resp = {}
        if secret_string is not None:
            resp["SecretString"] = secret_string
        client.get_secret_value.return_value = resp
    fake_boto3.client.return_value = client

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", MagicMock())
    monkeypatch.setitem(
        sys.modules, "botocore.exceptions", fake_botocore_exceptions
    )
    return fake_boto3, client, _BotoCoreError, _ClientError


# ---------------------------------------------------------------------------
# Silent no-op contract
# ---------------------------------------------------------------------------


def test_no_secret_id_is_noop(clean_env, dest_path):
    """Generic (non-pilot) deploys: silently do nothing, exit 0."""
    mod = _load_module()
    assert mod.main() == 0
    assert not dest_path.exists()


def test_empty_secret_id_is_noop(clean_env, dest_path, monkeypatch):
    """Empty/whitespace secret id is treated as unset, not as misconfig."""
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "   ")
    mod = _load_module()
    assert mod.main() == 0
    assert not dest_path.exists()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_installs_pem_from_secret(clean_env, dest_path, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    secret = json.dumps({"PRIVATE_KEY": _FAKE_PEM, "CLIENT_ID": "Iv23l..."})
    _install_fake_boto3(monkeypatch, secret_string=secret)

    mod = _load_module()
    assert mod.main() == 0

    assert dest_path.exists()
    contents = dest_path.read_text()
    assert contents.startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert contents.endswith("\n")
    assert "-----END RSA PRIVATE KEY-----" in contents
    # 0o400 — owner read-only
    assert (dest_path.stat().st_mode & 0o777) == 0o400


def test_normalizes_literal_backslash_n_in_private_key(
    clean_env, dest_path, monkeypatch
):
    """Some console-pasted secrets have ``\\n`` literals instead of real newlines."""
    escaped = _FAKE_PEM.replace("\n", "\\n")
    assert "\\n" in escaped and "\n" not in escaped
    secret = json.dumps({"PRIVATE_KEY": escaped})
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    _install_fake_boto3(monkeypatch, secret_string=secret)

    mod = _load_module()
    assert mod.main() == 0

    contents = dest_path.read_text()
    # Real newlines after normalization — the PEM parses cleanly.
    assert "\n" in contents
    assert "\\n" not in contents
    assert contents.startswith("-----BEGIN RSA PRIVATE KEY-----")


def test_idempotent_when_destination_matches(clean_env, dest_path, monkeypatch):
    """Second invocation with the same secret content skips the write."""
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    secret = json.dumps({"PRIVATE_KEY": _FAKE_PEM})
    _install_fake_boto3(monkeypatch, secret_string=secret)

    mod = _load_module()
    assert mod.main() == 0
    first_mtime = dest_path.stat().st_mtime_ns

    # Second run — get_secret_value is still called (idempotency check is
    # against destination contents, not against a cache), but the file
    # itself should not be rewritten.  Sleep briefly so mtime would
    # otherwise differ.
    import time

    time.sleep(0.01)
    assert mod.main() == 0
    second_mtime = dest_path.stat().st_mtime_ns
    assert first_mtime == second_mtime


def test_uses_aws_region_env(clean_env, dest_path, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    secret = json.dumps({"PRIVATE_KEY": _FAKE_PEM})
    fake_boto3, _client, _, _ = _install_fake_boto3(
        monkeypatch, secret_string=secret
    )

    mod = _load_module()
    assert mod.main() == 0
    fake_boto3.client.assert_called_once_with(
        "secretsmanager", region_name="us-east-1"
    )


# ---------------------------------------------------------------------------
# Crash-loud contract: every failure mode returns non-zero
# ---------------------------------------------------------------------------


def test_aws_api_failure_is_fatal(clean_env, dest_path, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    # Single install: build the fake module with a raising client.  The
    # exception we raise must be an instance of the SAME _BotoCoreError
    # class the script imports from sys.modules['botocore.exceptions'],
    # so we can't re-install the fake module after constructing the
    # exception (that would replace the class in sys.modules).
    _, _, _BotoCoreError, _ = _install_fake_boto3(monkeypatch, secret_string="{}")
    # Now point the client at a raising side_effect using the SAME class.
    fake_botocore_exceptions = sys.modules["botocore.exceptions"]
    boom = fake_botocore_exceptions.BotoCoreError("boom")
    sys.modules["boto3"].client.return_value.get_secret_value.side_effect = boom

    mod = _load_module()
    rc = mod.main()
    assert rc != 0
    assert not dest_path.exists()


def test_missing_secret_string_is_fatal(clean_env, dest_path, monkeypatch):
    """Binary secrets (no SecretString) are not supported."""
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    _install_fake_boto3(monkeypatch, secret_string=None)

    mod = _load_module()
    assert mod.main() != 0
    assert not dest_path.exists()


def test_invalid_json_is_fatal(clean_env, dest_path, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    _install_fake_boto3(monkeypatch, secret_string="not-json {{")

    mod = _load_module()
    assert mod.main() != 0
    assert not dest_path.exists()


def test_missing_private_key_field_is_fatal(clean_env, dest_path, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    secret = json.dumps({"CLIENT_ID": "Iv23l..."})  # no PRIVATE_KEY
    _install_fake_boto3(monkeypatch, secret_string=secret)

    mod = _load_module()
    assert mod.main() != 0
    assert not dest_path.exists()


def test_empty_private_key_is_fatal(clean_env, dest_path, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    secret = json.dumps({"PRIVATE_KEY": "   "})
    _install_fake_boto3(monkeypatch, secret_string=secret)

    mod = _load_module()
    assert mod.main() != 0
    assert not dest_path.exists()


def test_non_pem_key_is_fatal(clean_env, dest_path, monkeypatch):
    """Garbage that isn't a PEM is rejected before write."""
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    secret = json.dumps({"PRIVATE_KEY": "totally not a PEM"})
    _install_fake_boto3(monkeypatch, secret_string=secret)

    mod = _load_module()
    assert mod.main() != 0
    assert not dest_path.exists()


def test_secret_payload_not_object_is_fatal(clean_env, dest_path, monkeypatch):
    """Secret JSON must be an object, not an array or scalar."""
    monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ID", "my/secret")
    _install_fake_boto3(monkeypatch, secret_string=json.dumps([_FAKE_PEM]))

    mod = _load_module()
    assert mod.main() != 0
    assert not dest_path.exists()
