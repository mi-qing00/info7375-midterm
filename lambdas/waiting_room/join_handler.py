"""
JOIN  (POST /waiting-room/join)  -  YOURS TO WRITE
==================================================
Answer exactly as CONTRACT.md section 4 says:

    202  {"visitor_id": "<uuid>", "status": "waiting", "poll_interval_seconds": <n>, "poll_path": "..."}

Environment variables the skeleton sets (add your own in cdk/waiting_room.py):
    ADMISSIONS_TABLE, ADMIT_PER_SECOND, POLL_INTERVAL_SECONDS, ADMISSION_VALID_SECONDS

When a visitor gets a place in line, count them with the provided helper (one line, once per visitor):

    import telemetry
    telemetry.joined()
"""
import json
import os  # noqa: F401

import telemetry  # noqa: F401  (provided: telemetry.joined(), .admitted(), .time_in_line(seconds))


def lambda_handler(event, context):
    # TODO: write this.
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "not_implemented", "message": "join is not written yet"}),
    }
