"""Small helpers shared by the waiting-room tests (DESIGN_SPEC.md sections 4 and 5)."""
import json
import os

from conftest import load_module


def body(response):
    return json.loads(response["body"])


def status_event(visitor_id):
    return {"pathParameters": {"visitor_id": visitor_id}}


def metric_lines(text, name):
    """How many Embedded Metric Format lines for `name` telemetry printed."""
    return sum(1 for line in text.splitlines() if line.startswith("{") and f'"{name}":' in line)


def load_gate(monkeypatch):
    """The PROVIDED gate (not ours), pointed at the test admissions table."""
    monkeypatch.setenv("WAITING_ROOM_TABLE", os.environ["ADMISSIONS_TABLE"])
    return load_module("order_producer/waiting_room_gate.py")


def order_event(visitor_id, token):
    return {"headers": {"X-Visitor-Id": visitor_id, "X-Admission-Token": token}}
