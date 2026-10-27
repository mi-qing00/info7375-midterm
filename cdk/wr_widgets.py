"""
Option A's own dashboard widgets (at most 3; stack.py refuses a 4th). Each answers one question about OUR design:

  1. Is the line paced at the rate we configured?            admitted per minute vs rate x 60
  2. Is the front door safe?                                 requests/s on the whole stage vs its 500/s limit, and
                                                             POST /orders vs its 40/s cap (the two places a 429 comes from)
  3. Is the one hot row (the counter) healthy?               UpdateItem latency, throttles, conditional-check failures

All CloudWatch metrics here have a 1-minute resolution, so per-second values are 1-minute AVERAGES (Sum / 60).
Short spikes inside a minute are flattened: summarize_stampede.py's 10-second windows show those.
"""
import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_dynamodb as dynamodb

API_NAME = "week5-orders-api"
STAGE = "prod"
STAGE_LIMIT_PER_SECOND = 500          # stack.py: deploy_options throttling_rate_limit
ORDERS_CAP_PER_SECOND = 40            # the exam's cap on POST /orders
ONE_MIN = cdk.Duration.minutes(1)


def _api_count(**extra_dimensions):
    return cw.Metric(namespace="AWS/ApiGateway", metric_name="Count",
                     dimensions_map={"ApiName": API_NAME, **extra_dimensions},
                     period=ONE_MIN, statistic="Sum")


def build(wr) -> list:
    target_per_minute = wr.admissions_per_second * 60

    pacing = cw.GraphWidget(
        title=f"Line pacing: admitted per minute vs configured {wr.admissions_per_second:g}/s",
        left=[cw.Metric(namespace="Info7375/WaitingRoom", metric_name="Admitted",
                        period=ONE_MIN, statistic="Sum", label="admitted per minute")],
        left_annotations=[cw.HorizontalAnnotation(value=target_per_minute,
                                                  label=f"configured rate x 60 = {target_per_minute:g}")],
        left_y_axis=cw.YAxisProps(min=0),
        width=8,
    )

    front_door = cw.GraphWidget(
        title="Front door headroom: requests/s (1-min avg) vs limits",
        left=[
            cw.MathExpression(expression="s / 60", using_metrics={"s": _api_count()},
                              label="whole API (all routes)", period=ONE_MIN),
            cw.MathExpression(expression="o / 60",
                              using_metrics={"o": _api_count(Stage=STAGE, Resource="/orders", Method="POST")},
                              label="POST /orders", period=ONE_MIN),
        ],
        left_annotations=[
            cw.HorizontalAnnotation(value=STAGE_LIMIT_PER_SECOND, label="stage limit 500/s"),
            cw.HorizontalAnnotation(value=ORDERS_CAP_PER_SECOND, label="orders cap 40/s"),
        ],
        left_y_axis=cw.YAxisProps(min=0),
        width=8,
    )

    table = wr.counters_table
    counter_row = cw.GraphWidget(
        title="Counter row (the hot key): UpdateItem health",
        left=[
            table.metric_throttled_requests_for_operations(
                operations=[dynamodb.Operation.UPDATE_ITEM], period=ONE_MIN, label="throttled"),
            table.metric_conditional_check_failed_requests(
                period=ONE_MIN, statistic="Sum", label="condition failed (busy/idle switch)"),
        ],
        right=[table.metric_successful_request_latency(
            dimensions_map={"TableName": table.table_name, "Operation": "UpdateItem"},
            period=ONE_MIN, statistic="p99", label="UpdateItem p99 latency (ms)")],
        left_y_axis=cw.YAxisProps(min=0, label="count per minute"),
        right_y_axis=cw.YAxisProps(min=0, label="ms"),
        width=8,
    )
    return [pacing, front_door, counter_row]
