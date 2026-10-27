"""
JOIN  (POST /waiting-room/join)  -  Option A, DynamoDB time-slot counter. Design: DESIGN_SPEC.md section 4.

Every visitor gets a fixed admission slot when they join. The counter row holds next_slot_ms, the earliest free slot.
A join atomically takes that slot and pushes it forward by 1000 / ADMIT_PER_SECOND ms, so slots are spaced exactly
1/rate apart in join order: first come, first served, and paced, with no scheduler. If the line is empty
(next_slot_ms is in the past) the visitor's slot is "now".
"""
import json
import math
import os
import time
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import telemetry

COUNTERS_TABLE = os.environ["COUNTERS_TABLE"]
VISITORS_TABLE = os.environ["VISITORS_TABLE"]
ADMIT_PER_SECOND = float(os.environ.get("ADMIT_PER_SECOND", "10"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
INTERVAL_MS = max(1, round(1000 / ADMIT_PER_SECOND))
COUNTER_ID = "line"
MAX_ATTEMPTS = 5
VISITOR_TTL_SECONDS = 86400

_ddb = boto3.resource("dynamodb")
_counters = _ddb.Table(COUNTERS_TABLE)
_visitors = _ddb.Table(VISITORS_TABLE)


class LineUnavailable(Exception):
    pass


def _is_conditional_failure(e):
    return isinstance(e, ClientError) and e.response["Error"]["Code"] == "ConditionalCheckFailedException"


def take_slot(now_ms):
    """Atomically reserve the next admission slot. Returns (position, admit_at_ms)."""
    for _ in range(MAX_ATTEMPTS):
        try:
            # Busy line: the next free slot is now or later. Take it and move it one interval on.
            r = _counters.update_item(
                Key={"counter_id": COUNTER_ID},
                UpdateExpression="SET next_slot_ms = next_slot_ms + :i ADD next_position :one",
                ConditionExpression="next_slot_ms >= :now",
                ExpressionAttributeValues={":i": INTERVAL_MS, ":one": 1, ":now": now_ms},
                ReturnValues="UPDATED_NEW",
            )
            a = r["Attributes"]
            return int(a["next_position"]), int(a["next_slot_ms"]) - INTERVAL_MS
        except ClientError as e:
            if not _is_conditional_failure(e):
                raise
        try:
            # Idle line (or first visitor ever): the slot is now; the next one starts one interval later.
            r = _counters.update_item(
                Key={"counter_id": COUNTER_ID},
                UpdateExpression="SET next_slot_ms = :next ADD next_position :one",
                ConditionExpression="attribute_not_exists(next_slot_ms) OR next_slot_ms < :now",
                ExpressionAttributeValues={":next": now_ms + INTERVAL_MS, ":one": 1, ":now": now_ms},
                ReturnValues="UPDATED_NEW",
            )
            return int(r["Attributes"]["next_position"]), now_ms
        except ClientError as e:
            if not _is_conditional_failure(e):
                raise
            # Another join moved next_slot_ms between our two attempts: the line is busy again, retry.
    raise LineUnavailable("could not take a slot")


def lambda_handler(event, context):
    now_ms = int(time.time() * 1000)
    try:
        position, admit_at_ms = take_slot(now_ms)
        visitor_id = str(uuid.uuid4())
        _visitors.put_item(Item={
            "visitor_id": visitor_id,
            "position": position,
            "joined_at_ms": now_ms,
            "admit_at_ms": admit_at_ms,
            "ttl": now_ms // 1000 + VISITOR_TTL_SECONDS,
        })
    except (ClientError, BotoCoreError, LineUnavailable) as e:
        print(json.dumps({"event": "join_failed", "error": str(e)}))
        return _response(503, {"error": "waiting_room_unavailable", "message": "Please try again."})

    telemetry.joined()
    wait_s = max(0.0, (admit_at_ms - now_ms) / 1000)
    return _response(202, {
        "visitor_id": visitor_id,
        "status": "waiting",
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "poll_path": f"/waiting-room/status/{visitor_id}",
        "position": position,
        "estimated_wait_seconds": math.ceil(wait_s),
    })


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
