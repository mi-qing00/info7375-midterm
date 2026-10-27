"""
AnalyticsConsumer Lambda
========================
Triggered by: SQS analytics-queue (batch_size=25 - analytics tolerates duplicates)
Does:
  1. Extracts OrderPlaced events from the batch
  2. Writes all events to Kinesis Data Stream in a single PutRecords call
  3. Handles partial Kinesis failures - only re-queues failed records

CONTRAST WITH NOTIFICATION CONSUMER:
  - batch_size=25 (vs 1 for notifications)
  - No idempotency check (duplicates are tolerable in analytics)
  - Kinesis PutRecords is used for throughput (one API call per batch vs one per record)

WHY duplicates are tolerable here:
  Analytics data is aggregated (counts, averages, totals). A duplicate
  order event inflates a count by 1 in a dataset of millions.
  That is a rounding error, not a business-critical failure.
  This is the architectural decision students must articulate in the
  Defend phase: different consumers can have different delivery guarantees
  on the SAME event from the SAME topic.

KINESIS PARTITION KEY:
  We use user_id as the partition key. This means all events for the
  same user go to the same shard in sequence order. Downstream consumers
  can reconstruct per-user event timelines without sorting.
"""
import base64
import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

kinesis_client   = boto3.client("kinesis")
ANALYTICS_STREAM = os.environ["ANALYTICS_STREAM"]


def lambda_handler(event, context):
    batch_item_failures = []
    kinesis_records     = []
    message_id_map      = {}  # kinesis record index -> SQS message_id

    # -- Build Kinesis records from SQS batch -------------------------------
    for i, record in enumerate(event["Records"]):
        message_id = record["messageId"]
        try:
            body    = json.loads(record["body"])
            message = json.loads(body["Message"])

            analytics_record = {
                "event_type":   message.get("event_type", "OrderPlaced"),
                "order_id":     message["order_id"],
                "user_id":      message.get("user_id", "unknown"),
                "items_count":  len(message.get("items", [])),
                "total":        message.get("total", 0),
                "occurred_at":  message.get("occurred_at",
                                            datetime.now(timezone.utc).isoformat()),
                "ingested_at":  datetime.now(timezone.utc).isoformat(),
            }

            kinesis_records.append({
                "Data":         json.dumps(analytics_record).encode(),
                "PartitionKey": message.get("user_id", message["order_id"]),
            })
            message_id_map[len(kinesis_records) - 1] = message_id

        except (KeyError, json.JSONDecodeError) as e:
            logger.warning("record_parse_failed",
                           extra={"message_id": message_id, "error": str(e)})
            # Malformed analytics record - send to DLQ rather than blocking
            batch_item_failures.append({"itemIdentifier": message_id})

    if not kinesis_records:
        return {"batchItemFailures": batch_item_failures}

    # -- Batch write to Kinesis ---------------------------------------------
    # PutRecords: one API call for up to 500 records.
    # Returns a per-record success/failure list.
    try:
        response = kinesis_client.put_records(
            StreamName=ANALYTICS_STREAM,
            Records=kinesis_records,
        )

        failed_count = response.get("FailedRecordCount", 0)
        if failed_count > 0:
            # Some records failed - identify which SQS messages to retry
            for i, result in enumerate(response["Records"]):
                if "ErrorCode" in result:
                    sqs_message_id = message_id_map.get(i)
                    if sqs_message_id:
                        logger.warning("kinesis_record_failed",
                                       extra={"index": i,
                                              "error_code": result["ErrorCode"],
                                              "message_id": sqs_message_id})
                        batch_item_failures.append({"itemIdentifier": sqs_message_id})

        logger.info("analytics_batch_written",
                    extra={"total": len(kinesis_records),
                           "failed": failed_count,
                           "stream": ANALYTICS_STREAM})

    except ClientError as e:
        # Full batch write failed - retry all messages
        logger.error("kinesis_put_records_failed", extra={"error": str(e)})
        for message_id in message_id_map.values():
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
