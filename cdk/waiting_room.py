"""
YOUR WAITING ROOM  (this file is yours to change)
=================================================
The provided stack (cdk/stack.py) builds this construct when you deploy with `-c waiting_room=on`. It hands you the
EXISTING REST API and connects your admissions table to the provided gate. Everything that makes your option work
goes here, in lambdas/waiting_room/, and in the tests you write.

WHAT THE STACK REQUIRES FROM YOU   (tests/test_starter_wiring.py checks all of it: keep it passing)
  1. A constructor   WaitingRoom(scope, construct_id, *, api, admissions_per_second)
  2. self.admissions_table : a DynamoDB table keyed by `visitor_id` (string). The provided gate reads and consumes
     the admission records in it (see CONTRACT.md, section 4). This skeleton already creates one.
  3. These two routes on `api` (already added below):
         POST /waiting-room/join
         GET  /waiting-room/status/{visitor_id}

OPTIONAL
  - self.functions : a list of every Lambda function you create. The dashboard's "errors and throttles" panel sums
    over this list, so add each function you create to it.
  - dashboard_widgets() : return up to 3 CloudWatch widgets for the dashboard's "your widgets" row.

WHAT THIS SKELETON ALREADY DOES: the admissions table, two stub functions that answer 501 (not implemented), and
the two routes. WHAT IS LEFT TO YOU: everything that makes it a waiting room. Change anything in this file.
"""
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudwatch as cw          # noqa: F401  (used by dashboard_widgets once you add widgets)
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

HANDLERS = Path(__file__).resolve().parent.parent / "lambdas" / "waiting_room"


class WaitingRoom(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        api: apigw.RestApi,
        admissions_per_second: float = 10,
    ) -> None:
        super().__init__(scope, construct_id)
        self.admissions_per_second = admissions_per_second

        # -- 1. REQUIRED: the table the provided gate reads ------------------------------------------------------
        self.admissions_table = dynamodb.Table(
            self, "Admissions",
            table_name="week5-wr-admissions",
            partition_key=dynamodb.Attribute(name="visitor_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # -- 2. The two functions behind the two routes. Both are STUBS until you write them. -----------------
        env = {
            "ADMISSIONS_TABLE": self.admissions_table.table_name,
            "ADMIT_PER_SECOND": str(int(admissions_per_second)),
            "POLL_INTERVAL_SECONDS": "5",
            "ADMISSION_VALID_SECONDS": "600",
        }
        code = lambda_.Code.from_asset(str(HANDLERS))

        def make_fn(construct_id, function_name, handler):
            return lambda_.Function(
                self, construct_id,
                function_name=function_name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=handler,
                code=code,
                timeout=cdk.Duration.seconds(10),
                memory_size=128,
                environment=env,
                log_retention=logs.RetentionDays.ONE_WEEK,
            )

        self.join_fn = make_fn("Join", "week5-wr-join", "join_handler.lambda_handler")
        self.status_fn = make_fn("Status", "week5-wr-status", "status_handler.lambda_handler")
        # TODO: grant each function exactly the access it needs. As shipped, ONLY the status function can use the
        # admissions table, and neither function can use anything else. If your design has join read or write that
        # table (or a queue, or a cache), grant it here, or the function fails with an AccessDenied error at run time.
        self.admissions_table.grant_read_write_data(self.status_fn)

        # Every function you create belongs in this list (the dashboard reads it).
        self.functions = [self.join_fn, self.status_fn]

        # -- 3. REQUIRED: the two routes ---------------------------------------------------------------------
        waiting_room = api.root.add_resource("waiting-room")
        waiting_room.add_resource("join").add_method("POST", apigw.LambdaIntegration(self.join_fn))
        waiting_room.add_resource("status").add_resource("{visitor_id}").add_method(
            "GET", apigw.LambdaIntegration(self.status_fn))

        # TODO: everything else your option needs.

    def dashboard_widgets(self) -> list:
        """Up to 3 widgets for the dashboard's 'your widgets' row. Show something that matters for YOUR design.

        EXAMPLE (delete it or replace it):

            return [cw.GraphWidget(
                title="Admissions table - throttled requests",
                left=[self.admissions_table.metric_throttled_requests_for_operations(
                    operations=[dynamodb.Operation.PUT_ITEM], period=cdk.Duration.minutes(1))],
                width=8,
            )]
        """
        return []
