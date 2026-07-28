"""The RDS region is required under IAM auth, and is NOT the Bedrock region.

Two regions legitimately differ in this codebase: Bedrock model calls default to
us-east-2 (the only region serving the OpenAI frontier models), while RDS lives
wherever it was deployed. Signing an RDS IAM token for the wrong region fails as
an opaque auth error, so the database region is never guessed.

Leaving it merely unset was its own trap: boto3 then builds
``https://rds..amazonaws.com`` and raises "Invalid endpoint" — precisely the
cryptic failure the no-guessing rule exists to avoid. It is validated where it
is actually consumed (IAM auth) rather than unconditionally, because password
auth never touches it.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def _pg_env(monkeypatch):
    """Minimal required Postgres env, with every region hint removed."""
    monkeypatch.setenv("POSTGRES_HOST", "db.example.internal")
    monkeypatch.setenv("POSTGRES_DB", "evaldb")
    monkeypatch.setenv("POSTGRES_USER", "evaluser")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)


def test_iam_auth_without_region_fails_with_actionable_message(_pg_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_USE_IAM_AUTH", "true")
    from backend.core.database import Database

    with pytest.raises(RuntimeError) as excinfo:
        Database()

    message = str(excinfo.value)
    assert "AWS_REGION" in message
    # Must not send the reader chasing the Bedrock region.
    assert "Bedrock" in message


def test_password_auth_without_region_is_fine(_pg_env, monkeypatch):
    """Only IAM auth consumes the region; don't break password-auth setups."""
    monkeypatch.setenv("POSTGRES_USE_IAM_AUTH", "false")
    from backend.core.database import Database

    assert Database().region == ""


def test_iam_auth_uses_the_configured_region(_pg_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_USE_IAM_AUTH", "true")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    from backend.core.database import Database

    assert Database().region == "eu-west-1"


def test_region_is_not_taken_from_the_bedrock_default(_pg_env, monkeypatch):
    """The database must never inherit bedrock_client.DEFAULT_REGION.

    If it did, a user in eu-west-1 would sign RDS tokens for us-east-2 and see an
    auth failure with no hint as to why.
    """
    monkeypatch.setenv("POSTGRES_USE_IAM_AUTH", "false")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    from backend.core.database import Database
    from eval_mcp.core.bedrock_client import DEFAULT_REGION

    region = Database().region
    assert region == "eu-west-1"
    assert region != DEFAULT_REGION
