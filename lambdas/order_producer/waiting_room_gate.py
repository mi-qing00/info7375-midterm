"""
waiting_room_gate.py - the ONLY waiting-room code inside OrderProducer
======================================================================
HOMEWORK ADD-ON. Vanilla Week 5 does not contain or import this file's
logic: handler.py imports it only when WAITING_ROOM_ENABLED=true (set by
CDK when you deploy with `-c waiting_room=on`).

WHAT THE GATE DOES
  A visitor who has been admitted by the waiting room holds an ADMISSION
  RECORD in DynamoDB:

      visitor_id (PK) | admission_token | used | expires_at | ttl

  To place an order the client sends two headers:

      X-Visitor-Id:       <visitor_id from POST /waiting-room/join>
      X-Admission-Token:  <token from GET /waiting-room/status/{id}>

  require_admission() verifies AND consumes that record in ONE atomic
  DynamoDB conditional update. If the update's condition fails, the
  request is turned away with a 403 before OrderProducer does any real
  work (no order write, no SNS publish).

THIS FILE IS THE SAME FOR EVERY WAITING-ROOM OPTION (A, B and C)
  The options differ in HOW a visitor becomes admitted (a DynamoDB
  counter, an SQS queue, a Redis counter). They all end by writing the
  same admission record, so the gate - the part that protects the
  backend - never changes. That separation is deliberate: the gate is
  the contract, the waiting room is the implementation.

DESIGN CHOICES WORTH DISCUSSING
  * Single use. One admission buys exactly one order. If a token could
    be reused, one admitted bot could place unlimited orders and the
    waiting room would only have rate-limited the first one.
  * Atomic verify+consume. Two concurrent requests with the same token
    cannot both succeed: the conditional update lets exactly one win.
  * Explicit expiry check. DynamoDB TTL is garbage collection, NOT
    enforcement - expired items can linger for up to ~48 hours. The
    gate therefore compares expires_at itself.
  * Fail closed. If DynamoDB is unreachable we return 503 rather than
    letting the order through. A gate that opens when it cannot check
    is not a gate.
"""
import json
import logging
import os
import time
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()

_table = boto3.resource("dynamodb").Table(os.environ["WAITING_ROOM_TABLE"])

VISITOR_HEADER = "x-visitor-id"
TOKEN_HEADER = "x-admission-token"


def require_admission(event: dict) -> Optional[dict]:
    """
    Returns None if the caller holds a valid, unused admission (which is
    consumed as a side effect), otherwise a ready-to-return API Gateway
    response (403 or 503).
    """
    # API Gateway REST APIs pass header names through with whatever case
    # the client used, so compare case-insensitively.
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    visitor_id = headers.get(VISITOR_HEADER)
    token = headers.get(TOKEN_HEADER)

    if not visitor_id or not token:
        logger.info("gate_rejected_no_credentials")
        return _reject(403, "admission_required",
                       "Join the waiting room first: POST /waiting-room/join")

    now = int(time.time())
    try:
        _table.update_item(
            Key={"visitor_id": visitor_id},
            UpdateExpression="SET #used = :true, used_at = :now",
            # All four conditions must hold, evaluated atomically:
            #   - the visitor was actually admitted (record exists)
            #   - the token matches
            #   - the admission has not already been spent
            #   - the admission has not expired
            ConditionExpression=(
                "attribute_exists(visitor_id) AND #tok = :tok "
                "AND #used = :false AND #exp > :now"
            ),
            ExpressionAttributeNames={
                "#used": "used",
                "#tok": "admission_token",
                "#exp": "expires_at",
            },
            ExpressionAttributeValues={
                ":true": True,
                ":false": False,
                ":tok": token,
                ":now": now,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("gate_rejected_invalid_admission",
                        extra={"visitor_id": visitor_id})
            return _reject(403, "invalid_admission",
                           "Admission is unknown, already used, or expired. "
                           "Re-join the waiting room.")
        logger.error("gate_check_failed", extra={"error": str(e)})
        return _reject(503, "admission_check_unavailable",
                       "Could not verify admission; failing closed.")
    except BotoCoreError as e:
        logger.error("gate_check_failed", extra={"error": str(e)})
        return _reject(503, "admission_check_unavailable",
                       "Could not verify admission; failing closed.")

    logger.info("gate_admitted", extra={"visitor_id": visitor_id})
    return None


def _reject(status_code: int, error: str, message: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": error, "message": message}),
    }
