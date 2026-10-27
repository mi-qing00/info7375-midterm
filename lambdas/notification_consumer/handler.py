"""
NotificationConsumer Lambda
===========================
Triggered by: SQS notification-queue (batch_size=1 - emails must not duplicate)
Does:
  1. Extracts the OrderPlaced event
  2. Checks sent_notifications table - skip if already sent
  3. Writes idempotency record to DynamoDB BEFORE sending email
  4. Sends order confirmation via SES (or logs if stub mode)
  5. Returns batch item failures on error

WHY batch_size=1:
  Sending duplicate order confirmation emails is the worst user experience
  in e-commerce after a wrong charge. batch_size=1 means each Lambda
  invocation handles exactly one email. Combined with the idempotency
  check, this guarantees at-most-once delivery to the user even though
  SQS gives us at-least-once.

WHY write idempotency record BEFORE sending:
  If we send the email first and then crash before writing the record,
  SQS redelivers, we check - no record - and we send again.
  Writing first means: if we crash after the write but before the send,
  SQS redelivers, we find the record, and we skip. The user gets no email
  in this case (silent failure), but that is better than two emails.
  This is the write-before-act idempotency pattern - a key trade-off
  students must understand and defend.

SES STUB MODE:
  If FROM_EMAIL=stub@example.com, the handler logs the email content
  instead of calling SES. Students see the full flow without needing
  a verified SES domain.
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

dynamodb   = boto3.resource("dynamodb")
ses_client = boto3.client("ses")

SENT_NOTIFICATIONS_TABLE = os.environ["SENT_NOTIFICATIONS_TABLE"]
FROM_EMAIL               = os.environ.get("FROM_EMAIL", "stub@example.com")
TTL_SECONDS              = 7 * 24 * 3600
STUB_MODE                = FROM_EMAIL == "stub@example.com"

sent_table = dynamodb.Table(SENT_NOTIFICATIONS_TABLE)


def lambda_handler(event, context):
    batch_item_failures = []
    for record in event["Records"]:
        message_id = record["messageId"]
        try:
            _process_record(record)
        except Exception as exc:
            logger.error("notification_failed",
                         extra={"message_id": message_id, "error": str(exc)})
            batch_item_failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": batch_item_failures}


def _process_record(record: dict):
    body    = json.loads(record["body"])
    message = json.loads(body["Message"])

    order_id = message["order_id"]
    user_id  = message.get("user_id", "unknown")
    items    = message.get("items", [])
    total    = message.get("total", 0)

    # -- Idempotency check --------------------------------------------------
    try:
        existing = sent_table.get_item(Key={"order_id": order_id})
        if "Item" in existing:
            logger.info("notification_already_sent",
                        extra={"order_id": order_id,
                               "sent_at": existing["Item"].get("sent_at")})
            return
    except ClientError as e:
        logger.error("idempotency_check_failed",
                     extra={"order_id": order_id, "error": str(e)})
        raise

    # -- Write idempotency record BEFORE sending ----------------------------
    # Pattern: write-before-act. Prevents duplicate sends on Lambda crash.
    # Trade-off: a crash between write and send means no email (silent miss),
    # but that is preferable to duplicate emails.
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        sent_table.put_item(
            Item={
                "order_id": order_id,
                "sent_at":  now_iso,
                "user_id":  user_id,
                "ttl":      int(time.time()) + TTL_SECONDS,
            },
            # Extra guard: if somehow two Lambdas race on the same order_id,
            # only one succeeds - the other gets ConditionalCheckFailed and skips
            ConditionExpression="attribute_not_exists(order_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("notification_race_condition_prevented",
                        extra={"order_id": order_id})
            return   # another Lambda already claimed this order
        raise

    # -- Send email ---------------------------------------------------------
    subject = f"Order Confirmed - #{order_id[:8].upper()}"
    # total's actual type isn't guaranteed by anything upstream - JSON
    # doesn't distinguish "347.99" from 347.99, and load-test.yml's
    # Artillery template ("{{ $randomInt(10,500) }}.99") produces a
    # quoted STRING, not a number. An f-string :.2f format spec on a
    # string raises TypeError, which would otherwise crash every
    # notification for load-test-generated traffic - not a deliberate
    # Break scenario, just realistic input this handler must tolerate.
    try:
        total_display = f"{float(total):.2f}"
    except (TypeError, ValueError):
        total_display = str(total)
    body_text = (
        f"Thank you for your order!\n\n"
        f"Order ID: {order_id}\n"
        f"Items: {len(items)}\n"
        f"Total: ${total_display}\n\n"
        f"Your order is being processed."
    )

    if STUB_MODE:
        # Lab mode: log instead of sending - no SES domain needed
        logger.info("email_stub_sent",
                    extra={"order_id": order_id, "user_id": user_id,
                           "subject": subject, "mode": "stub"})
    else:
        # Real SES send - requires verified FROM_EMAIL in SES
        # and recipient email to be verified (if SES is in sandbox mode)
        try:
            ses_client.send_email(
                Source=FROM_EMAIL,
                Destination={"ToAddresses": [f"{user_id}@example.com"]},
                Message={
                    "Subject": {"Data": subject},
                    "Body": {"Text": {"Data": body_text}},
                },
            )
            logger.info("email_sent",
                        extra={"order_id": order_id, "user_id": user_id})
        except ClientError as e:
            # SES failure - message stays in SQS for retry
            # But idempotency record is already written, so the retry
            # will find it and skip. This means: one SES failure = permanent miss.
            # In production you'd delete the idempotency record on SES failure.
            # Leave this as a discussion point for the Defend phase.
            logger.error("ses_send_failed",
                         extra={"order_id": order_id, "error": str(e)})
            raise
