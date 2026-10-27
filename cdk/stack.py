"""
Info7375 Week 5 - Event-Driven Architecture CDK Stack

Topology:
  POST /orders  ->  API Gateway  ->  Lambda: OrderProducer
                                        |
                                    SNS: order-events
                                   --------------------------
                              SQS:           SQS:           SQS:
                           inventory      notification    analytics
                              |               |               |
                           Lambda:         Lambda:         Lambda:
                        Inventory       Notification     Analytics
                        Consumer         Consumer        Consumer
                           |               |               |
                        DynamoDB         SES +          Kinesis
                        inventory      DynamoDB         Data
                        table         sent_notifs       Stream

Each queue has its own DLQ.
All DLQ depths alarm at > 0 (inventory, notification) or > 10 (analytics).

YOUR WAITING ROOM: deploy with `-c waiting_room=on` to build the WaitingRoom construct from cdk/waiting_room.py
(your work) and put it in front of POST /orders. Without the flag this stack is the plain Week 5 baseline.
Every line the waiting room touches in this file sits between ">>> WAITING ROOM" and "<<< WAITING ROOM" markers.
This file is part of the provided kit: do not edit it (scripts/verify_kit.py checks).

ALSO OPTIONAL: `-c orders_rate_limit=N` throttles ONLY POST /orders to N requests per second
(the exam's failing baseline). See MIDTERM_TOOLS.md.
"""
import json
import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_sqs as sqs,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_events,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_kinesis as kinesis,
    aws_logs as logs,
    aws_ses as ses,
)
from constructs import Construct


class EventDrivenStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # -- DynamoDB tables ------------------------------------------------

        orders_table = dynamodb.Table(
            self, "OrdersTable",
            table_name="week5-orders",
            partition_key=dynamodb.Attribute(
                name="order_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            point_in_time_recovery=False,
        )

        inventory_table = dynamodb.Table(
            self, "InventoryTable",
            table_name="week5-inventory",
            partition_key=dynamodb.Attribute(
                name="product_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # Idempotency log for NotificationConsumer
        # PK = order_id, TTL = 7 days - prevents duplicate emails on retry
        sent_notifications_table = dynamodb.Table(
            self, "SentNotificationsTable",
            table_name="week5-sent-notifications",
            partition_key=dynamodb.Attribute(
                name="order_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            time_to_live_attribute="ttl",   # auto-expire after 7 days
        )

        # Idempotency log for InventoryConsumer
        inventory_processed_table = dynamodb.Table(
            self, "InventoryProcessedTable",
            table_name="week5-inventory-processed",
            partition_key=dynamodb.Attribute(
                name="order_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            time_to_live_attribute="ttl",
        )

        # -- Kinesis stream (analytics sink) -------------------------------
        analytics_stream = kinesis.Stream(
            self, "AnalyticsStream",
            stream_name="week5-analytics",
            shard_count=1,              # 1 shard = 1 MB/s write, enough for lab
            retention_period=cdk.Duration.hours(24),
            # CDK defaults Kinesis streams to RemovalPolicy.RETAIN - safe for
            # production data, but means `cdk destroy` leaves this stream
            # behind, colliding with the next `cdk deploy` on this same
            # hardcoded stream_name. DESTROY is correct for our lab environment
            # where a clean destroy/redeploy cycle matters more than
            # protecting this stream's data.
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # -- SNS topic ------------------------------------------------------
        order_topic = sns.Topic(
            self, "OrderEventsTopic",
            topic_name="week5-order-events",
            display_name="Info7375 Week5 Order Events",
        )

        # -- Helper: build a queue + its DLQ -------------------------------
        def make_queue(name: str, visibility_timeout_s: int = 180,
                       max_receive_count: int = 3,
                       retention_days: int = 4) -> tuple[sqs.Queue, sqs.Queue]:
            dlq = sqs.Queue(
                self, f"{name}Dlq",
                queue_name=f"week5-{name}-dlq",
                retention_period=cdk.Duration.days(14),
                # DLQ messages kept 14 days - long enough to investigate and replay
            )
            queue = sqs.Queue(
                self, f"{name}Queue",
                queue_name=f"week5-{name}",
                visibility_timeout=cdk.Duration.seconds(visibility_timeout_s),
                retention_period=cdk.Duration.days(retention_days),
                dead_letter_queue=sqs.DeadLetterQueue(
                    max_receive_count=max_receive_count,
                    queue=dlq,
                ),
            )
            return queue, dlq

        # Visibility timeout = 6 x Lambda timeout (29s) per AWS recommendation
        inventory_queue,     inventory_dlq     = make_queue("inventory",     180, 3)
        notification_queue,  notification_dlq  = make_queue("notification",  180, 3)
        analytics_queue,     analytics_dlq     = make_queue("analytics",     180, 3)

        # Subscribe all three queues to the SNS topic.
        #
        # inventory_queue and notification_queue are filtered to
        # OrderPlaced only. Without this, InventoryConsumer's own
        # StockOut publish (same topic, unfiltered fan-out) loops back
        # to its OWN queue - message["items"] does not exist on a
        # StockOut payload, so every stock-out (a normal business
        # event) would KeyError, retry 3x, and land in inventory-dlq -
        # directly polluting the exact "threshold=0=critical" signal
        # this week's dashboard is built around. NotificationConsumer
        # has the same exposure via its permissive .get() calls: a
        # StockOut event (same order_id as the real order, no
        # "user_id"/"total") can race the real OrderPlaced-triggered
        # send, write the idempotency record first, and cause the
        # REAL confirmation to be silently skipped.
        #
        # analytics_queue stays unfiltered on purpose - AnalyticsConsumer
        # is written to tolerate any event shape via .get() defaults,
        # and StockOut events are genuinely useful analytics data.
        synchronous_event_filter = sns.SubscriptionFilter.string_filter(
            allowlist=["OrderPlaced", "OrderStatusUpdate"]
        )

        order_topic.add_subscription(sns_subs.SqsSubscription(
            inventory_queue,
            filter_policy={"event_type": synchronous_event_filter},
        ))
        order_topic.add_subscription(sns_subs.SqsSubscription(
            notification_queue,
            filter_policy={"event_type": synchronous_event_filter},
        ))
        order_topic.add_subscription(sns_subs.SqsSubscription(analytics_queue))

        # -- Shared Lambda environment --------------------------------------
        common_env = {
            "ORDERS_TABLE":               orders_table.table_name,
            "INVENTORY_TABLE":            inventory_table.table_name,
            "SENT_NOTIFICATIONS_TABLE":   sent_notifications_table.table_name,
            "INVENTORY_PROCESSED_TABLE":  inventory_processed_table.table_name,
            "ANALYTICS_STREAM":           analytics_stream.stream_name,
            "ORDER_TOPIC_ARN":            order_topic.topic_arn,
            "AWS_REGION_NAME":            self.region,
            "LOG_LEVEL":                  "INFO",
        }

        # -- Lambda layer: shared utilities --------------------------------
        # In the lab, each Lambda is self-contained (easier to read).
        # A shared layer would DRY up common code in production.

        # -- Lambda: OrderProducer ------------------------------------------
        producer_fn = lambda_.Function(
            self, "OrderProducer",
            function_name="week5-order-producer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../lambdas/order_producer"),
            timeout=cdk.Duration.seconds(29),
            memory_size=256,
            environment=common_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        orders_table.grant_write_data(producer_fn)
        order_topic.grant_publish(producer_fn)

        # -- Lambda: InventoryConsumer --------------------------------------
        inventory_fn = lambda_.Function(
            self, "InventoryConsumer",
            function_name="week5-inventory-consumer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../lambdas/inventory_consumer"),
            timeout=cdk.Duration.seconds(29),
            memory_size=256,
            environment=common_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        inventory_fn.add_event_source(
            lambda_events.SqsEventSource(
                inventory_queue,
                batch_size=10,
                # Partial batch response: if 3 of 10 messages fail,
                # only those 3 are retried - not the whole batch
                report_batch_item_failures=True,
            )
        )
        inventory_table.grant_read_write_data(inventory_fn)
        inventory_processed_table.grant_read_write_data(inventory_fn)

        # -- Lambda: NotificationConsumer -----------------------------------
        notification_fn = lambda_.Function(
            self, "NotificationConsumer",
            function_name="week5-notification-consumer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../lambdas/notification_consumer"),
            timeout=cdk.Duration.seconds(29),
            memory_size=128,
            environment={
                **common_env,
                # Set to a verified SES email for real sending.
                # Leave as stub@example.com for lab - handler detects and logs only.
                "FROM_EMAIL": "stub@example.com",
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        notification_fn.add_event_source(
            lambda_events.SqsEventSource(
                notification_queue,
                batch_size=1,           # emails MUST NOT duplicate - process one at a time
                report_batch_item_failures=True,
            )
        )
        sent_notifications_table.grant_read_write_data(notification_fn)
        # SES send permission
        notification_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ses:SendEmail", "ses:SendRawEmail"],
            resources=["*"],
        ))

        # -- Lambda: AnalyticsConsumer --------------------------------------
        analytics_fn = lambda_.Function(
            self, "AnalyticsConsumer",
            function_name="week5-analytics-consumer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../lambdas/analytics_consumer"),
            timeout=cdk.Duration.seconds(29),
            memory_size=128,
            environment=common_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        analytics_fn.add_event_source(
            lambda_events.SqsEventSource(
                analytics_queue,
                batch_size=25,          # analytics tolerates higher batching
                # AWS Lambda's SQS event source REQUIRES an explicit
                # batching window whenever batch_size exceeds 10 (10 is
                # the max Lambda will accumulate on its own before
                # invoking) - without this, CDK's own synth-time
                # validation rejects the stack outright:
                # "Maximum batch size must be between 1 and 10
                # inclusive (given 25) when batching window is not
                # specified." This isn't a runtime/AWS-API failure,
                # it's client-side CDK validation - cdk deploy would
                # never even reach AWS with the original config.
                max_batching_window=cdk.Duration.seconds(30),
                report_batch_item_failures=True,
            )
        )
        analytics_stream.grant_write(analytics_fn)

        # -- API Gateway ----------------------------------------------------
        # >>> ORDERS CAP (exam add-on) >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        # Optional: throttle ONLY POST /orders, to model a backend with limited capacity.
        #     cdk deploy -c orders_rate_limit=40              requests/second; the burst allowance
        #                                                     defaults to max(2 x rate, 100)
        #     cdk deploy -c orders_rate_limit=40 -c orders_burst_limit=150
        # Without the flag this stays None and the stack is exactly as it was. With it, a crowd
        # bigger than the cap gets HTTP 429 from API Gateway for the excess: the failing baseline.
        # A waiting-room option can be deployed under the same cap to show it stays beneath it.
        orders_method_options = None
        orders_rate_context = self.node.try_get_context("orders_rate_limit")
        if orders_rate_context:
            try:
                orders_rate = int(orders_rate_context)
                orders_burst = int(self.node.try_get_context("orders_burst_limit")
                                   or max(2 * orders_rate, 100))
            except ValueError:
                raise ValueError("orders_rate_limit and orders_burst_limit must be whole numbers")
            if orders_rate < 1 or orders_burst < orders_rate:
                raise ValueError("orders_rate_limit must be >= 1 and orders_burst_limit >= orders_rate_limit")
            orders_method_options = {
                "/orders/POST": apigw.MethodDeploymentOptions(
                    throttling_rate_limit=orders_rate,
                    throttling_burst_limit=orders_burst,
                )
            }
        # <<< ORDERS CAP <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

        api = apigw.RestApi(
            self, "OrdersApi",
            rest_api_name="week5-orders-api",
            description="Info7375 Week5 - Order ingestion endpoint",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                throttling_rate_limit=500,      # matches scale target
                throttling_burst_limit=1000,
                logging_level=apigw.MethodLoggingLevel.INFO,
                metrics_enabled=True,
                data_trace_enabled=False,       # avoid logging request bodies in prod
                method_options=orders_method_options,   # None unless -c orders_rate_limit is set
            ),
        )

        orders_resource = api.root.add_resource("orders")
        orders_resource.add_method(
            "POST",
            apigw.LambdaIntegration(
                producer_fn,
                proxy=True,
                timeout=cdk.Duration.seconds(29),
            ),
        )

        # Health endpoint - always returns 200, no Lambda needed
        health_resource = api.root.add_resource("health")
        health_resource.add_method(
            "GET",
            apigw.MockIntegration(
                integration_responses=[apigw.IntegrationResponse(
                    status_code="200",
                    response_templates={"application/json": '{"status":"ok"}'},
                )],
                request_templates={"application/json": '{"statusCode":200}'},
            ),
            method_responses=[apigw.MethodResponse(status_code="200")],
        )

        # >>> WAITING ROOM >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        # Opt-in via CDK context:   cdk deploy -c waiting_room=on
        # Absent the flag, NOTHING below runs and the stack is the plain Week 5 baseline.
        # Optional knob:            -c admit_per_second=10
        waiting_room = None
        option = self.node.try_get_context("waiting_room")
        if option:
            if option != "on":
                raise ValueError(f"-c waiting_room must be 'on' (got {option!r})")
            rate = float(self.node.try_get_context("admit_per_second") or 10)
            from waiting_room import WaitingRoom        # YOUR construct: cdk/waiting_room.py
            waiting_room = WaitingRoom(self, "WaitingRoom", api=api, admissions_per_second=rate)

            # The ONLY change to an existing resource: OrderProducer learns to ask the provided gate for an
            # admission token (see waiting_room_gate.py). Write-only access is enough: the gate does a
            # conditional UpdateItem.
            producer_fn.add_environment("WAITING_ROOM_ENABLED", "true")
            producer_fn.add_environment("WAITING_ROOM_TABLE", waiting_room.admissions_table.table_name)
            waiting_room.admissions_table.grant_write_data(producer_fn)
        # <<< WAITING ROOM <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

        # -- CloudWatch Alarms ----------------------------------------------
        alert_topic = sns.Topic(self, "AlertTopic",
                                display_name="Info7375-Week5-Alerts")

        def dlq_alarm(name: str, dlq: sqs.Queue,
                      threshold: int = 0, critical: bool = True):
            alarm = cw.Alarm(
                self, f"{name}DlqAlarm",
                alarm_name=f"Week5-{name}-DLQ-Depth",
                alarm_description=f"{name} DLQ depth > {threshold} - "
                                  f"{'CRITICAL: lost business outcomes' if critical else 'analytics lag'}",
                metric=dlq.metric_approximate_number_of_messages_visible(
                    period=cdk.Duration.minutes(1),
                    statistic="Maximum",
                ),
                threshold=threshold,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))
            return alarm

        def queue_age_alarm(name: str, queue: sqs.Queue, threshold_s: int = 300):
            alarm = cw.Alarm(
                self, f"{name}AgeAlarm",
                alarm_name=f"Week5-{name}-OldestMessage",
                alarm_description=f"{name} oldest message > {threshold_s}s - consumer lagging",
                metric=queue.metric_approximate_age_of_oldest_message(
                    period=cdk.Duration.minutes(1),
                    statistic="Maximum",
                ),
                threshold=threshold_s,
                evaluation_periods=3,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))
            return alarm

        # DLQ alarms - inventory and notification at 0 (any loss = critical)
        dlq_alarm("Inventory",    inventory_dlq,    threshold=0,  critical=True)
        dlq_alarm("Notification", notification_dlq, threshold=0,  critical=True)
        dlq_alarm("Analytics",    analytics_dlq,    threshold=10, critical=False)

        # Queue age alarms - detect consumer lag before messages expire
        queue_age_alarm("Inventory",    inventory_queue,    300)
        queue_age_alarm("Notification", notification_queue, 300)
        queue_age_alarm("Analytics",    analytics_queue,    600)

        # Lambda error rate alarms
        for fn_name, fn in [
            ("Producer",     producer_fn),
            ("Inventory",    inventory_fn),
            ("Notification", notification_fn),
            ("Analytics",    analytics_fn),
        ]:
            cw.Alarm(
                self, f"{fn_name}ErrorAlarm",
                alarm_name=f"Week5-{fn_name}-Errors",
                alarm_description=f"{fn_name} Lambda error rate > 1%",
                metric=fn.metric_errors(
                    period=cdk.Duration.minutes(5),
                    statistic="Sum",
                ),
                threshold=5,
                evaluation_periods=2,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ).add_alarm_action(cw_actions.SnsAction(alert_topic))

        # -- CloudWatch Dashboard -------------------------------------------
        dashboard = cw.Dashboard(
            self, "EventDrivenDashboard",
            dashboard_name="Info7375-Week5-EventDriven",
        )

        def queue_depth_metric(queue: sqs.Queue, label: str):
            return queue.metric_approximate_number_of_messages_visible(
                period=cdk.Duration.minutes(1), statistic="Maximum", label=label,
            )

        def dlq_depth_metric(dlq: sqs.Queue, label: str):
            return dlq.metric_approximate_number_of_messages_visible(
                period=cdk.Duration.minutes(1), statistic="Maximum", label=label,
            )

        dashboard.add_widgets(
            # Row 1: Queue depths - the most important panel during Break
            cw.GraphWidget(
                title="Queue depths (visible messages)",
                left=[
                    queue_depth_metric(inventory_queue,    "inventory"),
                    queue_depth_metric(notification_queue, "notification"),
                    queue_depth_metric(analytics_queue,    "analytics"),
                ],
                width=12,
            ),
            # DLQ depths - zero should be the normal state
            cw.GraphWidget(
                title="DLQ depths - non-zero = action required",
                left=[
                    dlq_depth_metric(inventory_dlq,    "inventory-dlq"),
                    dlq_depth_metric(notification_dlq, "notification-dlq"),
                    dlq_depth_metric(analytics_dlq,    "analytics-dlq"),
                ],
                width=12,
            ),
            # Row 2: Lambda invocations and errors
            cw.GraphWidget(
                title="Lambda invocations",
                left=[fn.metric_invocations(period=cdk.Duration.minutes(1), statistic="Sum", label=name)
                      for name, fn in [("producer", producer_fn), ("inventory", inventory_fn),
                                       ("notification", notification_fn), ("analytics", analytics_fn)]],
                width=12,
            ),
            cw.GraphWidget(
                title="Lambda errors",
                left=[fn.metric_errors(period=cdk.Duration.minutes(1), statistic="Sum", label=name)
                      for name, fn in [("producer", producer_fn), ("inventory", inventory_fn),
                                       ("notification", notification_fn), ("analytics", analytics_fn)]],
                width=12,
            ),
            # Row 3: Duration and oldest message age
            cw.GraphWidget(
                title="Lambda duration p99 (ms)",
                left=[fn.metric_duration(period=cdk.Duration.minutes(1), statistic="p99", label=name)
                      for name, fn in [("producer", producer_fn), ("inventory", inventory_fn),
                                       ("notification", notification_fn), ("analytics", analytics_fn)]],
                width=12,
            ),
            cw.GraphWidget(
                title="Oldest message age (s) - spikes = consumer lag",
                left=[
                    inventory_queue.metric_approximate_age_of_oldest_message(
                        period=cdk.Duration.minutes(1), statistic="Maximum", label="inventory"),
                    notification_queue.metric_approximate_age_of_oldest_message(
                        period=cdk.Duration.minutes(1), statistic="Maximum", label="notification"),
                    analytics_queue.metric_approximate_age_of_oldest_message(
                        period=cdk.Duration.minutes(1), statistic="Maximum", label="analytics"),
                ],
                width=12,
            ),
            # Row 4: API Gateway
            cw.GraphWidget(
                title="API Gateway - request count",
                left=[cw.Metric(
                    namespace="AWS/ApiGateway", metric_name="Count",
                    dimensions_map={"ApiName": "week5-orders-api"},
                    period=cdk.Duration.minutes(1), statistic="Sum",
                )],
                width=8,
            ),
            cw.GraphWidget(
                title="API Gateway - p99 latency (ms)",
                left=[cw.Metric(
                    namespace="AWS/ApiGateway", metric_name="IntegrationLatency",
                    dimensions_map={"ApiName": "week5-orders-api"},
                    period=cdk.Duration.minutes(1), statistic="p99",
                )],
                width=8,
            ),
            cw.GraphWidget(
                title="Kinesis - records written",
                left=[cw.Metric(
                    namespace="AWS/Kinesis", metric_name="PutRecords.Records",
                    dimensions_map={"StreamName": analytics_stream.stream_name},
                    period=cdk.Duration.minutes(1), statistic="Sum",
                )],
                width=8,
            ),
            # Row 5: Lambda throttles and concurrency - all 4 functions.
            # Titles are deliberately NEUTRAL (they say what the panel
            # shows, not what it means) so students draw their own
            # conclusions from the live dashboard. No per-function CDK
            # helper exists for ConcurrentExecutions, so this uses a raw
            # cw.Metric, same pattern as the Kinesis/API Gateway widgets.
            cw.GraphWidget(
                title="Lambda throttles",
                left=[fn.metric_throttles(period=cdk.Duration.minutes(1), statistic="Sum", label=name)
                      for name, fn in [("producer", producer_fn), ("inventory", inventory_fn),
                                       ("notification", notification_fn), ("analytics", analytics_fn)]],
                width=12,
            ),
            cw.GraphWidget(
                title="Lambda concurrent executions",
                left=[cw.Metric(
                    namespace="AWS/Lambda", metric_name="ConcurrentExecutions",
                    dimensions_map={"FunctionName": fn.function_name},
                    period=cdk.Duration.minutes(1), statistic="Maximum", label=name,
                ) for name, fn in [("producer", producer_fn), ("inventory", inventory_fn),
                                    ("notification", notification_fn), ("analytics", analytics_fn)]],
                width=12,
            ),
            # Row 6: Kinesis throughput ceiling and API Gateway error codes
            cw.GraphWidget(
                title="Kinesis - write throughput exceeded",
                left=[cw.Metric(
                    namespace="AWS/Kinesis", metric_name="WriteProvisionedThroughputExceeded",
                    dimensions_map={"StreamName": analytics_stream.stream_name},
                    period=cdk.Duration.minutes(1), statistic="Sum",
                )],
                width=12,
            ),
            cw.GraphWidget(
                title="API Gateway - 4xx and 5xx errors",
                left=[
                    cw.Metric(
                        namespace="AWS/ApiGateway", metric_name="4XXError",
                        dimensions_map={"ApiName": "week5-orders-api"},
                        period=cdk.Duration.minutes(1), statistic="Sum", label="4xx",
                    ),
                    cw.Metric(
                        namespace="AWS/ApiGateway", metric_name="5XXError",
                        dimensions_map={"ApiName": "week5-orders-api"},
                        period=cdk.Duration.minutes(1), statistic="Sum", label="5xx",
                    ),
                ],
                width=12,
            ),
        )

        # >>> WAITING ROOM one-page dashboard >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        # The original dashboard above is left exactly as it was. The waiting-room exercise gets its OWN page:
        # twelve common panels in a grid, then a row for YOUR widgets (waiting_room.dashboard_widgets(), at most
        # three). Titles are neutral on purpose: they say what a panel shows, not what to conclude. The page exists
        # only when your waiting room or the orders cap is deployed.
        if waiting_room is not None or orders_method_options is not None:
            one_min = cdk.Duration.minutes(1)
            core = [("producer", producer_fn), ("inventory", inventory_fn),
                    ("notification", notification_fn), ("analytics", analytics_fn)]
            # every Lambda in the picture, including yours (waiting_room.functions)
            every_fn = [fn for _, fn in core] + list(getattr(waiting_room, "functions", []))

            def total(metric_method, label, prefix):
                """One line summing a metric across every function. Each line in a widget
                needs its own metric-id prefix, or CDK rejects duplicate ids."""
                parts = {f"{prefix}{i}": getattr(fn, metric_method)(period=one_min, statistic="Sum")
                         for i, fn in enumerate(every_fn)}
                return cw.MathExpression(expression="+".join(parts), using_metrics=parts,
                                         label=label, period=one_min)

            # The three standard metrics that lambdas/waiting_room/telemetry.py emits.
            wr_namespace = "Info7375/WaitingRoom"
            joined_metric = cw.Metric(namespace=wr_namespace, metric_name="Joined",
                                      period=one_min, statistic="Sum", label="joined")
            admitted_metric = cw.Metric(namespace=wr_namespace, metric_name="Admitted",
                                        period=one_min, statistic="Sum", label="admitted")
            waiting_now = cw.MathExpression(expression="RUNNING_SUM(j - a)",
                                            using_metrics={"j": joined_metric, "a": admitted_metric},
                                            label="waiting now", period=one_min)

            your_widgets = []
            if waiting_room is not None and hasattr(waiting_room, "dashboard_widgets"):
                your_widgets = list(waiting_room.dashboard_widgets() or [])
            if len(your_widgets) > 3:
                raise ValueError(f"dashboard_widgets() may return at most 3 widgets (got {len(your_widgets)})")

            midterm = cw.Dashboard(self, "MidtermDashboard",
                                   dashboard_name="Info7375-Week5-Midterm")
            midterm.add_widgets(
                # Row 1: what the crowd did to the front door and to the backend
                cw.GraphWidget(
                    title="API Gateway - 4xx and 5xx errors",
                    left=[
                        cw.Metric(namespace="AWS/ApiGateway", metric_name="4XXError",
                                  dimensions_map={"ApiName": "week5-orders-api"},
                                  period=one_min, statistic="Sum", label="4xx"),
                        cw.Metric(namespace="AWS/ApiGateway", metric_name="5XXError",
                                  dimensions_map={"ApiName": "week5-orders-api"},
                                  period=one_min, statistic="Sum", label="5xx"),
                    ],
                    width=8,
                ),
                cw.GraphWidget(
                    title="Lambda concurrent executions",
                    left=[cw.Metric(namespace="AWS/Lambda", metric_name="ConcurrentExecutions",
                                    dimensions_map={"FunctionName": fn.function_name},
                                    period=one_min, statistic="Maximum", label=name)
                          for name, fn in core]
                         + [cw.Metric(namespace="AWS/Lambda", metric_name="ConcurrentExecutions",
                                      period=one_min, statistic="Maximum", label="account total")],
                    width=8,
                ),
                cw.GraphWidget(
                    title="Lambda invocations",
                    left=[fn.metric_invocations(period=one_min, statistic="Sum", label=name)
                          for name, fn in core],
                    width=8,
                ),
                # Row 2: the queues behind the fan-out
                cw.GraphWidget(
                    title="Queue depths (visible messages)",
                    left=[queue_depth_metric(inventory_queue, "inventory"),
                          queue_depth_metric(notification_queue, "notification"),
                          queue_depth_metric(analytics_queue, "analytics")],
                    width=8,
                ),
                cw.GraphWidget(
                    title="Oldest message age (s)",
                    left=[q.metric_approximate_age_of_oldest_message(
                              period=one_min, statistic="Maximum", label=name)
                          for name, q in (("inventory", inventory_queue),
                                          ("notification", notification_queue),
                                          ("analytics", analytics_queue))],
                    width=8,
                ),
                cw.GraphWidget(
                    title="DLQ depths",
                    left=[dlq_depth_metric(inventory_dlq, "inventory-dlq"),
                          dlq_depth_metric(notification_dlq, "notification-dlq"),
                          dlq_depth_metric(analytics_dlq, "analytics-dlq")],
                    width=8,
                ),
                # Row 3: health, tail latency, and how long people waited
                cw.GraphWidget(
                    title="Lambda errors and throttles",
                    left=[total("metric_errors", "errors, all functions", "e"),
                          total("metric_throttles", "throttles, all functions", "t")],
                    width=8,
                ),
                cw.GraphWidget(
                    title="Lambda duration p99 (ms)",
                    left=[fn.metric_duration(period=one_min, statistic="p99", label=name)
                          for name, fn in core],
                    width=8,
                ),
                cw.GraphWidget(
                    title="Time in line (s): median and p95",
                    left=[cw.Metric(namespace=wr_namespace, metric_name="TimeInLineSeconds",
                                    period=one_min, statistic="p50", label="median"),
                          cw.Metric(namespace=wr_namespace, metric_name="TimeInLineSeconds",
                                    period=one_min, statistic="p95", label="p95")],
                    width=8,
                ),
                # Row 4: the line itself, and how much traffic reached the API
                cw.GraphWidget(
                    title="Visitors joined and admitted (per minute)",
                    left=[joined_metric, admitted_metric],
                    width=8,
                ),
                cw.GraphWidget(
                    title="People waiting now (running total)",
                    left=[waiting_now],
                    width=8,
                ),
                cw.GraphWidget(
                    title="API Gateway - request count",
                    left=[cw.Metric(namespace="AWS/ApiGateway", metric_name="Count",
                                    dimensions_map={"ApiName": "week5-orders-api"},
                                    period=one_min, statistic="Sum", label="requests")],
                    width=8,
                ),
            )
            if your_widgets:
                midterm.add_widgets(cw.TextWidget(markdown="### Your widgets", width=24, height=1))
                midterm.add_widgets(*your_widgets)
            cdk.CfnOutput(
                self, "MidtermDashboardUrl",
                value=(f"https://{self.region}.console.aws.amazon.com/cloudwatch/home"
                       f"?region={self.region}#dashboards:name=Info7375-Week5-Midterm"),
                description="One-page waiting-room dashboard",
            )
        # <<< WAITING ROOM <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

        # -- Outputs --------------------------------------------------------
        cdk.CfnOutput(self, "ApiEndpoint",
                      value=api.url,
                      description="Base URL - POST /orders, GET /health")
        cdk.CfnOutput(self, "OrdersTableName",
                      value=orders_table.table_name,
                      description="DynamoDB orders table")
        cdk.CfnOutput(self, "InventoryTableName",
                      value=inventory_table.table_name,
                      description="DynamoDB inventory table")
        cdk.CfnOutput(self, "SentNotificationsTableName",
                      value=sent_notifications_table.table_name,
                      description="DynamoDB idempotency log for notifications")
        cdk.CfnOutput(self, "OrderTopicArn",
                      value=order_topic.topic_arn,
                      description="SNS topic - publish poison pills here for Scenario A")
        cdk.CfnOutput(self, "InventoryQueueUrl",
                      value=inventory_queue.queue_url,
                      description="SQS inventory queue")
        cdk.CfnOutput(self, "InventoryDlqUrl",
                      value=inventory_dlq.queue_url,
                      description="SQS inventory DLQ - inspect failed messages here")
        cdk.CfnOutput(self, "NotificationDlqUrl",
                      value=notification_dlq.queue_url,
                      description="SQS notification DLQ")
        cdk.CfnOutput(self, "AnalyticsStreamName",
                      value=analytics_stream.stream_name,
                      description="Kinesis analytics stream")
        cdk.CfnOutput(self, "DashboardUrl",
                      value=f"https://{self.region}.console.aws.amazon.com/cloudwatch/home"
                            f"#dashboards:name=Info7375-Week5-EventDriven",
                      description="CloudWatch dashboard - open before Break phase")
