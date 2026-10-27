"""
STATUS  (GET /waiting-room/status/{visitor_id})  -  YOURS TO WRITE
===================================================================
Answer exactly as CONTRACT.md section 4 says: waiting, admitted (with the token), admitted after the order was
placed (no token), or 400 for a malformed visitor_id.

When you admit a visitor you must write their admission record (CONTRACT.md section 4). Count it once, at that
moment (not on every poll), with the provided helper:

    import telemetry
    telemetry.admitted()
    telemetry.time_in_line(seconds_since_they_joined)
"""
import json
import os  # noqa: F401

import telemetry  # noqa: F401


def lambda_handler(event, context):
    # TODO: write this.
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "not_implemented", "message": "status is not written yet"}),
    }
