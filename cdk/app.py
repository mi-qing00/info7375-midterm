#!/usr/bin/env python3
import os

import aws_cdk as cdk
from stack import EventDrivenStack

app = cdk.App()

EventDrivenStack(
    app,
    "Week5EventDrivenStack",
    # Account and region are auto-discovered from the active AWS
    # credentials/profile (CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION) -
    # never hardcoded, so this repo works in any student's account.
    # Override with `-c account=... -c region=...` if needed.
    env=cdk.Environment(
        account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="Info7375 Week 5 - Event-Driven Architecture Lab",
)

app.synth()
