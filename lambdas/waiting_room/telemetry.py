"""
telemetry.py - three standard waiting-room metrics, one line each.   (PROVIDED: do not modify)

    import telemetry
    telemetry.joined()                   # once per visitor, when they get a place in line
    telemetry.admitted()                 # once per visitor, the moment their admission is written
    telemetry.time_in_line(seconds)      # once per visitor, at admission: seconds since they joined

The common dashboard draws these as "Visitors joined and admitted", "People waiting now" and "Time in line".

HOW IT WORKS
  Each call prints ONE line of JSON in CloudWatch's Embedded Metric Format. In Lambda, printed lines go to the
  function's log group, and CloudWatch Logs extracts the metric from the line by itself: no extra permission, no
  network call, no change to your function's role. Metrics appear in the namespace "Info7375/WaitingRoom" about a
  minute after the call. Your own code can call emit() for anything else you want to chart.

  A telemetry call never raises: a problem with a metric must not break a visitor's request. A call that is given
  a bad value returns False and prints nothing.
"""
import json
import math
import sys
import time

NAMESPACE = "Info7375/WaitingRoom"
VALID_UNITS = {"Count", "Seconds", "Milliseconds", "Percent", "None"}


def emit(name, value=1, unit="Count", *, high_resolution=False, now=None, stream=None):
    """Print one metric as an Embedded Metric Format line. Returns True if a line was printed."""
    try:
        if not isinstance(name, str) or not name:
            return False
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if math.isnan(value) or math.isinf(value) or value < 0:
            return False
        if unit not in VALID_UNITS:
            return False
        definition = {"Name": name, "Unit": unit}
        if high_resolution:
            definition["StorageResolution"] = 1
        document = {
            "_aws": {
                "Timestamp": int((time.time() if now is None else now) * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": NAMESPACE,
                    "Dimensions": [[]],            # no dimensions: one metric per name
                    "Metrics": [definition],
                }],
            },
            name: value,
        }
        print(json.dumps(document), file=stream or sys.stdout, flush=True)
        return True
    except Exception:       # a metric must never break the request that produced it
        return False


def joined():
    return emit("Joined", 1, "Count")


def admitted():
    return emit("Admitted", 1, "Count")


def time_in_line(seconds):
    return emit("TimeInLineSeconds", seconds, "Seconds")
