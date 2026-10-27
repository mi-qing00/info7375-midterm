"""Things that happen at the same time (RUBRIC.md: Tests). Two kinds:
  * threaded: many real calls at once, checking the invariants that must survive any interleaving;
  * forced interleavings: we inject the competing write at the exact worst moment, so the race is hit EVERY run.

WHY THE LOCK. Real DynamoDB applies each single-item write (including its condition) atomically; our design relies
on exactly that and nothing more. moto does NOT: two threads can both pass the same condition inside one mocked call.
`atomic()` restores DynamoDB's documented per-request atomicity by letting one mocked call run at a time. Threads
still interleave freely BETWEEN calls, which is where a bug in our logic (read-then-write, check-then-act) would show.
"""
import threading

from conftest import load_module
from wr_helpers import body, load_gate, metric_lines, order_event, status_event


_one_request_at_a_time = threading.Lock()


def atomic(table, monkeypatch, *operations):
    for op in operations:
        real = getattr(table, op)

        def wrapped(*args, _real=real, **kwargs):
            with _one_request_at_a_time:
                return _real(*args, **kwargs)

        monkeypatch.setattr(table, op, wrapped)


def run_together(fn, n):
    results, errors = [], []
    start = threading.Barrier(n)

    def worker():
        try:
            start.wait()
            results.append(fn())
        except Exception as e:          # surface thread failures in the test
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def test_simultaneous_joins_get_unique_positions_and_unique_paced_slots(aws, clock, monkeypatch):
    join = load_module("waiting_room/join_handler.py")
    atomic(join._counters, monkeypatch, "update_item")
    ids = [body(r)["visitor_id"] for r in run_together(lambda: join.lambda_handler({}, None), 40)]
    rows = [aws.visitors.get_item(Key={"visitor_id": i})["Item"] for i in ids]
    positions = sorted(int(r["position"]) for r in rows)
    assert positions == list(range(1, 41))                       # nobody shares a place, nobody is skipped
    by_position = sorted(rows, key=lambda r: int(r["position"]))
    slots = [int(r["admit_at_ms"]) for r in by_position]
    assert slots == sorted(slots)                                # first come, first served
    assert all(b - a == 100 for a, b in zip(slots, slots[1:]))   # exactly one slot per 100 ms
    counter = aws.counters.get_item(Key={"counter_id": "line"})["Item"]
    assert int(counter["next_position"]) == 40
    assert int(counter["next_slot_ms"]) == slots[-1] + 100


def test_a_join_that_loses_the_idle_race_retries_as_busy(aws, clock, monkeypatch):
    """Visitor X finds the line idle, but visitor Y takes the 'now' slot between X's two attempts.
    X must NOT also get 'now' (two admissions in the same slot); X retries and gets the next slot."""
    join = load_module("waiting_room/join_handler.py")
    other = load_module("waiting_room/join_handler.py")
    real_update = join._counters.update_item
    calls = {"n": 0}

    def update_with_competitor(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:                    # X failed 'busy'; just before X's 'idle' attempt, Y joins
            other.lambda_handler({}, None)
        return real_update(**kwargs)

    monkeypatch.setattr(join._counters, "update_item", update_with_competitor)
    x = body(join.lambda_handler({}, None))["visitor_id"]
    rows = sorted(aws.visitors.scan()["Items"], key=lambda r: int(r["position"]))
    assert [r["visitor_id"] for r in rows][1] == x
    now_ms = int(clock.now * 1000)
    assert [int(r["admit_at_ms"]) for r in rows] == [now_ms, now_ms + 100]


def test_simultaneous_polls_create_one_admission_one_token_one_count(aws, clock, capsys, monkeypatch):
    join = load_module("waiting_room/join_handler.py")
    status = load_module("waiting_room/status_handler.py")
    atomic(status._admissions, monkeypatch, "put_item", "get_item")
    v = body(join.lambda_handler({}, None))["visitor_id"]
    answers = [body(r) for r in run_together(lambda: status.lambda_handler(status_event(v), None), 15)]
    tokens = {a["admission_token"] for a in answers}
    assert len(tokens) == 1
    assert aws.table.get_item(Key={"visitor_id": v})["Item"]["admission_token"] in tokens
    assert metric_lines(capsys.readouterr().out, "Admitted") == 1


def test_the_poll_that_loses_the_admission_race_hands_out_the_winners_token(aws, clock, capsys, monkeypatch):
    """Forced interleaving: both polls read 'no record yet'; the other poll writes first. Ours must return the
    winner's token (not overwrite it with a second one) and must not count a second admission."""
    join = load_module("waiting_room/join_handler.py")
    status = load_module("waiting_room/status_handler.py")
    v = body(join.lambda_handler({}, None))["visitor_id"]
    real_put = status._admissions.put_item

    def put_after_competitor(**kwargs):
        real_put(Item={"visitor_id": v, "admission_token": "winner-token", "used": False,
                       "expires_at": int(clock.now) + 600, "ttl": int(clock.now) + 4200})
        return real_put(**kwargs)

    monkeypatch.setattr(status._admissions, "put_item", put_after_competitor)
    b = body(status.lambda_handler(status_event(v), None))
    assert b["admission_token"] == "winner-token"
    assert aws.table.get_item(Key={"visitor_id": v})["Item"]["admission_token"] == "winner-token"
    assert metric_lines(capsys.readouterr().out, "Admitted") == 0


def test_two_orders_racing_on_one_admission_only_one_wins(aws, clock, monkeypatch):
    """Our record must work with the provided gate's atomic consume under concurrency."""
    join = load_module("waiting_room/join_handler.py")
    status = load_module("waiting_room/status_handler.py")
    gate = load_gate(monkeypatch)
    atomic(gate._table, monkeypatch, "update_item")
    v = body(join.lambda_handler({}, None))["visitor_id"]
    token = body(status.lambda_handler(status_event(v), None))["admission_token"]
    results = run_together(lambda: gate.require_admission(order_event(v, token)), 8)
    assert sum(r is None for r in results) == 1
    assert all(r["statusCode"] == 403 for r in results if r is not None)
