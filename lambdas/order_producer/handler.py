"""
OrderProducer Lambda
====================
Triggered by: API Gateway POST /orders
Does:
  1. Validates the request body (schema guard - prevents poison pills)
  2. Writes the order to DynamoDB orders table (source of truth)
  3. Publishes an OrderPlaced event to SNS (fan-out to all consumers)
  4. Returns 201 to the caller synchronously

IMPORTANT: The API response is 201 as soon as SNS accepts the message.
The caller does NOT wait for inventory, notification, or analytics to
complete. This is the core async contract - and the core failure mode
students must reason about in the Defend phase.
"""
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
sns_client = boto3.client("sns")

ORDERS_TABLE   = os.environ["ORDERS_TABLE"]
ORDER_TOPIC_ARN = os.environ["ORDER_TOPIC_ARN"]

orders_table = dynamodb.Table(ORDERS_TABLE)

# >>> WAITING ROOM (homework add-on) >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# Off by default: vanilla Week 5 behaves exactly as before. CDK sets
# WAITING_ROOM_ENABLED=true only when deployed with `-c waiting_room=on`.
# The gate lives in its own module so none of its logic is mixed into
# this file - see waiting_room_gate.py for what it does and why.
WAITING_ROOM_ENABLED = os.getenv("WAITING_ROOM_ENABLED", "false").lower() == "true"
if WAITING_ROOM_ENABLED:
    import waiting_room_gate
# <<< WAITING ROOM <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# -- Schema validation ------------------------------------------------------
REQUIRED_FIELDS = {"user_id", "items"}
REQUIRED_ITEM_FIELDS = {"product_id", "quantity"}


def _to_dynamodb_safe(obj):
    """
    Recursively convert Python float to Decimal.

    DynamoDB's boto3 resource layer rejects a raw Python float outright
    ("Float types are not supported. Use Decimal types instead.") -
    this is NOT a hypothetical edge case here: the request body's
    "total" field (and any per-item "price" a client might include) is
    exactly the kind of value a real client sends as a JSON number,
    e.g. {"total": 49.99}. json.loads parses that as a native float,
    and writing it straight into orders_table.put_item(Item=order)
    would 500 on literally the first realistic order.

    Only used for the DynamoDB write - the SNS/JSON payload downstream
    keeps whatever type the client actually sent, since json.dumps
    cannot serialize Decimal and consumers don't need DynamoDB's type
    restrictions.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, list):
        return [_to_dynamodb_safe(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_dynamodb_safe(v) for k, v in obj.items()}
    return obj


def validate_order(body: dict) -> list[str]:
    """Returns a list of validation errors. Empty list = valid."""
    errors = []
    missing = REQUIRED_FIELDS - set(body.keys())
    if missing:
        errors.append(f"Missing required fields: {missing}")
        return errors  # stop early - can't validate items if missing

    if not isinstance(body["items"], list) or len(body["items"]) == 0:
        errors.append("items must be a non-empty list")
    else:
        for i, item in enumerate(body["items"]):
            item_missing = REQUIRED_ITEM_FIELDS - set(item.keys())
            if item_missing:
                errors.append(f"Item {i}: missing {item_missing}")
            elif not isinstance(item["quantity"], int) or item["quantity"] < 1:
                errors.append(f"Item {i}: quantity must be a positive integer")

    if not isinstance(body.get("user_id"), str) or not body["user_id"].strip():
        errors.append("user_id must be a non-empty string")

    return errors


def lambda_handler(event, context):
    start = time.time()

    # API Gateway proxy integration wraps the body as a string
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Request body is not valid JSON"})

    # -- Validate -----------------------------------------------------------
    errors = validate_order(body)
    if errors:
        logger.warning("order_validation_failed", extra={"errors": errors})
        return _response(400, {"error": "Validation failed", "details": errors})

    # >>> WAITING ROOM GATE (homework add-on) >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
    # Placed AFTER validation so a malformed order cannot burn a visitor's
    # single-use admission, and BEFORE any expensive work: a visitor who
    # was never admitted is turned away here, before the DynamoDB write
    # and the SNS publish below. Rejecting early and cheaply is the
    # "load shedding" half of the Ticketmaster lesson.
    if WAITING_ROOM_ENABLED:
        rejection = waiting_room_gate.require_admission(event)
        if rejection is not None:
            return rejection
    # <<< WAITING ROOM GATE <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

    # -- Build order record -------------------------------------------------
    order_id  = str(uuid.uuid4())
    now_iso   = datetime.now(timezone.utc).isoformat()
    now_epoch = int(time.time())

    order = {
        "order_id":   order_id,
        "user_id":    body["user_id"],
        "items":      body["items"],
        "status":     "pending",
        "total":      body.get("total", 0),
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    # -- Write to DynamoDB (source of truth) -------------------------------
    try:
        orders_table.put_item(
            Item=_to_dynamodb_safe(order),
            ConditionExpression="attribute_not_exists(order_id)",  # idempotency guard
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Duplicate order_id (astronomically unlikely with UUID4 but correct to handle)
            logger.warning("duplicate_order_id", extra={"order_id": order_id})
            return _response(409, {"error": "Duplicate order", "order_id": order_id})
        logger.error("dynamodb_write_failed", extra={"error": str(e)})
        return _response(500, {"error": "Failed to persist order"})

    # -- Publish to SNS ----------------------------------------------------
    event_payload = {
        "event_type":  "OrderPlaced",
        "order_id":    order_id,
        "user_id":     body["user_id"],
        "items":       body["items"],
        "total":       body.get("total", 0),
        "occurred_at": now_iso,
    }
    try:
        sns_client.publish(
            TopicArn=ORDER_TOPIC_ARN,
            Message=json.dumps(event_payload),
            Subject="OrderPlaced",
            MessageAttributes={
                "event_type": {
                    "DataType": "String",
                    "StringValue": "OrderPlaced",
                }
            },
        )
    except ClientError as e:
        # DynamoDB write succeeded but SNS publish failed.
        # Order is persisted but consumers will never process it.
        # This is a real failure mode - students explore it in the Defend phase.
        logger.error("sns_publish_failed",
                     extra={"order_id": order_id, "error": str(e)})
        # Mark the order as publish_failed so it's visible in DynamoDB
        orders_table.update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "publish_failed"},
        )
        return _response(500, {"error": "Order saved but event publish failed",
                                "order_id": order_id})

    duration_ms = round((time.time() - start) * 1000, 1)
    logger.info("order_produced",
                extra={"order_id": order_id, "duration_ms": duration_ms})

    return _response(201, {
        "order_id":  order_id,
        "status":    "pending",
        "message":   "Order accepted. Processing is asynchronous.",
    })


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
