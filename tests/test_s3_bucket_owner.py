"""eval-mcp must only use S3 buckets owned by the expected AWS account.

Bucket names are global and `<name>-<account-id>` is predictable, so another
account could pre-create it. These tests check that every S3 call pins
ExpectedBucketOwner and that `eval-mcp init` rejects a bucket it can't verify.
"""

from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from eval_mcp import cli, s3_sync

OWNER = "111111111111"
OTHER = "222222222222"


def _head_error(code, status):
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": {}}},
        "HeadBucket",
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("EVAL_MCP_HOME", str(tmp_path))
    monkeypatch.setattr("eval_mcp.config.get_home", lambda: tmp_path)
    s3_sync._caller_account = None


# --- botocore accepts the parameter on every call we make -------------------

def test_head_bucket_and_list_accept_expected_owner():
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    with Stubber(client) as stub:
        stub.add_response("head_bucket", {}, {"Bucket": "b", "ExpectedBucketOwner": OWNER})
        stub.add_response("list_objects_v2", {}, {"Bucket": "b", "Prefix": "p", "ExpectedBucketOwner": OWNER})
        client.head_bucket(Bucket="b", ExpectedBucketOwner=OWNER)
        client.list_objects_v2(Bucket="b", Prefix="p", ExpectedBucketOwner=OWNER)


def test_transfer_allows_expected_owner():
    from boto3.s3.transfer import S3Transfer

    assert "ExpectedBucketOwner" in S3Transfer.ALLOWED_UPLOAD_ARGS
    assert "ExpectedBucketOwner" in S3Transfer.ALLOWED_DOWNLOAD_ARGS


# --- eval-mcp init ------------------------------------------------------------

def _fake_s3(owned_buckets):
    """head_bucket succeeds only for buckets owned by OWNER."""
    client = MagicMock()

    def head_bucket(Bucket, ExpectedBucketOwner=None):
        if ExpectedBucketOwner is None:
            raise AssertionError("head_bucket called without ExpectedBucketOwner")
        if Bucket in owned_buckets and ExpectedBucketOwner == OWNER:
            return {"ResponseMetadata": {"HTTPHeaders": {"x-amz-bucket-region": "us-west-2"}}}
        if Bucket in owned_buckets or Bucket.startswith("squatted"):
            raise _head_error("403", 403)
        raise _head_error("404", 404)

    client.head_bucket.side_effect = head_bucket
    return client


def _resolve(name, owned, account=OWNER):
    with patch.object(cli, "_get_account_id", return_value=account), \
         patch("boto3.client", return_value=_fake_s3(owned)):
        return cli._resolve_bucket(name)


def test_init_accepts_owned_suffixed_bucket():
    assert _resolve("team", {f"team-{OWNER}"}) == (f"team-{OWNER}", "us-west-2", OWNER, None)


def test_init_rejects_bucket_owned_by_another_account():
    # Attacker pre-created squatted-<account-id> in their own account.
    name, _, _, err = _resolve("squatted", set())
    assert name is None and err == cli._ERR_FORBIDDEN


def test_init_fails_closed_without_account_id():
    with patch.object(cli, "_get_account_id", return_value=None), \
         patch.object(cli, "_detect_bucket_region", return_value=("us-west-2", None)) as probe:
        name, _, _, err = cli._resolve_bucket("team")
    assert name is None and err == cli._ERR_NO_OWNER
    probe.assert_called_once_with("team")


def test_init_honours_bucket_owner_override():
    from eval_mcp.config import set_config_value

    set_config_value("bucket_owner", OTHER)
    with patch.object(cli, "_get_account_id", return_value=OWNER), \
         patch.object(cli, "_detect_bucket_region", return_value=("us-west-2", None)) as probe:
        assert cli._resolve_bucket("shared")[2] == OTHER
    probe.assert_called_once_with("shared", expected_owner=OTHER)


# --- sync calls ---------------------------------------------------------------

def test_sync_calls_pin_owner(tmp_path, monkeypatch):
    from eval_mcp.config import set_config_value

    set_config_value("bucket", "team-bucket")
    set_config_value("bucket_owner", OWNER)
    root = tmp_path / "users" / "me"
    root.mkdir(parents=True)
    (root / "a.json").write_text("{}")
    monkeypatch.setenv("USER_STORAGE_BASE", str(tmp_path / "users"))

    s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": [{"Key": "users/me/b.json"}]}]
    s3.get_paginator.return_value = paginator
    with patch.object(s3_sync, "_get_s3_client", return_value=s3):
        s3_sync.sync_up(user_id="me")
        s3_sync.sync_down(user_id="me")

    for call in paginator.paginate.call_args_list:
        assert call.kwargs["ExpectedBucketOwner"] == OWNER
    s3.upload_file.assert_called_once()
    assert s3.upload_file.call_args.kwargs["ExtraArgs"] == {"ExpectedBucketOwner": OWNER}
    s3.download_file.assert_called()
    for call in s3.download_file.call_args_list:
        assert call.kwargs["ExtraArgs"] == {"ExpectedBucketOwner": OWNER}


def test_sync_falls_back_to_caller_account():
    with patch("boto3.client") as client:
        client.return_value.get_caller_identity.return_value = {"Account": OWNER}
        assert s3_sync._expected_owner() == OWNER
        assert s3_sync._expected_owner() == OWNER
    client.assert_called_once_with("sts")
