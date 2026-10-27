"""
STATUS  (GET /waiting-room/status/{visitor_id})  -  Option A. Design: DESIGN_SPEC.md section 5.

A visitor is admitted once now >= their admit_at_ms (fixed at join). The first poll after that writes the admission
record the provided gate reads. The write is conditional (attribute_not_exists), so two simultaneous polls create
exactly one record: the loser reads the winner's token back. Telemetry fires only in the poll that won.
"""
import json
import math
import os
import secrets
import time
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import telemetry

VISITORS_TABLE = os.environ["VISITORS_TABLE"]
ADMISSIONS_TABLE = os.environ["ADMISSIONS_TABLE"]
ADMIT_PER_SECOND = float(os.environ.get("ADMIT_PER_SECOND", "10"))
ADMISSION_VALID_SECONDS = int(os.environ.get("ADMISSION_VALID_SECONDS", "600"))
INTERVAL_MS = max(1, round(1000 / ADMIT_PER_SECOND))
RECORD_GRACE_SECONDS = 3600

_ddb = boto3.resource("dynamodb")
_visitors = _ddb.Table(VISITORS_TABLE)
_admissions = _ddb.Table(ADMISSIONS_TABLE)


def _valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _admitted_view(record, now_s):
    if record.get("used"):
        return {"status": "admitted", "order_placed": True}
    if int(record["expires_at"]) <= now_s:
        return {"status": "expired", "message": "Your admission expired. Join the line again."}
    return {"status": "admitted", "admission_token": record["admission_token"],
            "expires_at": int(record["expires_at"])}


def _admit(visitor_id, visitor, now_ms):
    """Return the visitor's admission view, creating the admission record exactly once."""
    now_s = now_ms // 1000
    existing = _admissions.get_item(Key={"visitor_id": visitor_id}, ConsistentRead=True).get("Item")
    if existing:
        return _admitted_view(existing, now_s)

    expires_at = now_s + ADMISSION_VALID_SECONDS
    record = {
        "visitor_id": visitor_id,
        "admission_token": secrets.token_urlsafe(32),
        "used": False,
        "expires_at": expires_at,
        "ttl": expires_at + RECORD_GRACE_SECONDS,
        "admitted_at": now_s,
    }
    try:
        _admissions.put_item(Item=record, ConditionExpression="attribute_not_exists(visitor_id)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # A simultaneous poll wrote the record first: hand out ITS token, count nothing.
        winner = _admissions.get_item(Key={"visitor_id": visitor_id}, ConsistentRead=True)["Item"]
        return _admitted_view(winner, now_s)

    telemetry.admitted()
    telemetry.time_in_line((now_ms - int(visitor["joined_at_ms"])) / 1000)
    return _admitted_view(record, now_s)


def lambda_handler(event, context):
    visitor_id = ((event or {}).get("pathParameters") or {}).get("visitor_id")
    if not _valid_uuid(visitor_id):
        return _response(400, {"error": "malformed_visitor_id"})

    now_ms = int(time.time() * 1000)
    try:
        visitor = _visitors.get_item(Key={"visitor_id": visitor_id}).get("Item")
        if not visitor:
            return _response(404, {"error": "unknown_visitor"})
        admit_at_ms = int(visitor["admit_at_ms"])
        if now_ms < admit_at_ms:
            wait_ms = admit_at_ms - now_ms
            return _response(200, {
                "status": "waiting",
                "position": int(visitor["position"]),
                "ahead": math.ceil(wait_ms / INTERVAL_MS),
                "estimated_wait_seconds": math.ceil(wait_ms / 1000),
            })
        return _response(200, _admit(visitor_id, visitor, now_ms))
    except (ClientError, BotoCoreError, KeyError) as e:
        print(json.dumps({"event": "status_failed", "visitor_id": visitor_id, "error": str(e)}))
        return _response(503, {"error": "waiting_room_unavailable", "message": "Please poll again."})


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
