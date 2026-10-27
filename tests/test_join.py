"""JOIN: a place in line and a paced admission slot (DESIGN_SPEC.md section 4)."""
import uuid

import pytest
from botocore.exceptions import ClientError

from conftest import load_module
from wr_helpers import body, metric_lines


def slot_of(aws, visitor_id):
    return int(aws.visitors.get_item(Key={"visitor_id": visitor_id})["Item"]["admit_at_ms"])


def test_join_answers_the_contract(aws, clock):
    join = load_module("waiting_room/join_handler.py")
    r = join.lambda_handler({}, None)
    b = body(r)
    assert r["statusCode"] == 202
    assert str(uuid.UUID(b["visitor_id"])) == b["visitor_id"]
    assert b["status"] == "waiting"
    assert b["poll_interval_seconds"] == 10
    assert b["poll_path"] == f"/waiting-room/status/{b['visitor_id']}"


def test_positions_count_up_and_slots_are_one_interval_apart(aws, clock):
    join = load_module("waiting_room/join_handler.py")
    ids = [body(join.lambda_handler({}, None))["visitor_id"] for _ in range(4)]
    rows = [aws.visitors.get_item(Key={"visitor_id": i})["Item"] for i in ids]
    assert [int(r["position"]) for r in rows] == [1, 2, 3, 4]
    start = int(clock.now * 1000)
    # 10 per second -> 100 ms apart, the first one admitted now (idle line)
    assert [int(r["admit_at_ms"]) for r in rows] == [start, start + 100, start + 200, start + 300]


def test_estimated_wait_grows_with_the_line(aws, clock):
    join = load_module("waiting_room/join_handler.py")
    waits = [body(join.lambda_handler({}, None))["estimated_wait_seconds"] for _ in range(25)]
    assert waits[0] == 0 and waits[-1] == 3          # 24 people ahead at 10/s = 2.4 s, rounded up
    assert waits == sorted(waits)


def test_an_idle_line_does_not_bank_slots(aws, clock):
    """After a quiet minute the next visitor is admitted NOW, not 600 visitors' worth of slots later or earlier."""
    join = load_module("waiting_room/join_handler.py")
    join.lambda_handler({}, None)
    clock.advance(60)
    v = body(join.lambda_handler({}, None))["visitor_id"]
    assert slot_of(aws, v) == int(clock.now * 1000)
    w = body(join.lambda_handler({}, None))["visitor_id"]
    assert slot_of(aws, w) == int(clock.now * 1000) + 100


def test_the_rate_comes_from_the_environment(aws, clock, monkeypatch):
    monkeypatch.setenv("ADMIT_PER_SECOND", "4")
    join = load_module("waiting_room/join_handler.py")
    a, b = (body(join.lambda_handler({}, None))["visitor_id"] for _ in range(2))
    assert slot_of(aws, b) - slot_of(aws, a) == 250


def test_joined_is_counted_once_per_visitor(aws, clock, capsys):
    join = load_module("waiting_room/join_handler.py")
    for _ in range(3):
        join.lambda_handler({}, None)
    out = capsys.readouterr().out
    assert metric_lines(out, "Joined") == 3
    assert metric_lines(out, "Admitted") == 0


def test_a_storage_failure_is_a_clean_503_and_counts_nobody(aws, clock, capsys, monkeypatch):
    join = load_module("waiting_room/join_handler.py")

    def broken(**kwargs):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "UpdateItem")

    monkeypatch.setattr(join._counters, "update_item", broken)
    r = join.lambda_handler({}, None)
    assert r["statusCode"] == 503
    assert body(r)["error"] == "waiting_room_unavailable"
    assert metric_lines(capsys.readouterr().out, "Joined") == 0
    assert aws.visitors.scan()["Count"] == 0
