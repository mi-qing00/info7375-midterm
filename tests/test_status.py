"""STATUS, ADMISSION and the single-use rule (DESIGN_SPEC.md section 5, CONTRACT.md section 4)."""
import uuid

import pytest
from botocore.exceptions import ClientError

from conftest import load_module
from wr_helpers import body, load_gate, metric_lines, order_event, status_event


@pytest.fixture
def wr(aws, clock):
    join = load_module("waiting_room/join_handler.py")
    status = load_module("waiting_room/status_handler.py")

    def new_visitor():
        return body(join.lambda_handler({}, None))["visitor_id"]

    def poll(visitor_id):
        r = status.lambda_handler(status_event(visitor_id), None)
        return r["statusCode"], body(r)

    return status, new_visitor, poll


@pytest.mark.parametrize("bad", ["not-a-uuid", "", "1234", str(uuid.uuid4()).upper(), "../" + str(uuid.uuid4())])
def test_a_malformed_visitor_id_is_400(wr, bad):
    _, _, poll = wr
    assert poll(bad)[0] == 400


def test_a_missing_path_parameter_is_400(wr):
    status, _, _ = wr
    assert status.lambda_handler({}, None)["statusCode"] == 400


def test_an_unknown_visitor_is_404(wr):
    _, _, poll = wr
    assert poll(str(uuid.uuid4())) == (404, {"error": "unknown_visitor"})


def test_waiting_until_the_slot_then_admitted(wr, aws, clock):
    _, new_visitor, poll = wr
    ids = [new_visitor() for _ in range(11)]           # the 11th slot is 1.0 s away
    code, b = poll(ids[10])
    assert code == 200 and b["status"] == "waiting"
    assert b["position"] == 11 and b["ahead"] == 10 and b["estimated_wait_seconds"] == 1
    assert "admission_token" not in b
    assert aws.table.get_item(Key={"visitor_id": ids[10]}).get("Item") is None   # joined only: no record
    clock.advance(1.0)
    code, b = poll(ids[10])
    assert code == 200 and b["status"] == "admitted" and b["admission_token"]


def test_the_admission_record_is_what_the_gate_needs(wr, aws, clock):
    _, new_visitor, poll = wr
    v = new_visitor()
    _, b = poll(v)
    rec = aws.table.get_item(Key={"visitor_id": v})["Item"]
    now = int(clock.now)
    assert rec["admission_token"] == b["admission_token"]
    assert rec["used"] is False
    assert int(rec["expires_at"]) == now + 600 == b["expires_at"]
    assert int(rec["ttl"]) > int(rec["expires_at"])


def test_asking_again_returns_the_same_token_and_counts_once(wr, clock, capsys):
    _, new_visitor, poll = wr
    v = new_visitor()
    clock.advance(12.5)
    tokens = {poll(v)[1]["admission_token"] for _ in range(5)}
    assert len(tokens) == 1
    out = capsys.readouterr().out
    assert metric_lines(out, "Admitted") == 1
    assert metric_lines(out, "TimeInLineSeconds") == 1
    assert '"TimeInLineSeconds": 12.5' in out


def test_tokens_are_long_and_differ_between_visitors(wr, clock):
    _, new_visitor, poll = wr
    a, b = new_visitor(), new_visitor()
    clock.advance(1)
    tok_a, tok_b = poll(a)[1]["admission_token"], poll(b)[1]["admission_token"]
    assert tok_a != tok_b and len(tok_a) >= 32


def test_single_use_one_admission_buys_one_order(wr, monkeypatch):
    _, new_visitor, poll = wr
    gate = load_gate(monkeypatch)
    v = new_visitor()
    token = poll(v)[1]["admission_token"]
    assert gate.require_admission(order_event(v, token)) is None          # the order goes through
    assert gate.require_admission(order_event(v, token))["statusCode"] == 403   # the replay does not
    code, b = poll(v)
    assert (code, b) == (200, {"status": "admitted", "order_placed": True})    # token no longer shown


def test_a_visitor_who_only_joined_cannot_order(wr, monkeypatch):
    _, new_visitor, poll = wr
    gate = load_gate(monkeypatch)
    ids = [new_visitor() for _ in range(5)]
    late = ids[-1]                                     # still waiting: no admission record yet
    assert poll(late)[1]["status"] == "waiting"
    assert gate.require_admission(order_event(late, "guessed-token"))["statusCode"] == 403


def test_another_visitors_token_does_not_work(wr, clock, monkeypatch):
    _, new_visitor, poll = wr
    gate = load_gate(monkeypatch)
    a, b = new_visitor(), new_visitor()
    clock.advance(1)
    tok_a = poll(a)[1]["admission_token"]
    poll(b)
    assert gate.require_admission(order_event(b, tok_a))["statusCode"] == 403


def test_an_admission_expires(wr, clock, monkeypatch):
    _, new_visitor, poll = wr
    gate = load_gate(monkeypatch)
    v = new_visitor()
    token = poll(v)[1]["admission_token"]
    clock.advance(601)
    assert poll(v)[1]["status"] == "expired"
    assert gate.require_admission(order_event(v, token))["statusCode"] == 403


def test_a_storage_failure_is_a_clean_503(wr, monkeypatch):
    status, new_visitor, poll = wr
    v = new_visitor()

    def broken(**kwargs):
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem")

    monkeypatch.setattr(status._visitors, "get_item", broken)
    code, b = poll(v)
    assert code == 503 and b["error"] == "waiting_room_unavailable"


def test_polls_after_admission_only_read(wr, monkeypatch):
    """Cost: ~1,200 people poll every 10 s at peak. Once admitted, a poll must be a read, never another
    (conditional, failing) write to the admissions table."""
    status, new_visitor, poll = wr
    v = new_visitor()
    poll(v)
    writes = []
    real_put = status._admissions.put_item
    monkeypatch.setattr(status._admissions, "put_item", lambda **kw: writes.append(kw) or real_put(**kw))
    for _ in range(3):
        assert poll(v)[1]["status"] == "admitted"
    assert writes == []
