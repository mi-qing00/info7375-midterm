"""
Shared test helpers.

Your handlers read their configuration and create their boto3 clients when they are IMPORTED (the normal Lambda
pattern: warm invocations reuse them). So each test has to
  1. start a moto mock,
  2. create the AWS resources the handler expects,
  3. THEN import the handler fresh.
load_module() does step 3 under a unique module name every time, so nothing leaks between tests.

Add whatever else your option needs (a queue, a second table, environment variables) to the `aws` fixture.
"""
import importlib.util
import itertools
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

REPO = Path(__file__).resolve().parent.parent
LAMBDAS = REPO / "lambdas"

_counter = itertools.count()


def load_module(relative_path: str):
    """Import a Lambda handler file fresh, e.g. load_module("waiting_room/join_handler.py")."""
    path = LAMBDAS / relative_path
    folder = str(path.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)          # like Lambda: files next to the handler (telemetry.py) can be imported
    name = f"_under_test_{next(_counter)}_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Aws:
    """Handle to the mocked resources, passed to your tests."""


@pytest.fixture
def aws(monkeypatch):
    """A fresh mocked AWS with an admissions table and the environment the skeleton's handlers read."""
    with mock_aws():
        for key, value in {
            "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "us-east-1", "AWS_REGION": "us-east-1",
        }.items():
            monkeypatch.setenv(key, value)

        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="wr-admissions",
            KeySchema=[{"AttributeName": "visitor_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "visitor_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        a = Aws()
        a.ddb = ddb
        a.table = boto3.resource("dynamodb").Table("wr-admissions")

        for key, value in {
            "ADMISSIONS_TABLE": "wr-admissions",
            "ADMIT_PER_SECOND": "10",
            "POLL_INTERVAL_SECONDS": "5",
            "ADMISSION_VALID_SECONDS": "600",
        }.items():
            monkeypatch.setenv(key, value)
        yield a
