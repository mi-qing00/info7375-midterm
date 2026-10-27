"""
InventoryConsumer Lambda
========================
Triggered by: SQS inventory-queue (batch_size=10, partial batch failures enabled)
Does:
  1. Extracts the OrderPlaced event from the SQS/SNS envelope
  2. Checks idempotency log - skip if already processed
  3. Atomically decrements stock in DynamoDB with a condition expression
  4. On stock-out: publishes StockOutEvent (does NOT throw - stock-out is not a crash)
  5. Writes to idempotency log
  6. Returns batch item failures for any messages that failed processing

IDEMPOTENCY KEY: order_id
If this Lambda crashes after the DynamoDB decrement but before the SQS
deleteMessage, SQS will redeliver the message. The idempotency check in
step 2 prevents a double-decrement on the retry.

This is the exact failure students reproduce in Break Scenario B.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

dynamodb     = boto3.resource("dynamodb")
sns_client   = boto3.client("sns")

INVENTORY_TABLE           = os.environ["INVENTORY_TABLE"]
INVENTORY_PROCESSED_TABLE = os.environ["INVENTORY_PROCESSED_TABLE"]
ORDER_TOPIC_ARN           = os.environ["ORDER_TOPIC_ARN"]
TTL_SECONDS               = 7 * 24 * 3600   # idempotency log TTL: 7 days

inventory_table  = dynamodb.Table(INVENTORY_TABLE)
processed_table  = dynamodb.Table(INVENTORY_PROCESSED_TABLE)


def lambda_handler(event, context):
    """
    Returns {"batchItemFailures": [...]} for any messages that failed.
    SQS will redeliver ONLY those messages - not the whole batch.
    """
    batch_item_failures = []

    for record in event["Records"]:
        message_id = record["messageId"]
        try:
            _process_record(record)
        except Exception as exc:
            logger.error("record_processing_failed",
                         extra={"message_id": message_id, "error": str(exc)})
            # Return this message ID as a failure - SQS will retry it
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


def _process_record(record: dict):
    # -- Unwrap SNS -> SQS envelope -----------------------------------------
    body    = json.loads(record["body"])
    message = json.loads(body["Message"])   # SNS wraps the payload in "Message"

    order_id = message["order_id"]
    items    = message["items"]

    # -- BREAK PHASE INJECTION POINT (Scenario D) ----------------------------
    # Log receipt order for ordering-test messages, BEFORE the idempotency
    # check - deliberately. All ordering_test() messages share ONE order_id
    # (they represent status updates on a single order), so the idempotency
    # guard would otherwise skip every message after the first as a
    # "duplicate," hiding delivery order entirely. This logs the raw
    # DEQUEUE order SQS actually delivered in, independent of what business
    # logic does with each message afterward.
    if "sequence" in message:
        logger.info("ordering_test_received",
                    extra={"order_id": order_id,
                           "sequence": message["sequence"],
                           "status": message.get("status")})

    # -- BREAK PHASE INJECTION POINT (Scenario C) ----------------------------
    # Artificially slow this invocation down - simulating a degraded
    # consumer (a slow downstream call, CPU contention), not a crash.
    # Applied uniformly, before anything else, regardless of whether this
    # message turns out to be an idempotency-hit, a stock-out, or a clean
    # success - Scenario C is about LATENCY, not correctness.
    #
    # CAUTION: batch_size=10 means up to 10 records are processed
    # SEQUENTIALLY within ONE invocation - this delay is PER MESSAGE, so a
    # worst-case full batch multiplies it by 10. With this function's
    # timeout=29s, keep delays at 2s or less so a full batch (10 x 2s = 20s)
    # stays safely under the timeout, with room left for real work too.
    delay_seconds = message.get("_artificial_delay_seconds")
    if delay_seconds:
        time.sleep(float(delay_seconds))

    # -- Idempotency check --------------------------------------------------
    # If we already processed this order, skip without error.
    # This handles SQS at-least-once redelivery safely.
    try:
        existing = processed_table.get_item(Key={"order_id": order_id})
        if "Item" in existing:
            logger.info("idempotency_hit",
                        extra={"order_id": order_id,
                               "processed_at": existing["Item"].get("processed_at")})
            return   # already processed - skip cleanly
    except ClientError as e:
        logger.error("idempotency_check_failed",
                     extra={"order_id": order_id, "error": str(e)})
        raise  # let SQS retry

    # ~dk NO! Leave this commented-out. Would not work. Replaced with further below.
    # -- BREAK PHASE INJECTION POINT ----------------------------------------
    # Uncomment to simulate a crash AFTER the DB write but BEFORE SQS ack.
    # This forces SQS to redeliver and tests idempotency protection.
    #
    # import random
    # if random.random() < 0.5:
    #     # Simulate crash mid-processing (after potential partial work)
    #     raise RuntimeError("Simulated mid-processing crash")

    # -- Decrement stock for each item -------------------------------------
    stock_outs = []
    for item in items:
        product_id = item["product_id"]
        quantity   = int(item["quantity"])
        try:
            inventory_table.update_item(
                Key={"product_id": product_id},
                # Condition: stock_count >= quantity (prevents negative stock)
                # This is an atomic check-and-decrement - no race condition
                UpdateExpression="SET stock_count = stock_count - :qty, "
                                 "updated_at = :ts",
                ConditionExpression="stock_count >= :qty",
                ExpressionAttributeValues={
                    ":qty": quantity,
                    ":ts":  datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info("stock_decremented",
                        extra={"order_id": order_id,
                               "product_id": product_id,
                               "quantity": quantity})
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Stock too low - record stock-out but do NOT raise
                # A stock-out is a business event, not a system error
                logger.warning("stock_out",
                               extra={"order_id": order_id,
                                      "product_id": product_id,
                                      "quantity": quantity})
                stock_outs.append(product_id)
            else:
                raise  # real DynamoDB error - let SQS retry

    # -- Publish StockOutEvent if any items ran out -------------------------
    if stock_outs:
        sns_client.publish(
            TopicArn=ORDER_TOPIC_ARN,
            Message=json.dumps({
                "event_type":   "StockOut",
                "order_id":     order_id,
                "product_ids":  stock_outs,
                "occurred_at":  datetime.now(timezone.utc).isoformat(),
            }),
            Subject="StockOut",
            # Without this, StockOut events fan out to ALL THREE queues
            # via the same topic - including back to InventoryConsumer's
            # own queue, where message["items"] does not exist on a
            # StockOut payload and raises KeyError on every single
            # stock-out. Every stock-out (a normal business event) would
            # then flood the inventory-dlq, which the CloudWatch alarm
            # treats as threshold=0/critical - a benign, expected event
            # masquerading as the "something is critically broken"
            # signal this whole dashboard is built around.
            MessageAttributes={
                "event_type": {
                    "DataType": "String",
                    "StringValue": "StockOut",
                }
            },
        )

    # -- BREAK PHASE INJECTION POINT (Scenario B) ----------------------------
    # Crash AFTER the decrement succeeds but BEFORE the idempotency record
    # below is written - the exact gap the comment on that write is about.
    # Only fires for a message explicitly marked for this test, and only on
    # the FIRST delivery attempt (via SQS's own ApproximateReceiveCount) -
    # so attempt 1 decrements stock then crashes before recording it, SQS
    # redelivers, and attempt 2 has no record that attempt 1 got this far -
    # it decrements a SECOND time before finally succeeding. Compare
    # stock_count before/after to see the double-decrement directly.
    if message.get("_simulate_crash_after_decrement"):
        receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
        if receive_count == 1:
            raise RuntimeError("Scenario B: simulated crash after decrement, before idempotency write")

    # -- Write idempotency record -------------------------------------------
    # Write AFTER successful processing - not before.
    # If we wrote before and then crashed, we'd silently skip the order on retry.
    processed_table.put_item(Item={
        "order_id":     order_id,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "stock_outs":   stock_outs,
        "ttl":          int(time.time()) + TTL_SECONDS,
    })

    logger.info("inventory_processed",
                extra={"order_id": order_id,
                       "items_count": len(items),
                       "stock_outs": stock_outs})
